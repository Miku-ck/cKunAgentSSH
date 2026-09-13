"""
SQLite 数据库层 — 会话与消息持久化

连接管理：线程本地连接池。每个线程持有一个持久连接，复用避免反复
connect/close 的开销；close() 被包装为 rollback-and-keep-alive，
现有代码的 `finally: conn.close()` 模式无需任何改动。
"""
import asyncio
import sqlite3
import uuid
import json
import atexit
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_db_path: str = ""

# ── 线程本地连接池 ──
_thread_local = threading.local()
# 跟踪所有线程的连接，用于进程退出时统一清理。
# 注意：不能用 WeakSet —— sqlite3.Connection 在部分 Python 构建（如 3.13）下
# 不支持弱引用，WeakSet.add() 会抛 "cannot create weak reference"，导致后端起不来。
# 由于 _thread_local 已保证每线程仅一条连接，这里改用普通 set，数量受线程数约束，不会无界增长。
_all_connections: set = set()
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
            # 修复：记录未提交事务的回滚，便于排查数据丢失问题
            if self._conn.in_transaction:
                logger.debug("回滚未提交的事务（连接保持复用）")
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


# 连接空闲超过该秒数才做 SELECT 1 探活（审查 NIT：本地文件 SQLite 连接不会
# 像网络连接那样断开，原实现每次 _get_conn() 都跑一遍探活，给每条 SQL 白加
# 一次往返；30s 空闲阈值兼顾"长期空闲后校验"与热路径零开销）
_CONN_IDLE_PROBE_SECONDS = 30.0


