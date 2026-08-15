"""
SQLite 数据库层 — 会话与消息持久化

连接管理：线程本地连接池。每个线程持有一个持久连接，复用避免反复
connect/close 的开销；close() 被包装为 rollback-and-keep-alive，
现有代码的 `finally: conn.close()` 模式无需任何改动。
"""
import sqlite3
import uuid
import json
import atexit
import threading
from datetime import datetime, timezone
from typing import Optional

_db_path: str = ""

# ── 线程本地连接池 ──
_thread_local = threading.local()
# 跟踪所有线程的连接，用于进程退出时统一清理
_all_connections: list[sqlite3.Connection] = []
_conn_lock = threading.Lock()


class _PooledConnection:
    """sqlite3.Connection 的包装类，用于线程本地连接池。

    - 所有属性/方法委托到底层 _conn（通过 __getattr__）
    - close() 不真正关闭连接，而是执行 rollback 清理未提交事务，
      连接保留在线程本地存储中供下次复用
    - 这样现有代码中 51 处 `finally: conn.close()` 无需改动
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        """No-op close：回滚未提交事务，但不关闭底层连接。"""
        try:
            self._conn.rollback()
        except Exception:
            pass  # 连接可能已损坏，下次 _get_conn() 会重建


def _create_connection() -> sqlite3.Connection:
    """创建一个新的底层连接并应用 PRAGMA 设置。"""
    conn = sqlite3.connect(_db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_conn() -> _PooledConnection:
    """获取当前线程的数据库连接（复用线程本地连接）。

    首次调用时创建底层连接并缓存到 threading.local()；
    后续调用直接返回包装后的连接。
    close() 被包装为 no-op，所以 `finally: conn.close()` 不会真正关闭连接。
    """
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        # 验证连接仍然有效（长时间空闲后可能已断开）
        try:
            conn.execute("SELECT 1")
        except Exception:
            # 连接已损坏，从跟踪列表移除并重建
            with _conn_lock:
                if conn in _all_connections:
                    _all_connections.remove(conn)
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
            _thread_local.conn = None

    if conn is None:
        conn = _create_connection()
        with _conn_lock:
            _all_connections.append(conn)
        _thread_local.conn = conn

    return _PooledConnection(conn)


def _close_all_connections() -> None:
    """关闭所有线程的连接。进程退出时由 atexit 钩子调用。"""
    with _conn_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()

    # 清除当前线程的引用
    if hasattr(_thread_local, "conn"):
        _thread_local.conn = None


atexit.register(_close_all_connections)


def init_db(db_path: str) -> None:
    """初始化数据库: 建表 + 启用 WAL + 迁移

    db_path 为绝对路径时直接用；相对路径落在本模块 cwd（脆弱，仅兜底）。
    显式打印实际路径便于排查「重启丢库」类问题。
    """
    import os
    import logging
    log = logging.getLogger("cKunAgentic.database")
    # 确保父目录存在（Electron 传入 userData/db/ckunagentic.db 时 dir 可能未建）
    try:
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
    except OSError as e:
        log.warning("init_db: mkdir parent failed for %s: %s", db_path, e)

    global _db_path
    _db_path = db_path
    log.info("init_db: opening %s", os.path.abspath(db_path))
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id           TEXT PRIMARY KEY,
                title        TEXT DEFAULT 'New Chat',
                status       TEXT DEFAULT 'completed',
                mode         TEXT NOT NULL DEFAULT 'ssh',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                reasoning       TEXT,
                model_used      TEXT,
                tokens_used     INTEGER DEFAULT 0,
                run_id          TEXT,                -- 关联 agent_runs.id（Agent 模式产生）
                agent_trace     TEXT,                -- AgentTraceSnapshot JSON（Agent 模式产生）
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_messages_conv
                ON messages(conversation_id, created_at);

            CREATE TABLE IF NOT EXISTS memories (
                id              TEXT PRIMARY KEY,
                mode            TEXT NOT NULL,
                content         TEXT NOT NULL,
                source_conv_id  TEXT,
                last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
                use_count       INTEGER DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_memories_mode
                ON memories(mode);
            CREATE INDEX IF NOT EXISTS ix_memories_used
                ON memories(mode, last_used_at);

            CREATE TABLE IF NOT EXISTS skills (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                slug        TEXT NOT NULL UNIQUE,
                description TEXT,
                category    TEXT,
                system_prompt TEXT NOT NULL,
                source      TEXT DEFAULT 'manual',
                is_builtin  INTEGER DEFAULT 0,
                enabled     INTEGER DEFAULT 0,
                priority    INTEGER DEFAULT 0,           -- 优先级：数值越大越优先
                trigger_words TEXT DEFAULT '[]',         -- 触发词 JSON 数组，如 ["关键词1","关键词2"]
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_skills_source
                ON skills(source);

            -- 技能按模式隔离的启用配置（按模式禁用某些技能）
            CREATE TABLE IF NOT EXISTS skill_mode_config (
                id          TEXT PRIMARY KEY,
                skill_id    TEXT NOT NULL,
                mode        TEXT NOT NULL,
                enabled     INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(skill_id, mode)
            );

            CREATE TABLE IF NOT EXISTS models_config (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                model_id         TEXT,
                provider         TEXT NOT NULL DEFAULT 'custom',
                base_url         TEXT NOT NULL,
                api_key          TEXT,
                supports_thinking INTEGER DEFAULT 0,
                supports_image   INTEGER DEFAULT 0,
                supports_tool_calling INTEGER DEFAULT 0,
                is_default       INTEGER DEFAULT 0,
                is_default_ssh   INTEGER DEFAULT 0,
                temperature      REAL DEFAULT 0.7,
                max_tokens       INTEGER DEFAULT 16384,
                max_output_tokens INTEGER DEFAULT 4096,
                top_p            REAL DEFAULT 0.9,
                sort_order       INTEGER DEFAULT 0,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Agent 运行记录
            CREATE TABLE IF NOT EXISTS agent_runs (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                task            TEXT,
                status          TEXT DEFAULT 'init',
                iteration       INTEGER DEFAULT 0,
                max_iterations  INTEGER DEFAULT 10,
                state_history   TEXT,
                total_tokens    INTEGER DEFAULT 0,
                prompt_tokens   INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cost_usd        REAL DEFAULT 0.0,
                duration_ms     INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                mode            TEXT,
                model_used      TEXT,
                started_at      TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at    TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS ix_agent_runs_conv
                ON agent_runs(conversation_id);

            -- 工具调用日志
            CREATE TABLE IF NOT EXISTS tool_calls (
                id            TEXT PRIMARY KEY,
                agent_run_id  TEXT NOT NULL,
                iteration     INTEGER DEFAULT 0,
                tool_name     TEXT NOT NULL,
                arguments     TEXT,
                result        TEXT,
                success       INTEGER DEFAULT 1,
                duration_ms   INTEGER DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS ix_tool_calls_run
                ON tool_calls(agent_run_id);

            -- Agent 文件变更记录（供 IDE 模式审查面板跨进程恢复）
            CREATE TABLE IF NOT EXISTS agent_file_changes (
                id              TEXT PRIMARY KEY,
                agent_run_id    TEXT NOT NULL,
                file_path       TEXT NOT NULL,
                abs_path        TEXT NOT NULL,
                operation       TEXT NOT NULL,           -- create / modify / delete
                added_lines     INTEGER DEFAULT 0,
                removed_lines   INTEGER DEFAULT 0,
                before_size     INTEGER DEFAULT 0,
                after_size      INTEGER DEFAULT 0,
                accepted_at     TEXT,                    -- 用户点击"保留"时写入
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS ix_agent_file_changes_run
                ON agent_file_changes(agent_run_id);

            -- Agent 三层记忆（episodic=情景总结, semantic=知识事实+embedding）
            CREATE TABLE IF NOT EXISTS agent_memories (
                id              TEXT PRIMARY KEY,
                type            TEXT NOT NULL,           -- episodic / semantic
                mode            TEXT NOT NULL,           -- work / ide / ssh
                content         TEXT NOT NULL,
                source_conv_id  TEXT,
                agent_run_id    TEXT,
                embedding       TEXT,                    -- JSON list[float]，仅 semantic
                importance      REAL DEFAULT 0.5,
                last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
                use_count       INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_agent_memories_mode_type
                ON agent_memories(mode, type);
            CREATE INDEX IF NOT EXISTS ix_agent_memories_used
                ON agent_memories(type, last_used_at);

            -- 应用设置（key-value 存储，供后端读取 memory.enabled 等）
            CREATE TABLE IF NOT EXISTS app_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Agent 策略（reflection=反思结果, evolved_config=演化后的配置）
            CREATE TABLE IF NOT EXISTS agent_strategies (
                id              TEXT PRIMARY KEY,
                mode            TEXT NOT NULL,
                strategy_type   TEXT NOT NULL,           -- reflection / evolved_config
                name            TEXT NOT NULL,
                content         TEXT NOT NULL,
                config          TEXT,                    -- JSON 详细配置
                score           REAL DEFAULT 0.5,        -- 评分/置信度
                enabled         INTEGER DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_agent_strategies_mode_type
                ON agent_strategies(mode, strategy_type);
            CREATE INDEX IF NOT EXISTS ix_agent_strategies_score
                ON agent_strategies(strategy_type, score DESC);

            -- 工具配置（运行时开关）
            CREATE TABLE IF NOT EXISTS tools_config (
                id          TEXT PRIMARY KEY,
                tool_name   TEXT NOT NULL,
                mode        TEXT NOT NULL DEFAULT 'all',
                enabled     INTEGER DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ix_tools_config_name_mode
                ON tools_config(tool_name, mode);


        """)

        # P7: 兼容旧库 — 增量补齐 agent_runs 新字段（executescript 不支持 PRAGMA 循环）
        cur = conn.execute("PRAGMA table_info(agent_runs)")
        existing_cols = {row[1] for row in cur.fetchall()}
        for col, col_def in [
            ("prompt_tokens",     "INTEGER DEFAULT 0"),
            ("completion_tokens", "INTEGER DEFAULT 0"),
            ("cost_usd",          "REAL DEFAULT 0.0"),
            ("duration_ms",       "INTEGER DEFAULT 0"),
            ("tool_call_count",   "INTEGER DEFAULT 0"),
            ("mode",              "TEXT"),
            ("model_used",        "TEXT"),
        ]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} {col_def}")
        _migrate_db(conn)
        _seed_models(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_db(conn: sqlite3.Connection) -> None:
    """数据库迁移：为旧表添加新字段"""
    cursor = conn.execute("PRAGMA table_info(messages)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'reasoning' not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT")
    if 'run_id' not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
    if 'agent_trace' not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN agent_trace TEXT")

    cursor = conn.execute("PRAGMA table_info(conversations)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'status' not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN status TEXT DEFAULT 'completed'")
    if 'mode' not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'ssh'")

    cursor = conn.execute("PRAGMA table_info(skills)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'source' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN source TEXT DEFAULT 'manual'")
    if 'enabled' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN enabled INTEGER DEFAULT 0")
    if 'category' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN category TEXT")
    if 'system_prompt' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''")
    # skills 表迁移：新增 priority 和 trigger_words 字段
    if 'priority' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN priority INTEGER DEFAULT 0")
    if 'trigger_words' not in columns:
        conn.execute("ALTER TABLE skills ADD COLUMN trigger_words TEXT DEFAULT '[]'")

    # ── models_config 表迁移（独立块，不嵌套在 skills 检查内）──
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='models_config'"
    )
    if cursor.fetchone():
        cursor = conn.execute("PRAGMA table_info(models_config)")
        cols = {row[1] for row in cursor.fetchall()}

        # 旧列 full_id → 重命名为 model_id
        if 'full_id' in cols and 'model_id' not in cols:
            conn.execute("ALTER TABLE models_config RENAME COLUMN full_id TO model_id")
            cols = {c for c in cols if c != 'full_id'}
            cols.add('model_id')

        # 确保 model_id 列存在（极旧版本可能没有）
        if 'model_id' not in cols:
            conn.execute("ALTER TABLE models_config ADD COLUMN model_id TEXT")
            cols.add('model_id')

        # 修正 name 与 model_id 的值（它们可能正好反了）
        _fix_model_name_and_id(conn)

        if 'is_default_ssh' not in cols:
            conn.execute("ALTER TABLE models_config ADD COLUMN is_default_ssh INTEGER DEFAULT 0")

        # 新增：supports_image, supports_tool_calling, max_output_tokens
        if 'supports_image' not in cols:
            conn.execute("ALTER TABLE models_config ADD COLUMN supports_image INTEGER DEFAULT 0")
        if 'supports_tool_calling' not in cols:
            conn.execute("ALTER TABLE models_config ADD COLUMN supports_tool_calling INTEGER DEFAULT 0")
        if 'max_output_tokens' not in cols:
            conn.execute("ALTER TABLE models_config ADD COLUMN max_output_tokens INTEGER DEFAULT 4096")


