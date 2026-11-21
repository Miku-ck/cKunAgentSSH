"""保留清理（prune）— P2-3 从 database.py 拆出，函数体与入口签名不变。"""
from db import _conn as _conn_mod


def prune_agent_runs(max_count: int = 5000) -> int:
    """删除最旧的 agent_runs（连带级联删除其 tool_calls），返回删除的 run 数。

    每个 Agent 运行插一行 agent_runs、每次工具调用插一行 tool_calls；
    两者只靠 conversation 级联回收，用户长期不删会话时无界增长
    （tool_calls 是最大头）。保留最近 max_count 个 run，由 SSE 事件流
    finally 低频节流触发（每 6h），不放在热路径上。
    注意：tool_calls 经 agent_run_id 外键 ON DELETE CASCADE 级联删除，
    故只删 agent_runs 即可，无需单独 prune tool_calls。
    """
    conn = _conn_mod._get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM agent_runs WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  ORDER BY started_at DESC
                              ) AS rn
                       FROM agent_runs
                       WHERE status <> 'waiting_confirmation'
                   ) WHERE rn > ?
               )""",
            (max_count,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def prune_conversations(keep_count: int = 1000) -> list[str]:
    """删除最旧的活动已结束的 conversations，保留最近 keep_count 个，返回被删会话 id。

    设计（第 8 轮审查 #2）：conversations 是用户聊天记录，默认**不**自动清理
    （用户数据，删除=数据丢失）。本函数仅当用户在设置里【显式开启】会话保留
    清理时才被调用，且只删 status='completed' 的会话——**永不碰 in_progress**
    （可能有 run 进行中）。

    "最近活跃" 判定用 MAX(messages.created_at)（真实最后一条消息时间），回退
    updated_at / created_at。注意不能用 conversations.updated_at 单字段：它只在
    agent 启动/状态/改标题时更新，普通聊天消息不更新，按它排序会误删近期活跃的会话。

    返回 id 而非删除数：调用方要用它回收 PermissionGate 的 session 授权池
    （这里是会话终结的第二条路径），拿不到 id 就只能看着被回收会话的授权键
    在进程内逐会话累积。先查 id 再按显式 id 删除（不用 IN (子查询)）：
    窗口查询与删除之间若有并发写入，两次求值会漂移，授权池就会清到没删的
    会话 / 漏掉已删的会话；显式 id 保证「删的 == 报的」。

    删除经外键级联回收 messages + agent_runs（再级联 tool_calls），故只删
    conversations 即可。keep_count<=0 视为不限制（不删）。
    """
    if keep_count is None or keep_count <= 0:
        return []
    conn = _conn_mod._get_conn()
    try:
        ids = [
            row[0] for row in conn.execute(
                """SELECT id FROM (
                       SELECT c.id,
                              ROW_NUMBER() OVER (
                                  ORDER BY COALESCE(
                                      (SELECT MAX(m.created_at) FROM messages m
                                       WHERE m.conversation_id = c.id),
                                      c.updated_at,
                                      c.created_at
                                  ) DESC
                              ) AS rn
                       FROM conversations c
                       WHERE c.status = 'completed'
                   ) WHERE rn > ?""",
                (keep_count,),
            ).fetchall()
        ]
        if not ids:
            return []
        # 占位符个数随 id 数量拼装，id 本身仍走参数绑定（外部输入不进 SQL 文本）
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM conversations WHERE id IN ({placeholders})", ids)
        conn.commit()
        return ids
    finally:
        conn.close()