def _get_conn() -> _PooledConnection:
    """获取当前线程的数据库连接（复用线程本地连接）。

    首次调用时创建底层连接并缓存到 threading.local()；
    后续调用直接返回包装后的连接。
    close() 被包装为 no-op，所以 `finally: conn.close()` 不会真正关闭连接。
    """
    import time as _time
    now = _time.monotonic()
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        last_used = getattr(_thread_local, "conn_last_used", 0.0)
        # 仅当连接空闲超过阈值时验证其有效性（热路径零探活开销）
        if now - last_used > _CONN_IDLE_PROBE_SECONDS:
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
        _thread_local.conn_last_used = now

    if conn is None:
        conn = _create_connection()
        with _conn_lock:
            _all_connections.add(conn)
        _thread_local.conn = conn
        _thread_local.conn_last_used = now

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
        _thread_local.conn_last_used = 0.0


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
                reasoning_tokens INTEGER DEFAULT 8192,  -- 推理/思考内容的独立 token 预算
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

            -- Agent 三层记忆（episodic=情景总结, semantic=知识事实+embedding）
            CREATE TABLE IF NOT EXISTS agent_memories (
                id              TEXT PRIMARY KEY,
                type            TEXT NOT NULL,           -- episodic / semantic
                mode            TEXT NOT NULL,           -- ssh（运维模式）
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

            -- 追加式、链式签名防篡改的运维审计表（对齐等保2.0 安全审计）
            CREATE TABLE IF NOT EXISTS audit_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                operator     TEXT NOT NULL DEFAULT '',
                target_type  TEXT NOT NULL DEFAULT '',
                target_id    TEXT NOT NULL DEFAULT '',
                environment  TEXT NOT NULL DEFAULT 'test',
                action       TEXT NOT NULL DEFAULT '',
                command_hash TEXT NOT NULL DEFAULT '',
                command_raw  TEXT NOT NULL DEFAULT '',
                result       TEXT NOT NULL DEFAULT '',
                conclusion   TEXT NOT NULL DEFAULT '',
                run_id       TEXT NOT NULL DEFAULT '',
                iteration    INTEGER NOT NULL DEFAULT 0,
                prev_hash    TEXT NOT NULL DEFAULT '',
                hash         TEXT NOT NULL UNIQUE,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_audit_ts     ON audit_log(ts);
            CREATE INDEX IF NOT EXISTS ix_audit_run    ON audit_log(run_id);
            CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_log(target_id);
            CREATE INDEX IF NOT EXISTS ix_audit_env    ON audit_log(environment);

            -- 定时任务（支持分钟间隔 / 每天 / 每周 / 每月）
            CREATE TABLE IF NOT EXISTS schedules (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                schedule_type   TEXT NOT NULL DEFAULT 'interval',
                interval_minutes INTEGER DEFAULT 60,
                time_hour       INTEGER DEFAULT 9,
                time_minute     INTEGER DEFAULT 0,
                weekday         INTEGER DEFAULT 0,
                day_of_month    INTEGER DEFAULT 1,
                target_host     TEXT DEFAULT '',
                task_prompt     TEXT DEFAULT '',
                enabled         INTEGER DEFAULT 1,
                next_run_at     TEXT,
                last_run_at     TEXT,
                last_status     TEXT DEFAULT 'idle',
                last_run_id     TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_schedules_enabled ON schedules(enabled, next_run_at);

            -- 定时任务执行历史
            CREATE TABLE IF NOT EXISTS schedule_runs (
                id          TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                run_id      TEXT DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'idle',
                summary     TEXT DEFAULT '',
                error       TEXT DEFAULT '',
                started_at  TEXT,
                finished_at TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_schedule_runs_sid ON schedule_runs(schedule_id, created_at);


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
        # 审查修复（P2 E9）：_seed_models（含同步 httpx.get）已移至独立异步入口
        # seed_models_async，从 lifespan 调用，避免阻塞事件循环
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

        # 幂等补齐所有查询依赖的列（与 CREATE TABLE 保持一致）。
        # 旧库可能缺少 reasoning_tokens/top_p/sort_order 等，若不补，
        # list_models_config / get_model_config 的 SELECT 会因 "no such column"
        # 抛 500，导致模型在前端下拉与设置面板均不显示。
        for col, col_def in {
            "name": "TEXT NOT NULL DEFAULT ''",
            "model_id": "TEXT",
            "provider": "TEXT NOT NULL DEFAULT 'custom'",
            "base_url": "TEXT NOT NULL DEFAULT ''",
            "api_key": "TEXT",
            "supports_thinking": "INTEGER DEFAULT 0",
            "supports_image": "INTEGER DEFAULT 0",
            "supports_tool_calling": "INTEGER DEFAULT 0",
            "is_default": "INTEGER DEFAULT 0",
            "is_default_ssh": "INTEGER DEFAULT 0",
            "temperature": "REAL DEFAULT 0.7",
            "max_tokens": "INTEGER DEFAULT 16384",
            "max_output_tokens": "INTEGER DEFAULT 4096",
            "reasoning_tokens": "INTEGER DEFAULT 8192",
            "top_p": "REAL DEFAULT 0.9",
            "sort_order": "INTEGER DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
        }.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE models_config ADD COLUMN {col} {col_def}")
                cols.add(col)


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


def _probe_first_model(base_url: str, timeout: float = 3.0) -> Optional[str]:
    """探测 OpenAI 兼容服务 /models 端点的第一个模型名。

    供首启模型自动种子使用：任何失败（不可达/超时/4xx/空列表/坏响应）
    都返回 None 并只写 warning 日志，静默降级为"不种子"，绝不阻塞启动。
    表仍为空时，下次 init_db（应用重启）会自动重试探测。
    """
    try:
        url = base_url.rstrip("/")
        if not url.endswith("/models"):
            url = f"{url}/models"
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        items = (resp.json() or {}).get("data") or []
        if items and items[0].get("id"):
            return str(items[0]["id"])
        logger.warning("模型自动探测: /models 返回空列表 %s", base_url)
    except Exception as e:
        logger.warning(
            "模型自动探测失败 %s: %s（未种子模型，可在设置中手动配置；"
            "服务启动后重启应用将自动重试）", base_url, e,
        )
    return None


async def seed_models_async(db_path: str) -> None:
    """异步模型种子 — 仅在 models_config 表为空且显式配置 VLLM_BASE_URL 时执行。

    审查修复（P2 E9）：_probe_first_model 内含同步 httpx.get（最长阻塞 3s×2=6s）。
    原实现从 async lifespan 直接调 init_db → _seed_models → _probe_first_model，
    6s 同步阻塞阻塞整个事件循环（所有 FastAPI 请求/后台任务/心跳全卡死）。
    改为独立异步入口：httpx 调用放 asyncio.to_thread 线程池，事件循环保持畅通。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM models_config").fetchone()
        if row["cnt"] > 0:
            return
        try:
            from config import settings
            base_url = (settings.vllm_base_url or "").strip()
            thinking_url = (settings.vllm_thinking_url or "").strip()
            api_key = settings.vllm_api_key or "EMPTY"
        except Exception:
            return
        if not base_url:
            return

        now = _now()
        # httpx.get 是同步阻塞调用，放线程池避免阻塞事件循环
        model_id = await asyncio.to_thread(_probe_first_model, base_url)
        if model_id:
            _insert_seed_model(
                conn, model_id=model_id, base_url=base_url, api_key=api_key,
                supports_thinking=False, is_default=True, sort_order=0, now=now,
            )
            logger.info("自动探测种子默认模型: %s @ %s", model_id, base_url)

        if thinking_url and thinking_url.rstrip("/") != base_url.rstrip("/"):
            t_model = await asyncio.to_thread(_probe_first_model, thinking_url)
            if t_model:
                _insert_seed_model(
                    conn, model_id=t_model, base_url=thinking_url, api_key=api_key,
                    supports_thinking=True, is_default=False, sort_order=1, now=now,
                )
                logger.info("自动探测种子思考模型: %s @ %s", t_model, thinking_url)
        conn.commit()
    finally:
        conn.close()


def _insert_seed_model(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    base_url: str,
    api_key: str,
    supports_thinking: bool,
    is_default: bool,
    sort_order: int,
    now: str,
) -> None:
    """插入一条首启种子模型配置（固定保守默认参数，用户可在 UI 调整）"""
    conn.execute(
        "INSERT INTO models_config "
        "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
        "is_default, is_default_ssh, "
        "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 0.7, 16384, 4096, 8192, 0.9, ?, ?, ?)",
        (
            model_id, model_id, model_id, "vllm",
            base_url, api_key, int(supports_thinking), int(is_default), int(is_default),
            sort_order, now, now,
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
        # F-4 修复：返回最新 N 条（按时间正序输出）。
        # 原 ORDER BY created_at ASC LIMIT ? 返回的是最旧 N 条，>50 条的会话
        # 重载后只显示最新消息之前的内容。内层子查询按 created_at DESC, rowid DESC
        # 取窗口（offset 语义保持为"从新到旧"窗口内偏移），外层再按时间正序输出。
        # C-review MINOR-3：同秒消息用 rowid（插入序）兜底 — id 是随机 uuid，
        # 按 id 排序不是时间序，同秒消息仍可能乱序。
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at "
            "FROM ("
            "  SELECT id, conversation_id, role, content, reasoning, model_used, tokens_used, run_id, agent_trace, created_at "
            "  FROM messages WHERE conversation_id = ? "
            "  ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
            ") ORDER BY created_at ASC, rowid ASC",
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


# ── 记忆 CRUD ──


def get_relevant_memories(mode: str, limit: int = 5) -> list[dict]:
    """获取某模式下最相关的记忆（按最近使用 + 使用次数排序）

    C-review M2：原实现读旧表 `memories`，但该表无任何写入者（全库无
    INSERT INTO memories），永远返回空 → 记忆注入形同虚设。真正的语义记忆
    由 post_processor → mem_mgr.save_semantic → add_agent_memory 写入
    `agent_memories`（type='semantic'）。改为读该表 semantic 层，字段形状
    与旧返回保持一致（调用方仅用 content）。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, mode, content, source_conv_id, last_used_at, use_count, created_at "
            "FROM agent_memories WHERE mode = ? AND type = 'semantic' "
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


def count_skills_by_source(source: str) -> int:
    """统计指定来源的技能数（审查：替代 list_skills(limit=9999) 全量加载只计数）"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM skills WHERE source = ?",
            (source,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
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
            "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at "
            "FROM models_config ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
        result = [dict(r) for r in rows]
        for item in result:
            item["api_key"] = _mask_api_key(item.get("api_key"))
        return result
    finally:
        conn.close()


def get_model_config(model_id: str, mask_key: bool = True) -> Optional[dict]:
    """根据 id 获取模型配置（mask_key=True 时 api_key 已脱敏）

    审查外热修：调用方可能传入行主键 id（model-<uuid>）或 LLM 模型名
    （model_id 列，如 qwen3.8-27b）— Agent 后处理原样传模型名、chat/agent
    的默认模型兜底也优先取模型名，导致按 id 查询落空、记忆/标题/技能提取
    被静默跳过（日志 unknown model_id 告警）。现先按行 id 查，落空再按
    model_id 列回退（重名取 rowid 最小的第一行）。
    """
    if not model_id:
        return None
    _MC_COLS = (
        "id, name, model_id, provider, base_url, api_key, "
        "supports_thinking, supports_image, supports_tool_calling, "
        "is_default, is_default_ssh, "
        "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at"
    )
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_MC_COLS} FROM models_config WHERE id = ?",
            (model_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_MC_COLS} FROM models_config WHERE model_id = ? ORDER BY rowid LIMIT 1",
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
    is_default_work: bool = False,   # 预留：Work 模式默认标记（当前 DB 无对应列，不参与 INSERT）
    is_default_ide: bool = False,    # 预留：IDE 模式默认标记（当前 DB 无对应列，不参与 INSERT）
    is_default_ssh: bool = False,
    reasoning_tokens: int = 8192,
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
            "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid, name, model_id, provider, base_url, api_key,
                1 if supports_thinking else 0,
                1 if supports_image else 0,
                1 if supports_tool_calling else 0,
                1 if is_default_ssh else 0,
                temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, next_order, now, now,
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
        "temperature", "max_tokens", "max_output_tokens", "reasoning_tokens", "top_p", "sort_order",
        "is_default_ssh",
    }
    sets = []
    params = []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            # api_key 空字符串表示「保留原值」，跳过更新
            if k == "api_key" and v == "":
                continue
            # B-10 修复：前端回传的掩码值（含 "****"）不是真密钥 —
            # 原实现会把掩码写回 DB 覆盖真实 api_key，导致模型 401
            if k == "api_key" and "****" in str(v):
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

    审查修复：原实现"全清 + 置一"为两条非原子 UPDATE，并发切换可产生
    0 个或 2 个默认模型（两条语句间交错）。改为单条条件 UPDATE
    （清 others 带 WHERE id != 目标后提交同一事务），消除中间状态。
    """
    conn = _get_conn()
    try:
        if mode is None:
            col = "is_default"
        else:
            col = "is_default_ssh"
        with conn:  # 单事务：BEGIN → 两条 UPDATE → commit/rollback
            conn.execute(
                f"UPDATE models_config SET {col} = 0 WHERE id != ?", (model_id,)
            )
            conn.execute(
                f"UPDATE models_config SET {col} = 1 WHERE id = ?", (model_id,)
            )
    finally:
        conn.close()


def get_default_model(mode: Optional[str] = None, mask_key: bool = True) -> Optional[dict]:
    """获取当前默认模型配置。
    mode 为 None 时返回全局 is_default 模型；
    mode 为 'ssh' 时返回 SSH 模式的默认模型。
    mask_key=True（默认）时 api_key 脱敏，面向前端/只取 id 的场景；
    内部真正调用 LLM/embedding 的场景传 mask_key=False 获取真实 key。
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
        # C1 修复：默认脱敏 api_key，避免明文密钥泄露到前端
        # 审查 M-15：内部需要真实 key 的调用方（embedding/LLM）传 mask_key=False，
        # 否则拿到 "EMPT****" 式脱敏值 → 401 → 静默降级伪 embedding → 记忆维度混存
        result["api_key"] = result.get("api_key") if not mask_key else _mask_api_key(result.get("api_key"))
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
    审查 NIT：completed_at 仅在终态（completed/failed/human_help）时写入，
    原实现每次更新（含中间状态）都刷新 completed_at，时间字段失真。
    """
    # B-7 修复：max_reached 同为终态（contracts.State.MAX_REACHED / reflection /
    # 统计查询 database.py get_agent_run_stats 均视其为终态），completed_at 需一并写入
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
        days: 最近 N 天（1-365，默认 7）
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
    # 修复：校验 days 参数范围，避免负数或极大值导致语义异常
    days = max(1, min(int(days) if days else 7, 365))
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


# ── 定时任务（schedules） ───────────────────────────────────────────────────────

_SCHEDULE_COLS = (
    "id, name, schedule_type, interval_minutes, time_hour, time_minute, "
    "weekday, day_of_month, target_host, task_prompt, enabled, next_run_at, "
    "last_run_at, last_status, last_run_id, created_at, updated_at"
)


def list_schedules() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_schedule(schedule_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_enabled_schedules() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_schedule(data: dict) -> dict:
    now = _now()
    sid = data.get("id") or _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO schedules (
                id, name, schedule_type, interval_minutes, time_hour, time_minute,
                weekday, day_of_month, target_host, task_prompt, enabled,
                next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                (data.get("name") or "").strip(),
                data.get("schedule_type") or "interval",
                int(data.get("interval_minutes") or 60),
                int(data.get("time_hour") or 9),
                int(data.get("time_minute") or 0),
                int(data.get("weekday") or 0),
                int(data.get("day_of_month") or 1),
                data.get("target_host") or "",
                data.get("task_prompt") or "",
                1 if data.get("enabled", True) else 0,
                data.get("next_run_at"),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_schedule(sid)


def update_schedule(schedule_id: str, fields: dict) -> Optional[dict]:
    now = _now()
    allowed = {
        "name", "schedule_type", "interval_minutes", "time_hour", "time_minute",
        "weekday", "day_of_month", "target_host", "task_prompt", "enabled",
    }
    cols, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        cols.append(f"{k} = ?")
        vals.append(v)
    if not cols:
        return get_schedule(schedule_id)
    cols.append("updated_at = ?")
    vals.append(now)
    vals.append(schedule_id)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE schedules SET {', '.join(cols)} WHERE id = ?", vals
        )
        conn.commit()
    finally:
        conn.close()
    return get_schedule(schedule_id)


def set_schedule_next_run(schedule_id: str, next_run_at: Optional[str]) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, _now(), schedule_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_schedule_run(
    schedule_id: str,
    status: str,
    last_run_at: Optional[str] = None,
    last_run_id: str = "",
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE schedules SET last_status = ?, updated_at = ?
               WHERE id = ?""",
            (status, _now(), schedule_id),
        )
        if last_run_at:
            conn.execute(
                "UPDATE schedules SET last_run_at = ? WHERE id = ?",
                (last_run_at, schedule_id),
            )
        if last_run_id:
            conn.execute(
                "UPDATE schedules SET last_run_id = ? WHERE id = ?",
                (last_run_id, schedule_id),
            )
        conn.commit()
    finally:
        conn.close()


def delete_schedule(schedule_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── 定时任务执行历史（schedule_runs） ───────────────────────────────────────────

def create_schedule_run(schedule_id: str, run_id: str) -> dict:
    rid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO schedule_runs (id, schedule_id, run_id, status, started_at, created_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (rid, schedule_id, run_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": rid, "schedule_id": schedule_id, "run_id": run_id,
        "status": "running", "summary": "", "error": "",
        "started_at": now, "finished_at": None, "created_at": now,
    }


def update_schedule_run(
    run_key: str,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """run_key 既可能是 schedule_runs.id 也可能是 harness run_id"""
    cols, vals = [], []
    if status is not None:
        cols.append("status = ?")
        vals.append(status)
        if status in ("completed", "failed"):
            cols.append("finished_at = ?")
            vals.append(_now())
    if summary is not None:
        cols.append("summary = ?")
        vals.append(summary)
    if error is not None:
        cols.append("error = ?")
        vals.append(error)
    if not cols:
        return
    vals.append(run_key)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE schedule_runs SET {', '.join(cols)} WHERE id = ? OR run_id = ?",
            vals + [run_key],
        )
        conn.commit()
    finally:
        conn.close()


def list_schedule_runs(schedule_id: str, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, schedule_id, run_id, status, summary, error,
                      started_at, finished_at, created_at
               FROM schedule_runs WHERE schedule_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (schedule_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


