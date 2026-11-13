"""Agent Runs / 工具调用 CRUD — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
迁移期两份实现必须逐字一致（AST parity，参照 tests/test_db_schedules_parity.py 的口径）。
"""
from typing import Optional

from db._conn import _assert_sql_ident, _assert_sql_ident_list, _get_conn
from db._util import _new_id, _now

__all__ = [
    "create_agent_run",
    "update_agent_run",
    "mark_stale_agent_runs_failed",
    "reset_stale_in_progress_conversations",
    "get_agent_run",
    "get_agent_runs",
    "count_agent_runs",
    "add_tool_call",
    "update_tool_call",
    "get_tool_calls",
]


def create_agent_run(run_id: str, conversation_id: str, task: str,
                     max_iterations: int = 10, mode: str = "",
                     model_used: str = "",
                     expert_id: Optional[str] = None,
                     expert_name: Optional[str] = None,
                     expert_snapshot_json: Optional[str] = None) -> None:
    """创建 Agent 运行记录。专家快照由服务端生成，调用方只传已序列化文本。"""
    conn = _get_conn()
    try:
        # 原 INSERT 保持逐字不变：未选专家的路径（含自建精简 schema 的测试、
        # 定时任务等无人值守入口）不碰任何新列。专家字段只在真的选中专家时
        # 追加一次 UPDATE —— 新列缺失也只影响用了专家的那条路径。
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, task, status, max_iterations, "
            "mode, model_used, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (run_id, conversation_id, task, max_iterations, mode, model_used, _now()),
        )
        if expert_id is not None:
            conn.execute(
                "UPDATE agent_runs SET expert_id = ?, expert_name = ?, "
                "expert_snapshot_json = ? WHERE id = ?",
                (expert_id, expert_name, expert_snapshot_json, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_agent_run(run_id: str, status: str, iteration: Optional[int] = None,
                     state_history: Optional[str] = None, total_tokens: Optional[int] = None,
                     prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
                     cost_usd: Optional[float] = None, duration_ms: Optional[int] = None,
                     tool_call_count: Optional[int] = None,
                     context_summary: Optional[str] = None,
                     undo_recorded: Optional[int] = None) -> None:
    """更新 Agent 运行状态（含 token/成本/耗时统计）

    C6 修复：所有可选字段用 `is not None` 判断，避免合法的 0 值被跳过。
    iteration 改为 Optional：仅在显式传入时写入，避免仅更新 status 时被重置为 0。
    审查 NIT：completed_at 仅在终态（completed/failed/human_help）时写入，
    原实现每次更新（含中间状态）都刷新 completed_at，时间字段失真。
    审查R2修复：其余统计参数的默认值同步改为 None —— 默认 0/0.0 配上
    `is not None` 判断，让 status-only 调用（error/finally 分支标 failed）
    把已写入的真实 token/成本/耗时统计覆盖成 0。显式传 0 仍照常写 0。
    """
    # B-7 修复：max_reached 同为终态（contracts.State.MAX_REACHED / reflection /
    # 统计查询均视其为终态），completed_at 需一并写入
    _TERMINAL_RUN_STATUSES = ("completed", "failed", "human_help", "max_reached")
    conn = _get_conn()
    try:
        sets = ["status = ?"]
        params: list = [status]
        if status in _TERMINAL_RUN_STATUSES:
            sets.append("completed_at = ?")
            params.append(_now())
        if iteration is not None:
            sets.append("iteration = ?")
            params.append(iteration)
        # C6 修复：用 is not None 代替 truthy 判断
        if state_history is not None:
            sets.append("state_history = ?")
            params.append(state_history)
        if total_tokens is not None:
            sets.append("total_tokens = ?")
            params.append(total_tokens)
        if prompt_tokens is not None:
            sets.append("prompt_tokens = ?")
            params.append(prompt_tokens)
        if completion_tokens is not None:
            sets.append("completion_tokens = ?")
            params.append(completion_tokens)
        if cost_usd is not None:
            sets.append("cost_usd = ?")
            params.append(cost_usd)
        if duration_ms is not None:
            sets.append("duration_ms = ?")
            params.append(duration_ms)
        if tool_call_count is not None:
            sets.append("tool_call_count = ?")
            params.append(tool_call_count)
        if context_summary is not None:
            sets.append("context_summary = ?")
            params.append(context_summary)
        if undo_recorded is not None:
            sets.append("undo_recorded = ?")
            params.append(undo_recorded)
        params.append(run_id)
        conn.execute(
            f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def mark_stale_agent_runs_failed() -> int:
    """C-review MINOR-12：启动对账 — 把仍停留在 running/init 的 run 标记 failed。

    进程崩溃/重启后，进行中的 run 在 DB 里永远滞留 'running'（内存中的
    并发防护随进程丢失），前端会一直显示"运行中"。进程启动时调用一次。
    返回被标记的条数。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE agent_runs SET status = 'failed', completed_at = ? "
            "WHERE status IN ('running', 'init')",
            (_now(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reset_stale_in_progress_conversations() -> int:
    """启动对账 — 把仍停留在 in_progress 的会话复位为 completed。

    Agent run 启动时将会话置 in_progress、结束时复位为 completed；若进程在
    运行中崩溃/被杀，该复位逻辑随进程丢失，会话永久滞留 in_progress。
    仅启动时调用一次：正常运行中 in_progress 表示真有 run 进行中，不能动。
    返回被复位的条数。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE conversations SET status = 'completed', updated_at = ? "
            "WHERE status = 'in_progress'",
            (_now(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_agent_run(run_id: str) -> Optional[dict]:
    """获取单个 Agent 运行记录"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_RUN_LIST_COLUMNS = (
    "id, conversation_id, task, status, mode, model_used, iteration, max_iterations, "
    "total_tokens, cost_usd, duration_ms, tool_call_count, undo_recorded, started_at, completed_at"
)
# 投影列是模块级常量，启动时校验一次，防止字面量被改坏带进 SELECT 列表
_assert_sql_ident_list(_RUN_LIST_COLUMNS)


def _run_filters(conversation_id: str = None, status: str = None,
                 run_id: str = None) -> tuple[str, list]:
    """按固定列序拼 WHERE 与参数；值全走绑定，列名是这里的字面量。"""
    conds: list[str] = []
    params: list = []
    for col, val in (("conversation_id", conversation_id), ("status", status), ("id", run_id)):
        _assert_sql_ident(col)
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    return (" WHERE " + " AND ".join(conds)) if conds else "", params


def get_agent_runs(conversation_id: str = None, limit: int = 20, offset: int = 0,
                   status: str = None, run_id: str = None,
                   full: bool = False) -> list[dict]:
    """Agent 运行记录列表（倒序）。

    默认走**投影列**：`state_history` 与 `context_summary` 是整段 JSON 文本，
    一张 50 行的表把它们全带上，列表接口自己就把响应撑大几十倍，而表格里没有
    一列用得到。两个现调用方（`GET /agent/runs`、consciousness_tick 的失败回看）
    都只读投影里的列，`full=True` 目前没有生产调用方，留作真要整行时的出口。

    倒序必须带 `id` 兜底：`_now()` 只到秒，同一秒起的多条 run 在 SQLite 里次序
    由查询计划自己决定。翻页是两次独立请求，没有 tiebreak 就会把同一条印在两页上、
    同时漏掉另一条，页脚的"第 51–100 条 / 共 N 条"跟着分叉。
    """
    where, params = _run_filters(conversation_id, status, run_id)
    cols = "*" if full else _RUN_LIST_COLUMNS
    params = params + [limit, offset]
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {cols} FROM agent_runs{where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_agent_runs(conversation_id: str = None, status: str = None,
                     run_id: str = None) -> int:
    """与列表同一份过滤条件的总数。

    两处各抄一遍 WHERE 迟早对不上：审计表那边 count 曾漏掉 action/run_id，
    翻页时"共 N 条"与眼前这几行不是同一个集合。
    """
    where, params = _run_filters(conversation_id, status, run_id)
    conn = _get_conn()
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM agent_runs{where}", params
        ).fetchone()[0]
    finally:
        conn.close()


def add_tool_call(agent_run_id: str, iteration: int, tool_name: str,
                  arguments: str, result: Optional[str] = None,
                  success: Optional[bool] = None,
                  duration_ms: int = 0) -> str:
    """记录工具调用，返回生成的 tool_call_id（H10 修复）

    本函数只在**调用开始前**被调用（routes/agent.py 的 tool_call_start），结果由
    update_tool_call 回写。因此 result/success 默认落 NULL = 「还没有结果」：
    原默认 ("", True) 让一条跑到一半崩溃、永远等不到回写的调用在轨迹回放里显示成
    「已调用（成功）」——同一件事在内联快照里显示「进行中」，两处读数互相矛盾。
    NULL 不是 False：False 是「工具明确失败」。
    """
    tool_call_id = _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO tool_calls (id, agent_run_id, iteration, tool_name, arguments, result, success, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_call_id, agent_run_id, iteration, tool_name, arguments, result,
             None if success is None else (1 if success else 0), duration_ms, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return tool_call_id


def update_tool_call(tool_call_id: str, result: str, success: bool, duration_ms: int = 0) -> None:
    """更新工具调用结果"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tool_calls SET result = ?, success = ?, duration_ms = ? WHERE id = ?",
            (result, 1 if success else 0, duration_ms, tool_call_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_tool_calls(agent_run_id: str) -> list[dict]:
    """获取某次 Agent 运行的所有工具调用

    带 rowid（插入序）兜底：一次 run 里连续几个工具调用常常落在同一秒，只按
    created_at 排时同键行的次序由查询计划决定，轨迹回放会印出「先重启服务、
    后改配置」这种因果颠倒的顺序 —— id 是 uuid，不能当次序用，故用 rowid。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tool_calls WHERE agent_run_id = ? ORDER BY created_at, rowid",
            (agent_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