def _fix_model_name_and_id(conn: sqlite3.Connection) -> None:
    """修正 models_config 表中 name 与 model_id 的值（仅当确实反了时）

    幂等：只有当 name 看起来像 model_id（含 / 或 Qwen/ 前缀）且 model_id
    看起来像显示名时才互换，避免重启后强制覆盖用户在前端的重命名。
    """
    rows = conn.execute("SELECT id, name, model_id FROM models_config").fetchall()
    for r in rows:
        n, mid = r["name"], r["model_id"]
        if not n or not mid:
            continue
        # 仅当 name 看起来像 model_id（含 / 或匹配 Qwen/ 前缀），且 model_id 看起来像显示名时才互换
        looks_like_id = lambda s: bool(s) and ("/" in s or s.startswith("Qwen/") or s.startswith("qwen/"))
        if looks_like_id(n) and not looks_like_id(mid):
            conn.execute(
                "UPDATE models_config SET name = ?, model_id = ?, updated_at = ? WHERE id = ?",
                (mid, n, _now(), r["id"])
            )


def _seed_models(conn: sqlite3.Connection) -> None:
    """首次运行时种子默认模型配置（来自 config.py 的默认值）"""
    row = conn.execute("SELECT COUNT(*) AS cnt FROM models_config").fetchone()
    if row["cnt"] > 0:
        return
    try:
        from config import settings
        base_url = settings.vllm_base_url
        thinking_url = settings.vllm_thinking_url or base_url
        api_key = settings.vllm_api_key
    except Exception:
        base_url = "http://192.168.150.40:9030/v1"
        thinking_url = "http://192.168.150.40:9031/v1"
        api_key = "EMPTY"
    now = _now()
    # 默认模型 1: Qwen3-VL（对应服务器 --served-model-name Qwen3-VL）
    conn.execute(
        "INSERT INTO models_config "
        "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
        "is_default, is_default_ssh, "
        "temperature, max_tokens, max_output_tokens, top_p, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Qwen3-VL", "Qwen3-VL-8B-Instruct", "Qwen3-VL", "qwen",
            base_url, api_key, 0, 0, 0,
            0.7, 16384, 4096, 0.9, 0, now, now,
        ),
    )
    # 默认模型 2: Qwen3-VL-Thinking（对应服务器 --served-model-name Qwen3-VL-Thinking）
    conn.execute(
        "INSERT INTO models_config "
        "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
        "is_default, is_default_ssh, "
        "temperature, max_tokens, max_output_tokens, top_p, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Qwen3-VL-Thinking", "Qwen3-VL-8B-Thinking", "Qwen3-VL-Thinking", "qwen",
            thinking_url, api_key, 1, 0, 0,
            0.7, 16384, 4096, 0.9, 1, now, now,
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex


# ── 会话 CRUD ──


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
    """获取会话列表 (按更新时间倒序)，附带最后一条消息预览和状态"""
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode is not None:
            conditions.append("c.mode = ?")
            params.append(mode)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        rows = conn.execute(f"""
            SELECT c.id, c.title, c.status, c.mode, c.created_at, c.updated_at,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC LIMIT 1) AS preview
            FROM conversations c
            {where}
            ORDER BY c.updated_at DESC
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


def update_conversation_title(cid: str, title: str) -> Optional[dict]:
    """更新会话标题"""
    now = _now()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, cid),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return {"id": cid, "title": title, "updated_at": now}
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


# ── 消息 CRUD ──


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
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
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
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


# ── 工具配置 CRUD ──


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
            conn.execute(
                "INSERT INTO tools_config (id, tool_name, mode, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_id, tool_name, mode, 1 if enabled else 0, now, now),
            )
            conn.commit()
            return {"id": new_id, "tool_name": tool_name, "mode": mode, "enabled": enabled, "created_at": now, "updated_at": now}
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


# ── 记忆 CRUD ──


def get_relevant_memories(mode: str, limit: int = 5) -> list[dict]:
    """获取某模式下最相关的记忆（按最近使用 + 使用次数排序）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, mode, content, source_conv_id, last_used_at, use_count, created_at "
            "FROM memories WHERE mode = ? "
            "ORDER BY last_used_at DESC, use_count DESC "
            "LIMIT ?",
            (mode, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── 技能 CRUD ──


def list_skills(limit: int = 200, offset: int = 0) -> list[dict]:
    """获取所有技能列表"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words, created_at, updated_at "
            "FROM skills ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_skills() -> list[dict]:
    """获取所有技能（含 disabled），用于注册表缓存"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words, created_at, updated_at "
            "FROM skills ORDER BY priority DESC, updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_skill(skill_id: str) -> Optional[dict]:
    """按 ID 获取单个技能"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words, created_at, updated_at "
            "FROM skills WHERE id = ?",
            (skill_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_skill_by_slug(slug: str) -> Optional[dict]:
    """按 slug 获取单个技能"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words, created_at, updated_at "
            "FROM skills WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_skill(
    name: str,
    slug: str,
    system_prompt: str,
    description: str = "",
    category: str = "",
    source: str = "manual",
    is_builtin: int = 0,
    enabled: int = 0,
    priority: int = 0,
    trigger_words: Optional[list] = None,
) -> dict:
    """创建技能，slug 冲突时自动追加后缀"""
    conn = _get_conn()
    try:
        original_slug = slug
        suffix = 1
        while conn.execute(
            "SELECT 1 FROM skills WHERE slug = ?", (slug,)
        ).fetchone() is not None:
            slug = f"{original_slug}-{suffix}"
            suffix += 1

        sid = _new_id()
        now = _now()
        # 触发词列表序列化为 JSON 字符串存储，与读取路径（TEXT 列）保持一致
        trigger_words_json = (
            json.dumps(trigger_words, ensure_ascii=False) if trigger_words else "[]"
        )
        conn.execute(
            "INSERT INTO skills (id, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, name, slug, description, category, system_prompt, source, is_builtin, enabled, priority, trigger_words_json, now, now),
        )
        conn.commit()
        return {
            "id": sid,
            "name": name,
            "slug": slug,
            "description": description,
            "category": category,
            "system_prompt": system_prompt,
            "source": source,
            "is_builtin": is_builtin,
            "enabled": enabled,
            "priority": priority,
            "trigger_words": trigger_words_json,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        conn.close()


def update_skill_enabled(skill_id: str, enabled: int) -> Optional[dict]:
    """更新技能启用状态"""
    now = _now()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE skills SET enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, now, skill_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return {"id": skill_id, "enabled": enabled, "updated_at": now}
    finally:
        conn.close()


def upsert_builtin_skill(
    name: str,
    slug: str,
    system_prompt: str,
    description: str = "",
    category: str = "",
    trigger_words: Optional[list] = None,
) -> dict:
    """同步内置技能到数据库（按 slug + is_builtin=1 upsert）

    与 create_skill 的区别：slug 冲突时更新而非追加后缀，
    避免每次同步都新增重复记录。保留用户已设置的 enabled 状态。
    """
    conn = _get_conn()
    try:
        now = _now()
        trigger_words_json = (
            json.dumps(trigger_words, ensure_ascii=False) if trigger_words else "[]"
        )
        existing = conn.execute(
            "SELECT id, enabled FROM skills WHERE slug = ? AND is_builtin = 1",
            (slug,),
        ).fetchone()
        if existing:
            # 更新内容字段，保留 enabled（用户可自定义启用状态）
            conn.execute(
                "UPDATE skills SET name = ?, system_prompt = ?, description = ?, "
                "category = ?, trigger_words = ?, updated_at = ? WHERE id = ?",
                (name, system_prompt, description, category, trigger_words_json, now, existing["id"]),
            )
            conn.commit()
            return {"id": existing["id"], "slug": slug, "updated": True}
        else:
            sid = _new_id()
            conn.execute(
                "INSERT INTO skills (id, name, slug, description, category, system_prompt, "
                "source, is_builtin, enabled, priority, trigger_words, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'builtin', 1, 0, 0, ?, ?, ?)",
                (sid, name, slug, description, category, system_prompt, trigger_words_json, now, now),
            )
            conn.commit()
            return {"id": sid, "slug": slug, "updated": False}
    finally:
        conn.close()


def list_builtin_skill_slugs() -> list[str]:
    """返回数据库中所有内置技能的 slug 列表（用于孤儿清理）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT slug FROM skills WHERE is_builtin = 1"
        ).fetchall()
        return [r["slug"] for r in rows]
    finally:
        conn.close()


def delete_builtin_skill_by_slug(slug: str) -> bool:
    """按 slug 删除内置技能（用于孤儿清理：文件系统已删除的技能）"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM skills WHERE slug = ? AND is_builtin = 1", (slug,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_skill(skill_id: str) -> bool:
    """删除技能（内置技能不可删除，由 is_builtin 控制）"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM skills WHERE id = ? AND is_builtin = 0", (skill_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 技能按模式隔离配置 CRUD ──


def list_disabled_skills_for_mode(mode: str) -> list[str]:
    """返回该模式下被禁用（enabled=0）的技能 ID 列表"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id FROM skill_mode_config WHERE mode = ? AND enabled = 0",
            (mode,),
        ).fetchall()
        return [r["skill_id"] for r in rows]
    finally:
        conn.close()


def list_all_skill_mode_configs() -> dict[str, dict[str, dict[str, bool]]]:
    """返回所有技能的按模式启用配置

    返回格式：{skill_id: {mode: {"enabled": bool}, ...}, ...}
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id, mode, enabled FROM skill_mode_config"
        ).fetchall()
        result: dict[str, dict[str, dict[str, bool]]] = {}
        for r in rows:
            skill_id = r["skill_id"]
            mode = r["mode"]
            enabled = r["enabled"] == 1
            result.setdefault(skill_id, {})[mode] = {"enabled": enabled}
        return result
    finally:
        conn.close()


def upsert_skill_mode_config(skill_id: str, mode: str, enabled: int) -> dict:
    """插入或更新某技能在某模式下的启用状态"""
    now = _now()
    conn = _get_conn()
    try:
        cfg_id = _new_id()
        conn.execute(
            "INSERT INTO skill_mode_config (id, skill_id, mode, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skill_id, mode) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (cfg_id, skill_id, mode, 1 if enabled else 0, now, now),
        )
        conn.commit()
        return {
            "id": cfg_id,
            "skill_id": skill_id,
            "mode": mode,
            "enabled": 1 if enabled else 0,
            "updated_at": now,
        }
    finally:
        conn.close()


# ── 模型配置 CRUD ──

def _mask_api_key(key: str | None) -> str:
    """脱敏 API Key：仅保留前4位 + ****"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"


def list_models_config() -> list[dict]:
    """获取所有模型配置（按 sort_order 正序，api_key 已脱敏）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, model_id, provider, base_url, api_key, "
            "supports_thinking, supports_image, supports_tool_calling, "
            "is_default, is_default_ssh, "
            "temperature, max_tokens, max_output_tokens, top_p, sort_order, created_at, updated_at "
            "FROM models_config ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
        result = [dict(r) for r in rows]
        for item in result:
            item["api_key"] = _mask_api_key(item.get("api_key"))
        return result
    finally:
        conn.close()


def get_model_config(model_id: str, mask_key: bool = True) -> Optional[dict]:
    """根据 id 获取模型配置（mask_key=True 时 api_key 已脱敏）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, name, model_id, provider, base_url, api_key, "
            "supports_thinking, supports_image, supports_tool_calling, "
            "is_default, is_default_ssh, "
            "temperature, max_tokens, max_output_tokens, top_p, sort_order, created_at, updated_at "
            "FROM models_config WHERE id = ?",
            (model_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        if mask_key:
            result["api_key"] = _mask_api_key(result.get("api_key"))
        return result
    finally:
        conn.close()


def create_model_config(
    name: str,
    base_url: str,
    api_key: str = "",
    model_id: str = "",
    provider: str = "custom",
    supports_thinking: bool = False,
    supports_image: bool = False,
    supports_tool_calling: bool = False,
    temperature: float = 0.7,
    # 默认 4096：远低于常见 vLLM 16K/32K 上下文，避免 max_tokens 占用全部窗口导致 400
    max_tokens: int = 4096,
    max_output_tokens: int = 4096,
    top_p: float = 0.9,
    is_default_ssh: bool = False,
) -> dict:
    """创建模型配置，返回完整记录"""
    # 去除首尾空格：粘贴时可能带入换行/空白，导致上游 API 校验失败（401）
    name = (name or "").strip()
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model_id = (model_id or "").strip()
    mid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        # 计算 sort_order（已有最大 + 1）
        row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS mx FROM models_config").fetchone()
        next_order = row["mx"] + 1
        conn.execute(
            "INSERT INTO models_config "
            "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
            "is_default, is_default_ssh, "
            "temperature, max_tokens, max_output_tokens, top_p, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid, name, model_id, provider, base_url, api_key,
                1 if supports_thinking else 0,
                1 if supports_image else 0,
                1 if supports_tool_calling else 0,
                1 if is_default_ssh else 0,
                temperature, max_tokens, max_output_tokens, top_p, next_order, now, now,
            ),
        )
        # 如果新模型被设为默认，需清除其他模型的默认标记
        if is_default_ssh:
            conn.execute("UPDATE models_config SET is_default_ssh = 0 WHERE id != ?", (mid,))
        conn.commit()
        return get_model_config(mid)  # type: ignore
    finally:
        conn.close()


def update_model_config(config_id: str, **kwargs) -> Optional[dict]:
    """更新模型配置（支持部分字段）

    参数 config_id 为记录主键（id 列），与 kwargs 中的 model_id 字段互不冲突。
    """
    allowed = {
        "name", "model_id", "provider", "base_url", "api_key",
        "supports_thinking", "supports_image", "supports_tool_calling",
        "temperature", "max_tokens", "max_output_tokens", "top_p", "sort_order",
        "is_default_ssh",
    }
    sets = []
    params = []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            # api_key 空字符串表示「保留原值」，跳过更新
            if k == "api_key" and v == "":
                continue
            sets.append(f"{k} = ?")
            params.append(1 if isinstance(v, bool) else v)
    # 记录是否存在（优先判断，避免 rowcount 0 的误判）
    existing = get_model_config(config_id)
    if not existing:
        return None
    if not sets:
        return existing
    now = _now()
    sets.append("updated_at = ?")
    params.append(now)
    # config_id 必须放在最后，对应 SQL 中 WHERE id = ? 的位置
    params.append(config_id)
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE models_config SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        # 如果更新了默认标记，需清除其他模型的默认标记
        if kwargs.get("is_default_ssh"):
            conn.execute("UPDATE models_config SET is_default_ssh = 0 WHERE id != ?", (config_id,))
        conn.commit()
        # rowcount 0 不代表记录不存在——SQLite 在值未变化时不计入 rowcount
        return get_model_config(config_id)
    finally:
        conn.close()


def delete_model_config(model_id: str) -> bool:
    """删除模型配置（默认模型不可删除）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT is_default, is_default_ssh FROM models_config WHERE id = ?",
            (model_id,),
        ).fetchone()
        if row and (row["is_default"] or row["is_default_ssh"]):
            raise ValueError("Cannot delete a model that is set as default")
        cur = conn.execute("DELETE FROM models_config WHERE id = ?", (model_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_default_model(model_id: str, mode: Optional[str] = None) -> None:
    """设置默认模型。
    mode 为 None 时设置全局 is_default；
    mode 为 'ssh' 时设置 SSH 模式的默认模型。
    """
    conn = _get_conn()
    try:
        if mode is None:
            conn.execute("UPDATE models_config SET is_default = 0")
            conn.execute("UPDATE models_config SET is_default = 1 WHERE id = ?", (model_id,))
        else:
            conn.execute("UPDATE models_config SET is_default_ssh = 0")
            conn.execute("UPDATE models_config SET is_default_ssh = 1 WHERE id = ?", (model_id,))
        conn.commit()
    finally:
        conn.close()


def get_default_model(mode: Optional[str] = None) -> Optional[dict]:
    """获取当前默认模型配置（api_key 已脱敏）。
    mode 为 None 时返回全局 is_default 模型；
    mode 为 'ssh' 时返回 SSH 模式的默认模型。
    """
    conn = _get_conn()
    try:
        if mode is None:
            row = conn.execute(
                "SELECT id, name, model_id, provider, base_url, api_key, "
                "supports_thinking, supports_image, supports_tool_calling, "
                "is_default, is_default_ssh, "
                "temperature, max_tokens, max_output_tokens, top_p "
                "FROM models_config WHERE is_default = 1 LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, model_id, provider, base_url, api_key, "
                "supports_thinking, supports_image, supports_tool_calling, "
                "is_default, is_default_ssh, "
                "temperature, max_tokens, max_output_tokens, top_p "
                "FROM models_config WHERE is_default_ssh = 1 LIMIT 1"
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        # C1 修复：脱敏 api_key，避免明文密钥泄露到前端
        result["api_key"] = _mask_api_key(result.get("api_key"))
        return result
    finally:
        conn.close()


# ── Agent Runs CRUD ──


def create_agent_run(run_id: str, conversation_id: str, task: str,
                     max_iterations: int = 10, mode: str = "",
                     model_used: str = "") -> None:
    """创建 Agent 运行记录"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, task, status, max_iterations, "
            "mode, model_used, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (run_id, conversation_id, task, max_iterations, mode, model_used, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def update_agent_run(run_id: str, status: str, iteration: Optional[int] = None,
                     state_history: str = None, total_tokens: int = 0,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     cost_usd: float = 0.0, duration_ms: int = 0,
                     tool_call_count: int = 0) -> None:
    """更新 Agent 运行状态（含 token/成本/耗时统计）

    C6 修复：所有可选字段用 `is not None` 判断，避免合法的 0 值被跳过。
    iteration 改为 Optional：仅在显式传入时写入，避免仅更新 status 时被重置为 0。
    """
    conn = _get_conn()
    try:
        sets = ["status = ?", "completed_at = ?"]
        params: list = [status, _now()]
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
        params.append(run_id)
        conn.execute(
            f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()
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


def get_agent_runs(conversation_id: str = None, limit: int = 20) -> list[dict]:
    """获取 Agent 运行记录列表"""
    conn = _get_conn()
    try:
        if conversation_id:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE conversation_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_agent_run_stats(mode: str = None, days: int = 7) -> dict:
    """
    获取 Agent 运行成本/耗时统计聚合

    Args:
        mode: 模式过滤（None=所有模式）
        days: 最近 N 天
    Returns:
        {
            "total_runs": int,
            "completed_runs": int,
            "failed_runs": int,
            "total_tokens": int,
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_cost_usd": float,
            "total_duration_ms": int,
            "total_tool_calls": int,
            "avg_iterations": float,
            "avg_cost_usd": float,
        }
    """
    conn = _get_conn()
    try:
        sql = (
            "SELECT "
            "  COUNT(*) as total_runs, "
            "  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed_runs, "
            "  SUM(CASE WHEN status IN ('failed','max_reached') THEN 1 ELSE 0 END) as failed_runs, "
            "  COALESCE(SUM(total_tokens), 0) as total_tokens, "
            "  COALESCE(SUM(prompt_tokens), 0) as prompt_tokens, "
            "  COALESCE(SUM(completion_tokens), 0) as completion_tokens, "
            "  COALESCE(SUM(cost_usd), 0) as total_cost_usd, "
            "  COALESCE(SUM(duration_ms), 0) as total_duration_ms, "
            "  COALESCE(SUM(tool_call_count), 0) as total_tool_calls, "
            "  COALESCE(AVG(iteration), 0) as avg_iterations, "
            "  COALESCE(AVG(cost_usd), 0) as avg_cost_usd "
            "FROM agent_runs "
            "WHERE started_at >= datetime('now', ?) "
        )
        params: list = [f"-{days} days"]
        if mode:
            sql += "AND mode = ? "
            params.append(mode)

        row = conn.execute(sql, params).fetchone()
        if not row:
            return {
                "total_runs": 0, "completed_runs": 0, "failed_runs": 0,
                "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_cost_usd": 0.0, "total_duration_ms": 0,
                "total_tool_calls": 0, "avg_iterations": 0.0, "avg_cost_usd": 0.0,
            }
        return dict(row)
    finally:
        conn.close()


# ── Tool Calls CRUD ──


def add_tool_call(agent_run_id: str, iteration: int, tool_name: str,
                  arguments: str, result: str = "", success: bool = True,
                  duration_ms: int = 0) -> str:
    """记录工具调用，返回生成的 tool_call_id（H10 修复）"""
    tool_call_id = _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO tool_calls (id, agent_run_id, iteration, tool_name, arguments, result, success, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_call_id, agent_run_id, iteration, tool_name, arguments, result,
             1 if success else 0, duration_ms, _now()),
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
    """获取某次 Agent 运行的所有工具调用"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tool_calls WHERE agent_run_id = ? ORDER BY created_at",
            (agent_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Agent 文件变更 CRUD ──


def save_agent_file_changes(agent_run_id: str, changes: list[dict]) -> None:
    """持久化一次 Agent 运行的文件变更记录（覆盖式写入，保证幂等）"""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM agent_file_changes WHERE agent_run_id = ?",
            (agent_run_id,),
        )
        for c in changes:
            conn.execute(
                "INSERT INTO agent_file_changes "
                "(id, agent_run_id, file_path, abs_path, operation, "
                " added_lines, removed_lines, before_size, after_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(),
                    agent_run_id,
                    c.get("file_path", ""),
                    c.get("abs_path", ""),
                    c.get("operation", "modify"),
                    c.get("added_lines", 0) or 0,
                    c.get("removed_lines", 0) or 0,
                    c.get("before_size", 0) or 0,
                    c.get("after_size", 0) or 0,
                    c.get("created_at") or _now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_agent_file_changes(agent_run_id: str) -> list[dict]:
    """获取某次 Agent 运行的文件变更记录（按路径排序）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_file_changes "
            "WHERE agent_run_id = ? AND accepted_at IS NULL "
            "ORDER BY file_path",
            (agent_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_agent_file_changes_summary(agent_run_id: str) -> dict:
    """获取某次 Agent 运行的文件变更汇总"""
    changes = get_agent_file_changes(agent_run_id)
    total_added = sum(c.get("added_lines", 0) or 0 for c in changes)
    total_removed = sum(c.get("removed_lines", 0) or 0 for c in changes)
    return {
        "files_changed": len(changes),
        "added_lines": total_added,
        "removed_lines": total_removed,
        "changes": changes,
    }


def accept_agent_file_changes(agent_run_id: str) -> None:
    """用户确认保留文件变更，标记 accepted_at"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_file_changes SET accepted_at = ? WHERE agent_run_id = ? AND accepted_at IS NULL",
            (_now(), agent_run_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_agent_file_changes(agent_run_id: str) -> None:
    """删除某次 Agent 运行的文件变更记录"""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM agent_file_changes WHERE agent_run_id = ?",
            (agent_run_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Agent 三层记忆 CRUD ──


def add_agent_memory(
    mode: str,
    mem_type: str,
    content: str,
    source_conv_id: Optional[str] = None,
    agent_run_id: Optional[str] = None,
    embedding: Optional[list] = None,
    importance: float = 0.5,
) -> str:
    """添加一条 Agent 记忆，返回新记忆 id"""
    mid = _new_id()
    now = _now()
    emb_json = json.dumps(embedding) if embedding else None
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_memories "
            "(id, type, mode, content, source_conv_id, agent_run_id, embedding, "
            " importance, last_used_at, use_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                mid, mem_type, mode, content,
                source_conv_id, agent_run_id, emb_json,
                importance, now, now,
            ),
        )
        conn.commit()
        return mid
    finally:
        conn.close()


def get_agent_memories(
    mode: Optional[str] = None,
    mem_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """获取 Agent 记忆列表（按 mode/type 过滤，按 last_used_at 倒序）"""
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if mem_type:
            conditions.append("type = ?")
            params.append(mem_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            "SELECT id, type, mode, content, source_conv_id, agent_run_id, "
            "       embedding, importance, last_used_at, use_count, created_at "
            f"FROM agent_memories {where} "
            "ORDER BY last_used_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def touch_agent_memory(memory_id: str) -> None:
    """更新记忆使用时间和计数"""
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_memories SET last_used_at = ?, use_count = use_count + 1 "
            "WHERE id = ?",
            (now, memory_id),
        )
        conn.commit()
    finally:
        conn.close()


def promote_hot_memories(
    mode: str,
    mem_type: str,
    min_use_count: int = 3,
    importance_delta: float = 0.05,
    max_importance: float = 1.0,
) -> int:
    """批量提升高频使用记忆的重要性，返回更新行数"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE agent_memories SET importance = MIN(?, importance + ?) "
            "WHERE mode = ? AND type = ? AND use_count >= ? "
            "AND importance < ?",
            (max_importance, importance_delta, mode, mem_type, min_use_count, max_importance),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_agent_memory(memory_id: str) -> bool:
    """删除单条 Agent 记忆"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM agent_memories WHERE id = ?", (memory_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def prune_agent_memories(
    mode: Optional[str] = None,
    mem_type: Optional[str] = None,
    max_count: int = 200,
) -> int:
    """删除最旧最少使用的 Agent 记忆，返回删除数量

    C4 修复：原 ORDER BY ASC + OFFSET 方向反了（保留最旧、删除最新）。
    改为 DESC 排序后 OFFSET 跳过最新 max_count 条，删除剩余旧行。
    """
    conn = _get_conn()
    try:
        sql = (
            "DELETE FROM agent_memories WHERE id IN ("
            "SELECT id FROM agent_memories "
        )
        conditions = []
        params: list = []
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if mem_type:
            conditions.append("type = ?")
            params.append(mem_type)
        if conditions:
            sql += "WHERE " + " AND ".join(conditions) + " "
        # C4 修复：DESC 排序，OFFSET 跳过最新 max_count 条
        sql += "ORDER BY last_used_at DESC, use_count DESC LIMIT -1 OFFSET ?)"
        params.append(max_count)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_all_memories(
    mode: Optional[str] = None,
    mem_type: Optional[str] = None,
) -> int:
    """删除所有匹配的 Agent 记忆，返回删除数量"""
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if mem_type:
            conditions.append("type = ?")
            params.append(mem_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur = conn.execute(f"DELETE FROM agent_memories {where}", params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── 应用设置 key-value CRUD ──


def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """读取单个应用设置"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_app_setting(key: str, value: str) -> None:
    """写入/更新应用设置"""
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
            (key, value, now, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_memory_settings() -> dict:
    """读取记忆相关设置，返回带默认值的 dict"""
    return {
        "enabled": get_app_setting("memory.enabled", "true") == "true",
        "strategy": get_app_setting("memory.strategy", "auto"),
        "max_items": int(get_app_setting("memory.max_items", "100") or "100"),
    }


# ── Agent 策略 CRUD（reflection / evolved_config）──


def add_agent_strategy(
    mode: str,
    strategy_type: str,
    name: str,
    content: str,
    config: Optional[dict] = None,
    score: float = 0.5,
    enabled: int = 1,
) -> str:
    """添加一条 Agent 策略，返回新策略 id"""
    sid = _new_id()
    now = _now()
    cfg_json = json.dumps(config) if config else None
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_strategies "
            "(id, mode, strategy_type, name, content, config, score, enabled, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, mode, strategy_type, name, content, cfg_json, score, enabled, now, now),
        )
        conn.commit()
        return sid
    finally:
        conn.close()


def get_agent_strategies(
    mode: Optional[str] = None,
    strategy_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """获取 Agent 策略列表"""
    conn = _get_conn()
    try:
        conditions = []
        params: list = []
        if mode:
            conditions.append("mode = ?")
            params.append(mode)
        if strategy_type:
            conditions.append("strategy_type = ?")
            params.append(strategy_type)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            "SELECT id, mode, strategy_type, name, content, config, score, enabled, "
            "       created_at, updated_at "
            f"FROM agent_strategies {where_clause} "
            "ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_agent_strategy(strategy_id: str) -> Optional[dict]:
    """获取单条 Agent 策略"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, mode, strategy_type, name, content, config, score, enabled, "
            "       created_at, updated_at "
            "FROM agent_strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_agent_strategy_enabled(strategy_id: str, enabled: int) -> bool:
    """启用/禁用 Agent 策略"""
    now = _now()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE agent_strategies SET enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, now, strategy_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_agent_strategy(strategy_id: str) -> bool:
    """删除单条 Agent 策略"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM agent_strategies WHERE id = ?", (strategy_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


