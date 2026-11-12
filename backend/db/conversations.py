"""会话 / 消息 / 工具配置 CRUD — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
"""
import json
from typing import Optional

from db._conn import _get_conn
from db._util import _new_id, _now


def create_conversation(title: str = "New Chat", mode: str = "ssh") -> dict:
    """创建新会话"""
    cid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO conversations (id, title, status, mode, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?, ?)",
            (cid, title, mode, now, now),
        )
        conn.commit()
        return {
            "id": cid,
            "title": title,
            "status": "completed",
            "mode": mode,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        conn.close()


def list_conversations(limit: int = 50, offset: int = 0, mode: Optional[str] = None) -> list[dict]:
    """获取会话列表 (按更新时间倒序)，附带最后一条消息预览和状态

    两处排序都带唯一兜底键（c.id / m.rowid）：_now() 只到秒，同批写入的行必然并列，
    而翻页是两次独立请求 —— 没有兜底键时 SQLite 对同键行的次序由查询计划自己决定，
    同一条会话会印在两页上、另一条永远翻不到；预览子查询同理，`LIMIT 1` 会在同秒的
    用户消息和回复里挑一条随机的。与 get_agent_runs / get_recent_messages 同一口径。
    """
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode is not None:
            # 注入面说明：conditions 只可能命中这一条**固定字面量**片段，
            # 值经 params 走 ? 占位符绑定 —— f-string 里拼进 SQL 的 {where}
            # 没有任何外部输入通道（CWE-89 加固时的显式声明）。
            conditions.append("c.mode = ?")
            params.append(mode)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        rows = conn.execute(f"""
            SELECT c.id, c.title, c.status, c.mode, c.created_at, c.updated_at,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS preview
            FROM conversations c
            {where}
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_conversation(cid: str) -> Optional[dict]:
    """获取单个会话"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, title, status, mode, created_at, updated_at FROM conversations WHERE id = ?",
            (cid,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_conversation_status(cid: str, status: str) -> None:
    """更新会话状态"""
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, cid),
        )
        conn.commit()
    finally:
        conn.close()


def try_set_default_conversation_title(cid: str, title: str,
                                       default_titles: tuple) -> bool:
    """C-review MINOR-1：仅当当前标题仍是默认值时才更新（原子 check-and-act）。

    标题 LLM 调用长达 30s：窗口期内用户改的自定义标题被无条件覆盖（原实现
    只在调用前检查一次）。改为条件 UPDATE，检查与写入同一条语句原子完成；
    返回 False 表示用户已改名，放弃覆盖。
    """
    now = _now()
    placeholders = ", ".join("?" * len(default_titles)) if default_titles else "''"
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE conversations SET title = ?, updated_at = ? "
            f"WHERE id = ? AND (title IS NULL OR title IN ({placeholders}))",
            (title, now, cid, *default_titles),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_conversation(cid: str) -> bool:
    """删除会话 (级联删除消息)"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def touch_conversation(cid: str) -> None:
    """更新会话的 updated_at"""
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, cid),
        )
        conn.commit()
    finally:
        conn.close()


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    reasoning: Optional[str] = None,
    model_used: Optional[str] = None,
    tokens_used: int = 0,
    run_id: Optional[str] = None,
    agent_trace: Optional[dict] = None,
) -> dict:
    """添加消息"""
    mid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                conversation_id,
                role,
                content,
                reasoning,
                model_used,
                tokens_used,
                run_id,
                json.dumps(agent_trace, ensure_ascii=False) if agent_trace else None,
                now,
            ),
        )
        conn.commit()
        return {
            "id": mid,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "reasoning": reasoning,
            "model_used": model_used,
            "tokens_used": tokens_used,
            "run_id": run_id,
            "agent_trace": agent_trace,
            "created_at": now,
        }
    finally:
        conn.close()


def get_messages(
    conversation_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """获取会话消息 (按时间正序)"""
    conn = _get_conn()
    try:
        # F-4 修复：返回最新 N 条（按时间正序输出）。
        # 原 ORDER BY created_at ASC LIMIT ? 返回的是最旧 N 条，>50 条的会话
        # 重载后只显示最新消息之前的内容。内层子查询按 created_at DESC, rowid DESC
        # 取窗口（offset 语义保持为"从新到旧"窗口内偏移），外层再按时间正序输出。
        # C-review MINOR-3：同秒消息用 rowid（插入序）兜底 — id 是随机 uuid，
        # 按 id 排序不是时间序，同秒消息仍可能乱序。
        # 修复：rowid 必须由内层显式投影（AS msg_rowid）—— 派生表没有隐式 rowid，
        # 外层直接 ORDER BY rowid 在 SQLite 3.51 实测报 "no such column: rowid"。
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at "
            "FROM ("
            "  SELECT id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at, rowid AS msg_rowid "
            "  FROM messages WHERE conversation_id = ? "
            "  ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
            ") ORDER BY created_at ASC, msg_rowid ASC",
            (conversation_id, limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("agent_trace"):
                try:
                    row_dict["agent_trace"] = json.loads(row_dict["agent_trace"])
                except Exception:
                    row_dict["agent_trace"] = None
            result.append(row_dict)
        return result
    finally:
        conn.close()


def get_recent_messages(conversation_id: str, limit: int = 20) -> list[dict]:
    """获取最近 N 条消息 (按时间正序返回，用于构建 LLM 上下文)"""
    conn = _get_conn()
    try:
        # C-review MINOR-3：created_at 是秒级时间戳，同秒消息加 rowid（插入序）
        # 兜底排序，避免乱序喂 LLM
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def count_messages_by_role(conversation_id: str, role: str) -> int:
    """统计会话中指定角色的消息数（审查：替代 get_messages(limit=1000) 全量拉取只计数）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE conversation_id = ? AND role = ?",
            (conversation_id, role),
        ).fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def upsert_tool_config(tool_name: str, mode: str, enabled: bool) -> dict:
    """插入或更新工具配置（按 tool_name + mode 唯一索引）"""
    import uuid as _uuid
    from datetime import datetime, timezone
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        existing = conn.execute(
            "SELECT id FROM tools_config WHERE tool_name = ? AND mode = ? LIMIT 1",
            (tool_name, mode),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE tools_config SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now, existing["id"]),
            )
            conn.commit()
            return {"id": existing["id"], "tool_name": tool_name, "mode": mode, "enabled": enabled, "updated_at": now}
        else:
            new_id = _uuid.uuid4().hex[:12]
            # B-MINOR 修复：SELECT→INSERT 非原子（UNIQUE(tool_name,mode) 下
            # 并发双 INSERT 会 500）。改为 INSERT ... ON CONFLICT DO UPDATE，
            # 冲突时回退到 UPDATE 既有行，消除竞态窗口
            conn.execute(
                "INSERT INTO tools_config (id, tool_name, mode, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tool_name, mode) DO UPDATE SET "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (new_id, tool_name, mode, 1 if enabled else 0, now, now),
            )
            conn.commit()
            # ON CONFLICT 命中时行 id 是既有值，重查保证返回真实 id
            row = conn.execute(
                "SELECT id FROM tools_config WHERE tool_name = ? AND mode = ?",
                (tool_name, mode),
            ).fetchone()
            real_id = row["id"] if row else new_id
            return {"id": real_id, "tool_name": tool_name, "mode": mode, "enabled": enabled, "updated_at": now}
    finally:
        conn.close()


def list_disabled_tools(mode: str = "all") -> list[str]:
    """获取指定模式下被禁用的工具名列表（用于 register_builtin_tools 过滤）"""
    conn = _get_conn()
    try:
        # 同时匹配 mode='all'（全局禁用）和 mode=指定模式
        rows = conn.execute(
            "SELECT tool_name FROM tools_config WHERE enabled = 0 AND (mode = ? OR mode = 'all')",
            (mode,),
        ).fetchall()
        return [r["tool_name"] for r in rows]
    finally:
        conn.close()
