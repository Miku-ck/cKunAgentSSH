"""
SQLite 数据库层 — 会话与消息持久化

连接管理：线程本地连接池。每个线程持有一个持久连接，复用避免反复
connect/close 的开销；close() 被包装为 rollback-and-keep-alive，
现有代码的 `finally: conn.close()` 模式无需任何改动。
"""
import asyncio
import base64
import re
import sqlite3
import sys
import uuid
import json
import atexit
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_db_path: str = ""

# SQL 标识符守卫（CWE-89 加固）：凡是拼进 f-string SQL 的表名/列名，必须是
# 裸标识符。这些标识符全部来自本模块的字面量常量（不接受任何外部入参），
# 校验是防御性的 —— 字面量手滑改出空格/引号/分号时当场抛 RuntimeError，
# 而不是静默生成一条语义被改写的 SQL。
_SQL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _assert_sql_ident(name: str) -> None:
    if not _SQL_IDENT_RE.fullmatch(name):
        raise RuntimeError(f"拒绝拼接非法 SQL 标识符: {name!r}")

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
        except Exception as e:
            # 连接可能已损坏，下次 _get_conn() 会重建。此处不能静默吞掉：
            # 这是"有事务没能回滚"的唯一可见信号，pass 会让数据丢失无从排查。
            logger.warning("回滚未提交事务失败（连接将重建）: %s", e)


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
                    except Exception as e:
                        # 探活失败已经证明这条连接坏掉了，close 再失败只是同一故障的
                        # 第二次回响；但泄漏的文件句柄只有这里能看到，留一条 warning
                        logger.warning("关闭失效连接失败（句柄可能泄漏）: %s", e)
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
                context_summary TEXT,
                undo_recorded   INTEGER DEFAULT 0,
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

            -- v1 三层记忆表（spec §10：迁移源，v2 运行时只读不写，去留待定）
            -- 迁移路径见 _migrate_memory_v1_to_v2；写入侧已全部改走 v2 `memories`
            CREATE TABLE IF NOT EXISTS agent_memories (
                id              TEXT PRIMARY KEY,
                type            TEXT NOT NULL,           -- episodic / semantic
                mode            TEXT NOT NULL,           -- ssh（运维模式）
                content         TEXT NOT NULL,
                source_conv_id  TEXT,
                agent_run_id    TEXT,
                embedding       TEXT,                    -- JSON list[float]，仅 semantic
                embedding_src   TEXT,                    -- 向量来源指纹（P4），仅 semantic
                importance      REAL DEFAULT 0.5,
                last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- 语义是"写入时间"，命中不再刷新（自增强回路教训）
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
                -- spec 2026-10-27-schedule-task-type：任务分类，'ops' | 'daily'，存量归运维
                task_type       TEXT NOT NULL DEFAULT 'ops',
                enabled         INTEGER DEFAULT 1,
                next_run_at     TEXT,
                last_run_at     TEXT,
                last_status     TEXT DEFAULT 'idle',
                last_run_id     TEXT,
                last_abnormal_at TEXT,
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

            -- 远程后台任务注册表（job_run target=remote）：进程/日志句柄持久化，
            -- backend 重启后 job_list 可恢复、对账与 kill
            CREATE TABLE IF NOT EXISTS agent_jobs (
                job_id      TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL DEFAULT '',
                session_id  TEXT NOT NULL DEFAULT '',
                cmd         TEXT NOT NULL DEFAULT '',
                pid         INTEGER,
                log_dir     TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'running',
                exit_code   INTEGER,
                out_offset  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_agent_jobs_status ON agent_jobs(status);
            CREATE INDEX IF NOT EXISTS ix_agent_jobs_run ON agent_jobs(run_id);

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
            ("undo_recorded",     "INTEGER DEFAULT 0"),
        ]:
            if col not in existing_cols:
                # {col} 拼进 DDL 前过标识符守卫（见 _assert_sql_ident）；
                # {col_def} 是同一份字面量元组里的 DDL 片段，不含任何外部输入
                _assert_sql_ident(col)
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} {col_def}")
        # agent_jobs 表：旧库补 out_offset（job_output 增量读日志的字节偏移，
        # 重启后恢复注册表时用它避免从头重读已见日志）
        cur = conn.execute("PRAGMA table_info(agent_jobs)")
        job_cols = {row[1] for row in cur.fetchall()}
        if "out_offset" not in job_cols:
            conn.execute(
                "ALTER TABLE agent_jobs ADD COLUMN out_offset INTEGER NOT NULL DEFAULT 0"
            )
        _migrate_db(conn)
        # 审查修复（P2 E9）：_seed_models（含同步 httpx.get）已移至独立异步入口
        # seed_models_async，从 lifespan 调用，避免阻塞事件循环
        conn.commit()
    finally:
        conn.close()


def _migrate_db(conn: sqlite3.Connection) -> None:
    """数据库迁移：为旧表添加新字段"""
    # schedules.last_abnormal_at：巡检异常沉淀时间（方案 Phase 3），正常巡检不动它。
    # 新库建表已含该列，这里只为旧库补齐；PRAGMA 探测避免重复 ALTER 报错。
    cursor = conn.execute("PRAGMA table_info(schedules)")
    schedule_cols = {row[1] for row in cursor.fetchall()}
    if "last_abnormal_at" not in schedule_cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN last_abnormal_at TEXT")
    # spec 2026-10-27-schedule-task-type：旧库补 task_type 列，DEFAULT 'ops' 使存量行自动归运维
    if "task_type" not in schedule_cols:
        conn.execute(
            "ALTER TABLE schedules ADD COLUMN task_type TEXT NOT NULL DEFAULT 'ops'"
        )
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

    # P4：embedding 向量来源标记（"api:<model_id>" 或 "local-<dim>"），
    # 跨来源的向量不在同一空间，检索/去重前据此互斥比较
    cursor = conn.execute("PRAGMA table_info(agent_memories)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'embedding_src' not in columns:
        conn.execute("ALTER TABLE agent_memories ADD COLUMN embedding_src TEXT")

    # spec §8：run 内滚动摘要落库 —— 旧库补列，否则 update_agent_run 写它时
    # "no such column"，整条 done 分支的统计落库一起挂掉
    cursor = conn.execute("PRAGMA table_info(agent_runs)")
    _run_cols = {row[1] for row in cursor.fetchall()}
    if 'context_summary' not in _run_cols:
        conn.execute("ALTER TABLE agent_runs ADD COLUMN context_summary TEXT")

    # v2 记忆子系统（spec §4/§7）：建表 + 一次性存量迁移，幂等由 app_settings 标记控制
    _init_memory_v2(conn)

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
                # {col} 拼进 DDL 前过标识符守卫（见 _assert_sql_ident）；
                # {col_def} 是上面同一份字面量字典里的 DDL 片段，不含任何外部输入
                _assert_sql_ident(col)
                conn.execute(f"ALTER TABLE models_config ADD COLUMN {col} {col_def}")
                cols.add(col)

        # 修正 name 与 model_id 的值（它们可能正好反了）。
        # 必须在上面那段补列之后：这个函数自己 SELECT id, name, model_id，
        # 而 name 正是补列循环里的一项 —— 原先它在循环之前跑，一个没有 name 列的
        # 旧库会在 init_db 里抛 OperationalError，而 init_db 只有 try/finally、
        # main.py 也不接 → 后端直接起不来（不是某个接口 500，是整个服务不启动）。
        _fix_model_name_and_id(conn)


def _init_memory_v2(conn: sqlite3.Connection) -> None:
    """v2 记忆 schema 建表 + FTS5 侧表 + 一次性存量迁移（spec §4 / §7）。

    幂等的前提是「IF NOT EXISTS 跳过 = 已经是我要的形状」，而 `memories` 这个表名
    v1 三层记忆用过、列完全不同，所以先验表形状再建表（见下）。迁移以
    app_settings('memory_v2.migrated') 为闸门；旧表被改名搁置的那一次，标记会被清掉
    重跑整批 —— 缺列的表上迁移不可能成功过，而失败路径也置了标记，不清就等于把
    存量永久锁在 agent_memories 里。任何异常只降级（记日志、置标记跳过），不拖垮
    应用启动 —— v2 表缺失时 mstore 调用方（retriever/tick/routes）各自有 except 兜底。
    """
    import struct
    logger = logging.getLogger(__name__)

    # v1 三层记忆占用的正是 `memories` 这个名字（列为 id/mode/content/…，没有 kind），
    # 而 CREATE TABLE IF NOT EXISTS 遇到同名旧表会静默跳过 —— 缺的是整张表的列，不是
    # 索引：此后每一条 v2 读写都抛 "no such column: kind"，v2 记忆在老库上整体失效。
    # 旧表改名保留（一行不删），随后建真正的 v2 表；标记清见下方迁移闸门。
    legacy_cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    revived_legacy = bool(legacy_cols) and "kind" not in legacy_cols
    if revived_legacy:
        conn.execute("ALTER TABLE memories RENAME TO memories_v1_layers")
        logger.warning(
            "memories 是 v1 三层记忆的旧表（缺 kind 等 v2 列，%d 行），已改名为 "
            "memories_v1_layers 原样保留，重建 v2 表",
            conn.execute("SELECT COUNT(*) FROM memories_v1_layers").fetchone()[0],
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            importance REAL NOT NULL DEFAULT 0.5,
            pinned INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0,
            valid_at TEXT,
            invalid_at TEXT,
            superseded_by TEXT,
            last_used_at TEXT,
            embedding BLOB,
            embedding_src TEXT,
            source_conv_id TEXT,
            agent_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # 索引建不上只是形状不符的症状，上面的闸门已经处理根因；这里仍兜一层 ——
    # 半截迁移留下的第三种形状不该让整个 init_db 崩。
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind_status ON memories(kind, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope_status ON memories(scope, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at)")
    except sqlite3.OperationalError as e:
        logger.error("memories 表形状异常，v2 索引未建: %s", e)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            job_id TEXT,
            action TEXT NOT NULL,
            old_content TEXT,
            new_content TEXT,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_blocks (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            limit_chars INTEGER NOT NULL DEFAULT 2000,
            version INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_block_history (
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            value TEXT NOT NULL,
            updated_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mbh_key ON memory_block_history(key, version DESC)"
    )

    # FTS5 侧表：内容由写入侧重分词（tokenize 空格/2-gram），故表只需 mid+tok。
    # 不支持 FTS5 的旧 SQLite 走 except，memory_v2.fts='0' 让 mstore 退回词重叠。
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(mid UNINDEXED, tok)")
        conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('memory_v2.fts','1')")
    except sqlite3.OperationalError as e:
        logger.warning("FTS5 不可用，关键词检索退回词重叠: %s", e)
        conn.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('memory_v2.fts','0')")

    if revived_legacy:
        # 旧 memories 表被改名搁置后，它留下的 FTS 行指向的 mid 已经不存在（或被
        # 下面的重迁移复用），全部清掉由重迁移重建。放在建表之后：老库里这张侧表
        # 可能根本还没建，提前 DELETE 会抛错。
        # 闸门读 memory_v2.fts 而不是探 sqlite_master：这个标记就是上面 try/except
        # 当场写的，与侧表是否真存在同源（迁移侧 _migrate_memory_v1_to_v2 读的也是它）。
        # FTS5 不可用时表从未建起来，无条件 DELETE 抛 "no such table" 且不落在任何
        # except 里 —— 它会一路冲出 init_db，把本函数「只降级不崩」的承诺变成后端
        # 整个起不来。表不存在就没有残留行可清，跳过即正确。
        fts_flag = conn.execute(
            "SELECT value FROM app_settings WHERE key='memory_v2.fts'"
        ).fetchone()
        if fts_flag and fts_flag[0] == "1":
            conn.execute("DELETE FROM memory_fts")

    conn.commit()

    row = conn.execute(
        "SELECT value FROM app_settings WHERE key='memory_v2.migrated'"
    ).fetchone()
    if revived_legacy:
        # 缺列的旧表上这次迁移不可能成功过（INSERT 直接找不到 kind 列），而下面的
        # 失败路径照样把标记置成 1 —— 存量就一直躺在 agent_memories 里没搬。清掉
        # 标记重跑整批：本次是真表重建后的第一次，没有"半截迁移"可踩。
        conn.execute("DELETE FROM app_settings WHERE key='memory_v2.migrated'")
    elif row and row[0] == "1":
        return
    try:
        _migrate_memory_v1_to_v2(conn, struct)
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key,value) VALUES('memory_v2.migrated','1')"
        )
        conn.commit()
        logger.info("memory v2: 存量迁移完成")
    except Exception as e:
        conn.rollback()
        # 失败也置标记：重跑半截迁移比重跑整批更危险（部分搬运的 embedding 无法分辨），
        # 存量数据仍在 agent_memories 里，v2 从零开始积累是安全状态。
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key,value) VALUES('memory_v2.migrated','1')"
        )
        conn.commit()
        logger.warning("memory v2 存量迁移失败（v2 从零积累，旧表保留）: %s", e)


def _migrate_memory_v1_to_v2(conn: sqlite3.Connection, struct_mod) -> None:
    """agent_memories / agent_strategies(reflection) → memories（spec §7）。

    - episodic 是事实记录不需对账 → active；semantic 全部 pending_reconcile，
      由 backfill_reconcile job 逐批裁决（矛盾/重复在首次整理时被消解）。
    - 旧 embedding 仅 'api:' 来源搬运为 float32 BLOB；local 伪向量一律丢弃
      （它们与真实向量不同空间，正是 v1 误注入源头）。
    - 旧 mode 列丢弃：v2 用 scope 表达归属，历史行统一 global。
    """
    now = _now()
    batch_ids: list[str] = []

    # 侧表由 mstore 的写入路径维护，而这里是直连批量 INSERT —— 不同步就会搬进一批
    # 「面板里列得出、检索搜不到」的记忆（词法路只查 memory_fts，装了 sqlite-vec 时
    # 向量路只查 memories_vec）。分词口径与 mstore._fts_sync 同源，故复用同一函数。
    from agent.memory.text import tokenize
    from agent.memory import vec as _v
    fts_row = conn.execute("SELECT value FROM app_settings WHERE key='memory_v2.fts'").fetchone()
    fts_on = bool(fts_row) and fts_row[0] == "1"

    def _index_side(mid: str, title: str, content: str, blob) -> None:
        if fts_on:
            conn.execute("DELETE FROM memory_fts WHERE mid=?", (mid,))
            toks = " ".join(tokenize(f"{title} {content}"))
            if toks:
                conn.execute("INSERT INTO memory_fts(mid, tok) VALUES(?,?)", (mid, toks))
        if blob is None:
            return
        dim = len(blob) // 4
        if _v.vec_available(conn) and _v.ensure_vec_table(conn, dim):
            _v.vec_upsert(conn, mid, blob, dim)

    rows = conn.execute(
        "SELECT id, type, content, embedding, embedding_src, importance, "
        "source_conv_id, agent_run_id, use_count, last_used_at, created_at "
        "FROM agent_memories"
    ).fetchall()
    for r in rows:
        mem_type = r["type"] if r["type"] in ("semantic", "episodic", "procedural") else "semantic"
        status = "active" if mem_type == "episodic" else "pending_reconcile"
        blob_src = None
        if (r["embedding_src"] or "").startswith("api:") and r["embedding"]:
            try:
                vec = json.loads(r["embedding"])
                if isinstance(vec, list) and vec and all(isinstance(x, (int, float)) for x in vec):
                    blob_src = (struct_mod.pack(f"{len(vec)}f", *vec), r["embedding_src"])
            except (ValueError, TypeError):
                blob_src = None
        blob, emb_src = blob_src if blob_src else (None, None)
        created = r["created_at"] or now
        conn.execute(
            "INSERT OR IGNORE INTO memories "
            "(id,kind,scope,title,content,source,status,importance,pinned,use_count,"
            "valid_at,last_used_at,embedding,embedding_src,source_conv_id,agent_run_id,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"], mem_type, "global", "", r["content"], "migration", status,
                r["importance"] if r["importance"] is not None else 0.5,
                0, r["use_count"] or 0, created, r["last_used_at"],
                blob, emb_src, r["source_conv_id"], r["agent_run_id"],
                created, now,
            ),
        )
        if status == "pending_reconcile":
            batch_ids.append(r["id"])
        _index_side(r["id"], "", r["content"], blob)

    # reflection 策略 → kind=procedural（spec §2/§7）：直接 active，无需对账
    try:
        srows = conn.execute(
            "SELECT id, name, content, score, created_at FROM agent_strategies "
            "WHERE strategy_type='reflection'"
        ).fetchall()
    except sqlite3.OperationalError:
        srows = []
    for s in srows:
        body = f"{s['name']}: {s['content']}" if s["name"] else s["content"]
        created = s["created_at"] or now
        sid = f"mig-{s['id']}"
        title = (s["name"] or "")[:60]
        conn.execute(
            "INSERT OR IGNORE INTO memories "
            "(id,kind,scope,title,content,source,status,importance,pinned,use_count,"
            "valid_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, "procedural", "global", title, body,
                "migration", "active",
                s["score"] if s["score"] is not None else 0.7,
                0, 0, created, created, now,
            ),
        )
        _index_side(sid, title, body, None)

    # 每 10 条 pending 投一个 backfill_reconcile job（priority=7 高于普通整理）
    # OR IGNORE：job id 按批次下标生成，重跑必须撞上同 id 而不炸整批迁移。
    for i in range(0, len(batch_ids), 10):
        conn.execute(
            "INSERT OR IGNORE INTO memory_jobs(id,kind,payload,priority) VALUES (?,?,?,?)",
            (
                f"mj-mig-{i}", "backfill_reconcile",
                json.dumps({"ids": batch_ids[i:i + 10]}), 7,
            ),
        )


def _fix_model_name_and_id(conn: sqlite3.Connection) -> None:
    """修正 models_config 表中 name 与 model_id 的值（仅当确实反了时）—— 一次性迁移。

    S10-08 修复：用 app_settings('models.name_id_fixed') 做闸门，每个库只跑一次。
    原实现每次 init_db 都无条件执行，而它的判据是纯形状猜测（"name 里有 / 就
    当成 model_id"），于是任何合法显示名都会被翻：用户把模型改名成
    "Qwen/2.5-Coder（本地）" 这类带斜杠的写法，下次启动 name 与 model_id 就互换
    了 —— 更糟的是互换后发往服务端的 model 字段变成那句显示名，请求直接 4xx，
    而用户看到的是"我没动过它，重启就坏了"。用户手动改回来，下次启动再翻一遍。

    它本质是修早期种子写入顺序错误的一次性数据修复，跑完就该永久退休。
    与 _init_memory_v2 的 memory_v2.migrated 同一范式。
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key='models.name_id_fixed'"
    ).fetchone()
    if row and row[0] == "1":
        return

    rows = conn.execute("SELECT id, name, model_id FROM models_config").fetchall()
    swapped = 0
    for r in rows:
        n, mid = r["name"], r["model_id"]
        if not n or not mid:
            continue
        # 仅当 name 看起来像 model_id（含 / 或匹配 Qwen/ 前缀），且 model_id 看起来像显示名时才互换
        def _looks_like_id(s: str) -> bool:
            return bool(s) and ("/" in s or s.startswith("Qwen/") or s.startswith("qwen/"))
        if _looks_like_id(n) and not _looks_like_id(mid):
            conn.execute(
                "UPDATE models_config SET name = ?, model_id = ?, updated_at = ? WHERE id = ?",
                (mid, n, _now(), r["id"])
            )
            swapped += 1
    # 置标记：这次跑过（哪怕一行都没改）就再也不跑。闸门放在猜测之后而不是之前，
    # 老库升级后仍能补上那一次真正的修复。
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES('models.name_id_fixed','1')"
    )
    if swapped:
        logger.info("models_config: 一次性修正 name/model_id 互换 %d 行", swapped)


# ARCH-002 环解除：_probe_first_model 依赖 netconfig（new_sync_client/redact_*），
# 而 netconfig 启动回读 HTTP 策略时又要 import database —— 函数体搬到独立叶子
# 模块 model_probe（见该文件 docstring）。此处顶层 re-export 保持既有调用点
# （_seed_models 内两处）与测试的 db._probe_first_model 引用/monkeypatch 目标不变；
# 本模块不再出现任何 netconfig 依赖。
from model_probe import _probe_first_model


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
        except Exception as e:
            # 静默 return 会让"首启没有预填模型"变成一个没有答案的问题：
            # 用户只看到空模型列表，而真正的失败原因（配置导入炸了）无处可查。
            logger.warning("首启自动探测：读取 config.settings 失败，跳过种子模型: %s", e)
            return
        if not base_url:
            return

        now = _now()
        # httpx.get 是同步阻塞调用，放线程池避免阻塞事件循环
        # 审查R2修复：两条种子各自独立 try + 即时 commit —— 原实现整批一条
        # commit，两条种子撞上同名模型（models_config.id 即模型名）抛
        # IntegrityError 时，连接关闭回滚把先一条已成功的种子连坐抹掉。
        # 现在单条冲突只 warning 跳过，不牵连另一条。
        model_id = await asyncio.to_thread(_probe_first_model, base_url, api_key)
        if model_id:
            try:
                _insert_seed_model(
                    conn, model_id=model_id, base_url=base_url, api_key=api_key,
                    supports_thinking=False, is_default=True, sort_order=0, now=now,
                )
                conn.commit()
                logger.info("自动探测种子默认模型: %s @ %s", model_id, base_url)
            except sqlite3.Error as e:
                conn.rollback()
                logger.warning("种子默认模型写入失败（跳过，不影响另一条种子）: %s @ %s: %s",
                               model_id, base_url, e)

        if thinking_url and thinking_url.rstrip("/") != base_url.rstrip("/"):
            t_model = await asyncio.to_thread(_probe_first_model, thinking_url, api_key)
            if t_model:
                try:
                    _insert_seed_model(
                        conn, model_id=t_model, base_url=thinking_url, api_key=api_key,
                        supports_thinking=True, is_default=False, sort_order=1, now=now,
                    )
                    conn.commit()
                    logger.info("自动探测种子思考模型: %s @ %s", t_model, thinking_url)
                except sqlite3.Error as e:
                    conn.rollback()
                    logger.warning("种子思考模型写入失败（跳过，不影响另一条种子）: %s @ %s: %s",
                                   t_model, thinking_url, e)
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
    # env 注入的种子 api_key 与 UI 写入同口径加信封（见 protect_secret 区块注释）
    api_key = protect_secret(api_key)
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


# ── 技能 CRUD ──


# skills 全列（四处查询 + 一处 INSERT 共用）。与 _MC_COLS/_SCHEDULE_COLS 同族：
# 各处各抄一份字面量时，加一列只会改到其中几处，其余读法永久拿不到那一列。
_SKILLS_COLS = (
    "id, name, slug, description, category, system_prompt, source, "
    "is_builtin, enabled, priority, trigger_words, created_at, updated_at"
)


def list_skills(limit: int = 200, offset: int = 0) -> list[dict]:
    """获取所有技能列表

    updated_at 只到秒，同批导入/批量启停的技能会整片并列；带 id 兜底键才让
    LIMIT/OFFSET 翻页可重复（否则同一条技能印在两页上、另一条翻不到）。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SKILLS_COLS} "
            "FROM skills ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
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
            f"SELECT {_SKILLS_COLS} "
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
            f"SELECT {_SKILLS_COLS} "
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
            f"SELECT {_SKILLS_COLS} "
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
            f"INSERT INTO skills ({_SKILLS_COLS}) "
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
    enabled_on_insert: int = 0,
) -> dict:
    """同步内置技能到数据库（按 slug + is_builtin=1 upsert）

    与 create_skill 的区别：slug 冲突时更新而非追加后缀，
    避免每次同步都新增重复记录。保留用户已设置的 enabled 状态；
    enabled_on_insert 仅控制首次插入的默认启用值（包内运维技能 = 1 开箱可用）。
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
                "VALUES (?, ?, ?, ?, ?, ?, 'builtin', 1, ?, 0, ?, ?, ?)",
                (sid, name, slug, description, category, system_prompt, enabled_on_insert, trigger_words_json, now, now),
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
        # skill_mode_config 没有指向 skills 的外键，不显式清就会每次删除残留一行
        # （GET /skills 与每次按 mode 过滤都全表扫它）
        conn.execute(
            "DELETE FROM skill_mode_config WHERE skill_id IN "
            "(SELECT id FROM skills WHERE slug = ? AND is_builtin = 1)",
            (slug,),
        )
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
        if cur.rowcount > 0:
            conn.execute("DELETE FROM skill_mode_config WHERE skill_id = ?", (skill_id,))
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
        # ON CONFLICT 命中时保留的是既有行 id，新生成的 cfg_id 并不存在于表中 —
        # 与 upsert_tool_config 同样重查，避免把幽灵 id 返回给调用方
        row = conn.execute(
            "SELECT id FROM skill_mode_config WHERE skill_id = ? AND mode = ?",
            (skill_id, mode),
        ).fetchone()
        return {
            "id": row["id"] if row else cfg_id,
            "skill_id": skill_id,
            "mode": mode,
            "enabled": 1 if enabled else 0,
            "updated_at": now,
        }
    finally:
        conn.close()


# ── 模型配置 CRUD ──

# ── 凭据信封（Windows DPAPI，零第三方依赖）────────────────────────────────────
# LLM api_key（models_config.api_key）与网络工具密钥（app_settings: web.tavily_api_key）
# 不再明文落库：Windows 上经 DPAPI（CryptProtectData，当前用户作用域）加密后以
# `dpapi:v1:<base64>` 信封前缀存储 —— 库文件被拖走时拿到的是只有同一 Windows 用户
# 才能解开的密文。读取侧（unprotect_secret）见到前缀就解密；无前缀按旧明文原样
# 返回（历史行兼容，该值下次经写入侧保存时自动升级为信封）。
# 非 Windows 平台暂保持明文落库（TODO: 接各平台系统凭据库，如 libsecret/Keychain）。
# 原子性口径：本信封只作用于 SQLite 行值，落库走单条 INSERT/UPDATE 事务 ——
# 不引入任何设置文件写盘，CONTEXT.md「半写损坏」的 tmp+rename 由 DB 事务天然保证。
_SECRET_ENVELOPE_PREFIX = "dpapi:v1:"
# 加密平台开关（测试可改写以模拟非 Windows 路径）；仅 win32 启用 DPAPI。
_SECRET_PROTECTION_PLATFORM = "win32"


def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    """调 crypt32 的 DPAPI 做当前用户级加解密（仅 Windows）。

    CRYPTPROTECT_UI_FORBIDDEN(0x1)：后端是无人值守的服务进程，绝不允许
    DPAPI 弹 PIN/确认 UI 卡住请求。两个 ctypes 陷阱（实测 0xc0000374 堆损坏）：
    - pbData 字段必须是 c_void_p 而不是 c_char_p —— c_char_p 字段读出来的是
      「按 NUL 截断的副本」，binary 密文被截断不说，拿它调 LocalFree 等于
      释放一块非堆指针，堆当场损坏；
    - LocalFree 必须声明 argtypes=[c_void_p] —— 不声明时 Python int（64 位
      地址）按 c_int 传参，地址被截断成 32 位，同样释放到错误的指针上。
    """
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.c_void_p),
        ]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = crypt32.CryptProtectData.argtypes
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError(
            f"DPAPI {'加密' if protect else '解密'}失败（WinError {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_secret(value: str) -> str:
    """机密写入侧：明文 → `dpapi:v1:<base64>` 信封（仅 win32）。

    空值原样返回（空串 = 用户清除密钥的语义，不包信封）；已是信封的值不二次
    包裹（幂等）。DPAPI 调用失败向上抛 OSError：静默降级成明文落盘会把
    「这次没加密」藏进库，调用方（模型保存/Tavily 保存）应当让这次保存显式失败。
    """
    value = value or ""
    if (not value or value.startswith(_SECRET_ENVELOPE_PREFIX)
            or sys.platform != _SECRET_PROTECTION_PLATFORM):
        return value
    blob = _dpapi_transform(value.encode("utf-8"), protect=True)
    return _SECRET_ENVELOPE_PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect_secret(value: str) -> str:
    """机密读取侧：见信封前缀就解密还原明文；无前缀按旧明文原样返回。

    解密失败（典型：库文件被拷到别的 Windows 用户/机器）不抛也不回密文，
    返回空串并留 warning —— 把密文当 key 发给上游只会得到费解的 401，
    报「未配置」则引导用户重填一次密钥（下次保存即按当前环境重新加封）。
    """
    if not value or not value.startswith(_SECRET_ENVELOPE_PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(_SECRET_ENVELOPE_PREFIX):], validate=True)
        return _dpapi_transform(blob, protect=False).decode("utf-8")
    except (ValueError, OSError) as e:
        # binascii.Error（base64 畸形，ValueError 子类）、UnicodeDecodeError、
        # DPAPI 的 OSError —— 除此之外的异常不属于"密文读不出"，照抛
        logger.warning("凭据信封解密失败，按未配置处理（库可能来自其他 Windows 用户/机器）: %s", e)
        return ""


def _mask_api_key(key: str | None) -> str:
    """脱敏 API Key：仅保留前4位 + ****"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"


# models_config 全列（三处查询共用）。此前各处各写一份字面量，
# get_default_model 那份漏了 reasoning_tokens —— 读默认模型的调用方拿到 None，
# 思考预算退化为「不限制」。
_MC_COLS = (
    "id, name, model_id, provider, base_url, api_key, "
    "supports_thinking, supports_image, supports_tool_calling, "
    "is_default, is_default_ssh, "
    "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at"
)


def _resolve_model_pk(ref: str) -> Optional[str]:
    """把「行主键 id」或「LLM 模型名（model_id 列）」统一解析成行主键；解析不到返回 None。

    读路径（get_model_config）一直认两种键 —— Agent 后处理与默认模型兜底都传模型名；
    写路径此前只认行主键，于是 PATCH default 传模型名时：存在性检查过 → 清 others 的
    UPDATE 按 id != 名字匹配到**全部**行（真默认被清掉）→ 置一的 UPDATE 匹配 0 行 →
    结果是 0 个默认模型，而响应 200。读写必须共用一个解析器，否则每加一个写入口
    就要再踩一遍（round 14 已登记）。
    """
    if not ref:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM models_config WHERE id = ?", (ref,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM models_config WHERE model_id = ? ORDER BY rowid LIMIT 1",
                (ref,),
            ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def list_models_config() -> list[dict]:
    """获取所有模型配置（按 sort_order 正序，api_key 已脱敏）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_MC_COLS} "
            "FROM models_config ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
        result = [dict(r) for r in rows]
        for item in result:
            # 信封值先还原再掩码：掩码面向前端的口径是「明文的前 4 位」，不是
            # 信封密文的前 4 位（密文每次加封都不同，掩出来就是噪声）
            item["api_key"] = _mask_api_key(unprotect_secret(item.get("api_key") or ""))
        return result
    finally:
        conn.close()


def get_model_config(model_id: str, mask_key: bool = True) -> Optional[dict]:
    """根据 id 获取模型配置（mask_key=True 时 api_key 已脱敏）

    调用方可能传入行主键 id（model-<uuid>）或 LLM 模型名（model_id 列，如
    qwen3.8-27b）— Agent 后处理原样传模型名、chat/agent 的默认模型兜底也优先取
    模型名。两种键的解析规则**只存在于 `_resolve_model_pk` 一处**（S10-22）：
    此前这里抄了第二份「先按 id、落空再按 model_id 回退」，读写两侧各种一份，
    以后调整匹配规则（例如重名取舍）就会只改一边而留下会静默失效的副本。
    """
    if not model_id:
        return None
    pk = _resolve_model_pk(model_id)
    if pk is None:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_MC_COLS} FROM models_config WHERE id = ?",
            (pk,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # 先还原凭据信封（unprotect_secret 对旧明文行是恒等变换），掩码与
        # mask_key=False 的内部消费方（llm_adapter 等）拿到的都是明文
        _raw_key = result.get("api_key")
        if _raw_key is not None:
            _raw_key = unprotect_secret(_raw_key)
        result["api_key"] = _raw_key
        if mask_key:
            result["api_key"] = _mask_api_key(_raw_key)
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
    # max_tokens 是「上下文窗口」列（与 models_config 建表 DEFAULT 16384 一致），
    # 不是输出预算 —— 输出预算由 max_output_tokens 管。窗口若默认 4096，
    # safe_max_tokens 会把输出压到 2048、思考模式下压到 256 下限。
    max_tokens: int = 16384,
    max_output_tokens: int = 4096,
    top_p: float = 0.9,
    is_default_ssh: bool = False,
    reasoning_tokens: int = 8192,
) -> dict:
    """创建模型配置，返回完整记录"""
    # 去除首尾空格：粘贴时可能带入换行/空白，导致上游 API 校验失败（401）
    name = (name or "").strip()
    base_url = (base_url or "").strip()
    api_key = protect_secret((api_key or "").strip())
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

    config_id 认行主键 id 也认模型名（与 get_model_config / set_default_model 同一
    解析器），kwargs 里的 model_id 字段与此互不冲突。
    """
    pk = _resolve_model_pk(config_id)
    if pk is None:
        return None
    allowed = {
        "name", "model_id", "provider", "base_url", "api_key",
        "supports_thinking", "supports_image", "supports_tool_calling",
        "temperature", "max_tokens", "max_output_tokens", "reasoning_tokens", "top_p", "sort_order",
        "is_default", "is_default_ssh",
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
            # api_key 落库前加凭据信封（见 protect_secret；历史明文行在下次
            # 保存时于此处完成信封升级）
            if k == "api_key":
                v = protect_secret(str(v))
            sets.append(f"{k} = ?")
            params.append(1 if isinstance(v, bool) else v)
    # 记录是否存在（优先判断，避免 rowcount 0 的误判）
    existing = get_model_config(pk)
    if not existing:
        return None
    if not sets:
        return existing
    now = _now()
    sets.append("updated_at = ?")
    params.append(now)
    # pk 必须放在最后，对应 SQL 中 WHERE id = ? 的位置
    params.append(pk)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE models_config SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        # 默认标记是唯一标记：置了某一档就得清掉同档的其它行，与 set_default_model
        # 同一个原语，免得两处对"唯一"的理解各写各的
        for col, key in (("is_default", "is_default"), ("is_default_ssh", "is_default_ssh")):
            if kwargs.get(key):
                conn.execute(
                    f"UPDATE models_config SET {col} = 0 WHERE id != ?", (pk,))
        conn.commit()
        # rowcount 0 不代表记录不存在——SQLite 在值未变化时不计入 rowcount
        return get_model_config(pk)
    finally:
        conn.close()


def delete_model_config(model_id: str) -> bool:
    """删除模型配置（默认模型不可删除，任一档都算）"""
    pk = _resolve_model_pk(model_id)
    if pk is None:
        return False
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT is_default, is_default_ssh FROM models_config WHERE id = ?",
            (pk,),
        ).fetchone()
        if row and (row["is_default"] or row["is_default_ssh"]):
            raise ValueError("Cannot delete a model that is set as default")
        cur = conn.execute("DELETE FROM models_config WHERE id = ?", (pk,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# 默认档位 → 列名。mode=None 沿用历史口径（不传 = 全局档），"ssh" 是 routes 里
# SSH_MODE 的取值；显式收窄成这张表，别让"任何非 ssh 的值"都落进 SSH 列。
_DEFAULT_COLUMNS = {None: "is_default", "global": "is_default", "ssh": "is_default_ssh"}


def set_default_model(model_id: str, mode: Optional[str] = None) -> None:
    """设置默认模型。

    mode：None 或 "global" → 全局 is_default；"ssh" → SSH 模式默认。
    model_id 认行主键也认模型名（_resolve_model_pk）；解析不到抛 ValueError ——
    旧实现按名字传进来会"清掉真默认 + 目标匹配 0 行 + 静默成功"，是本函数的
    主要踩坑面。

    审查修复：原实现"全清 + 置一"为两条非原子 UPDATE，并发切换可产生
    0 个或 2 个默认模型（两条语句间交错）。改为单条条件 UPDATE
    （清 others 带 WHERE id != 目标后提交同一事务），消除中间状态。
    """
    if mode not in _DEFAULT_COLUMNS:
        raise ValueError(f"未知的默认档位 {mode!r}（可用：global / ssh）")
    pk = _resolve_model_pk(model_id)
    if pk is None:
        raise ValueError(f"模型不存在: {model_id}")
    col = _DEFAULT_COLUMNS[mode]
    conn = _get_conn()
    try:
        # 两条 UPDATE 置于同一隐式事务，最后一次性 commit（保持原子性）
        conn.execute(
            f"UPDATE models_config SET {col} = 0 WHERE id != ?", (pk,)
        )
        conn.execute(
            f"UPDATE models_config SET {col} = 1 WHERE id = ?", (pk,)
        )
        conn.commit()
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
        col = "is_default" if mode is None else "is_default_ssh"
        row = conn.execute(
            f"SELECT {_MC_COLS} FROM models_config WHERE {col} = 1 LIMIT 1"
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # C1 修复：默认脱敏 api_key，避免明文密钥泄露到前端
        # 审查 M-15：内部需要真实 key 的调用方（embedding/LLM）传 mask_key=False，
        # 否则拿到 "EMPT****" 式脱敏值 → 401 → 静默降级伪 embedding → 记忆维度混存
        # 两个分支都先还原凭据信封（get_model_config 同款），再按 mask_key 决定回明文还是掩码
        _raw_key = result.get("api_key")
        if _raw_key is not None:
            _raw_key = unprotect_secret(_raw_key)
        result["api_key"] = _raw_key if not mask_key else _mask_api_key(_raw_key)
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


def _run_filters(conversation_id: str = None, status: str = None,
                 run_id: str = None) -> tuple[str, list]:
    """按固定列序拼 WHERE 与参数；值全走绑定，列名是这里的字面量。"""
    conds: list[str] = []
    params: list = []
    for col, val in (("conversation_id", conversation_id), ("status", status), ("id", run_id)):
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
    原默认 (\"\", True) 让一条跑到一半崩溃、永远等不到回写的调用在轨迹回放里显示成
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


# ── 应用设置 key-value CRUD ──


def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """读取单个应用设置。

    带凭据信封（`dpapi:v1:` 前缀，见 protect_secret 区块注释）的值在这里统一
    还原成明文 —— 消费方（web_tools 的 Tavily 调用、诊断面板等）不感知信封，
    见到的永远是可用的明文或未设置的空值。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        value = row["value"] if row else default
        return unprotect_secret(value) if value else value
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


def update_app_settings_bulk(settings: dict[str, str]) -> None:
    """单事务批量写入多个应用设置。

    审查R2修复：多键设置（审批策略 / 出站 HTTP 策略）此前逐条 set_app_setting
    各自 commit，中途一条失败 = 库里一半新一半旧，而路由随后仍把内存策略按"全部
    新值"应用 —— 两边各说各话。本 helper 把整批写进同一事务，任何一条失败整体
    回滚并抛出，由调用方以 500 结束、内存策略保持旧值。
    """
    now = _now()
    conn = _get_conn()
    try:
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
                (key, value, now, value, now),
            )
        conn.commit()
    except Exception:
        # 线程本地池化连接的 close 是 no-op，不显式 rollback 会把半截事务
        # 留在该线程上，污染后续读写
        conn.rollback()
        raise
    finally:
        conn.close()


def list_app_settings() -> dict:
    """全部应用设置键值（诊断面板用）。只读，脱敏由调用方负责。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_settings ORDER BY key"
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def get_db_path() -> str:
    """当前生效的数据库路径（init_db 之后才有值；未初始化返回空串）。"""
    return _db_path


# 诊断面板的行数统计白名单：拼进 SQL 的表名必须来自这里，不接受任意入参。
# 这是 count_rows 里 f-string 的唯一放行守卫（fail-closed）：不在元组内的
# 表名直接返回 None，标识符不可能带进 SQL —— 数据值本就没有插值位。
_DIAG_COUNT_TABLES = ("conversations", "messages", "agent_runs", "models_config", "skills")


def count_rows(table: str) -> Optional[int]:
    if table not in _DIAG_COUNT_TABLES:
        return None
    conn = _get_conn()
    try:
        # {table} 已被上方 _DIAG_COUNT_TABLES 白名单收窄（见其注释），剩余
        # 风险只剩「白名单值本身写坏」，字面量元组由 review 兜底，无需再断言
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    except Exception:
        return None
    finally:
        conn.close()


def get_http_policy() -> dict:
    """出站 HTTP 策略（G4/G5）。唯一读取点：netconfig 的启动/重载；进程内实际
    生效值由 netconfig 缓存，routes/settings.py 的 GET/PUT 读的是那份缓存。

    区间与写入侧 Field 同口径：超时 0..600（0 = 不覆盖调用点默认），重试 0..3。
    代理在库里可能带 userinfo，读取时原样返回，脱敏是展示层的责任。

    retry_set / timeout_set / proxy_set 把"库里有没有一行合法值"如实带出去：
    netconfig 靠它决定 env 兜底该不该让位（缺行才用 env，用户写过就以 DB 为准，
    显式写的 0 也算写过）。
    这里不读 env —— env 优先级归 netconfig 单管，两边各读一份会让规则在两个文件里
    各演化一遍。脏行/越界行按默认读数显示但不算"用户设定"，免得一个改坏的值
    永久锁掉运维注入。
    """
    timeout, timeout_set = is_int_app_setting_set(
        "http.timeout_seconds", 0, low=0, high=600)
    retries, retry_set = is_int_app_setting_set(
        "http.retry_count", 1, low=0, high=3)
    return {
        # 代理**故意没有** proxy_set。本仓口径是「库里非空才压过 env」，空串行与缺行
        # 同等对待（round 19 登记、round 22 用两条测试钉住）。切片审查 S0-01 对此有
        # 异议并要求产品裁决 —— 裁决之前不要在这里加「行存在即权威」的判据，
        # 完整来龙去脉见 netconfig.load_policy_from_db 的注释。
        "proxy_url": (get_app_setting("http.proxy_url") or "").strip(),
        "timeout_seconds": timeout,
        "retry_count": retries,
        "timeout_set": timeout_set,
        "retry_set": retry_set,
    }


def diag_counts() -> dict:
    """诊断面板的行数快照（G10）。表名固定取自白名单，不接受外部入参。"""
    return {t: count_rows(t) for t in _DIAG_COUNT_TABLES}


def _int_app_setting(key: str, default: int, low: Optional[int] = None, high: Optional[int] = None) -> int:
    """读取整型 app_setting：脏值/越界值回默认值而不是抛 ValueError。

    写入侧（routes/settings.py 的 Field 区间）只管得住走接口的写：升级前的旧行、
    直接改库、或校验收紧前落下的越界值仍会留在表里。读取侧不兜住的话，一个脏行
    就让 GET /settings/* 整条 500、面板永远打不开，用户连改回来的入口都没有。

    low/high 传入时按写入区间一并校验：越界同样回默认值（不钳到边界——库里那行是
    什么用户意图不可知，默认值是唯一说得过去的读数）。
    """
    value, explicit = is_int_app_setting_set(key, default, low=low, high=high)
    return value if explicit else default


def is_int_app_setting_set(key: str, default: int,
                           low: Optional[int] = None,
                           high: Optional[int] = None) -> tuple[int, bool]:
    """返回 (读数, 这一行是不是用户写下的合法值)。

    "合法"= 存在、非空、能按 int 解析、且不越 low/high。脏行与越界行都算 False：
    调用方（netconfig 的 env 兜底）据此决定该不该让 env 说话 —— 一个手工改坏的
    值不该永久锁掉运维注入。读数按 _int_app_setting 的口径回默认，不钳边界。
    """
    raw = get_app_setting(key)
    if raw is None or not str(raw).strip():
        return default, False
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, False
    if low is not None and value < low:
        return default, False
    if high is not None and value > high:
        return default, False
    return value, True


def get_conv_retention() -> dict:
    """会话保留清理设置（默认关闭）。

    唯一读取点：routes/settings.py 的 GET/PUT 与 routes/agent.py 的 run 前置清理
    原先各抄一份，key 或默认值一改就会两边漂移。
    """
    return {
        "enabled": bool_app_setting("conv_retention.enabled", False),
        # 区间与写入侧 Field 同口径：这条设置打开后是真删用户会话的，脏行/越界行
        # 必须回默认而不是原样放出去（keep_count 会进 DELETE ... rn > ?）。
        "keep_count": _int_app_setting("conv_retention.keep_count", 1000, low=0, high=100000),
    }


def get_permission_policy() -> dict:
    """审批授权策略（E3 + 审批策略两新字段）。唯一读取点：routes/settings.py 的
    GET/PUT 与 main.py 启动回填。

    allowlist_ttl_hours=0（默认）= 永不过期，即引入 TTL 之前 allowlist 的原始语义 ——
    默认值不能选"7 天"，那会让升级当天既有 prod 预登记条目凭空开始倒计时。
    auto_approve_enabled=True（默认）= 设置面板侧不额外拦截；env kill switch
    仍在调用处优先压过它（auto_approve.auto_approve_effective）。
    approval_fallback_s=600（默认）= 审批兜底等待秒数，0 = 永久等待。
    区间与写入侧 Field 同口径（0..8760 小时 / 0..86400 秒）。
    """
    return {
        "allowlist_ttl_hours": _int_app_setting(
            "permission.allowlist_ttl_hours", 0, low=0, high=8760),
        "auto_approve_enabled": bool_app_setting(
            "permission.auto_approve_enabled", True),
        "approval_fallback_s": _int_app_setting(
            "permission.approval_fallback_s", 600, low=0, high=86400),
    }


def bool_app_setting(key: str, default: bool) -> bool:
    """布尔 app_setting：只有字面 "true" 才为 True，脏值回默认。

    全库唯一的布尔设置解析口径 —— 此前 `== "true"`、`.lower() == "true"` 与
    各自的局部函数写了好几份，同一个 "1" 在不同设置上含义不同。写入侧
    （routes/settings.py）一律 `"true" if v else "false"`，所以这里认得比它多
    没有意义，只会把历史脏值洗成意外打开的开关。
    """
    raw = get_app_setting(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip() == "true"


def _float_app_setting(key: str, default: float,
                       low: Optional[float] = None, high: Optional[float] = None) -> float:
    """浮点 app_setting，语义同 _int_app_setting（脏值/越界回默认，不钳边界）"""
    raw = get_app_setting(key)
    try:
        value = float(raw) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN 字面量：任何区间比较都为 False，会溜过 low/high 检查
        return default
    if low is not None and value < low:
        return default
    if high is not None and value > high:
        return default
    return value


def get_memory_settings() -> dict:
    """读取记忆相关设置，返回带默认值的 dict

    max_items 的区间就是写入侧的 Field 区间（10..500），越界读数不能原样放出去：
    这个值喂到 consciousness_tick → mstore.trim_pools(max_items)，其 SQL 是
    `LIMIT -1 OFFSET ?` —— max_items=0 等于 OFFSET 0 无 LIMIT，把每个 kind 下
    全部 active 且未 pin 的记忆一次 retire 掉。没有面板能改回这行，用户只能
    眼看着经验被清光。

    两个 token 预算分账：注入记忆（检索到的历史经验）与画像块（常驻已知事实）各有
    上限，一方变长不会把另一方挤没 —— 写在一处会让"改大画像"悄悄削减"检索召回"。
    """
    return {
        "enabled": bool_app_setting("memory.enabled", True),
        "max_items": _int_app_setting("memory.max_items", 100, low=10, high=500),
        "inject_budget_tokens": _int_app_setting(
            "memory.inject_budget_tokens", 1500, low=200, high=8000),
        "blocks_budget_tokens": _int_app_setting(
            "memory.blocks_budget_tokens", 1200, low=200, high=8000),
        "search_timeout_s": _float_app_setting("memory.search_timeout_s", 8.0, low=0.5, high=30.0),
        "reconcile_llm_enabled": bool_app_setting("memory.reconcile_llm_enabled", True),
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
    "id, name, task_type, schedule_type, interval_minutes, time_hour, time_minute, "
    "weekday, day_of_month, target_host, task_prompt, enabled, next_run_at, "
    "last_run_at, last_status, last_run_id, last_abnormal_at, created_at, updated_at"
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
                id, name, task_type, schedule_type, interval_minutes, time_hour, time_minute,
                weekday, day_of_month, target_host, task_prompt, enabled,
                next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                (data.get("name") or "").strip(),
                # spec 2026-10-27-schedule-task-type：分类缺省归运维；update_schedule 的
                # allowed 集不含 task_type，故类型创建后不可变（即便 patch 带也被丢弃）。
                data.get("task_type") or "ops",
                data.get("schedule_type") or "interval",
                int(data.get("interval_minutes") or 60),
                # None 判定而非 `or`：0（午夜）是合法 time_hour，`0 or 9` 会把每日/每周/
                # 每月任务静默挪到 09:00（与 compute_next_run 同源修复，勿回退成 or）。
                int(data["time_hour"]) if data.get("time_hour") is not None else 9,
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
    """回写调度行的上次执行状态：一次调用只发一条 UPDATE。

    原先是三条 —— 同一行、同一事务、三个写操作加三次索引维护，而 updated_at 只写在
    第一条里。`last_run_at` / `last_run_id` 的「不传或传空串 = 保持原值」是调用方唯一
    在用的形态（schedule_engine.py:324/340/349 收尾那几支只带状态过来），合并成一条时
    必须原样保留，否则面板「上次执行」那一列每次跑完都被抹空。
    """
    sets, params = ["last_status = ?", "updated_at = ?"], [status, _now()]
    if last_run_at:
        sets.append("last_run_at = ?")
        params.append(last_run_at)
    if last_run_id:
        sets.append("last_run_id = ?")
        params.append(last_run_id)
    params.append(schedule_id)
    conn = _get_conn()
    try:
        # 拼进 SQL 的只有上面这几个字面量，值一律走占位符
        conn.execute(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    finally:
        conn.close()


def set_schedule_last_abnormal(schedule_id: str, ts: str) -> None:
    """记录巡检异常沉淀时间（方案 Phase 3）。只在判读为异常时调用，正常巡检零痕迹。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE schedules SET last_abnormal_at = ?, updated_at = ? WHERE id = ?",
            (ts, _now(), schedule_id),
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


def fail_running_schedule_runs(schedule_id: str, error: str) -> int:
    """把某任务所有还在 running 的执行历史标为 failed，返回受影响行数。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE schedule_runs SET status = 'failed', error = ?, finished_at = ?
               WHERE schedule_id = ? AND status = 'running'""",
            (error, _now(), schedule_id),
        )
        conn.commit()
        return cur.rowcount
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


def prune_schedule_runs(max_count: int = 2000) -> int:
    """删除最旧的 schedule_runs，每个 schedule 各保留最近 max_count 条，
    返回删除数量。

    每次到期触发都会插一行（interval 任务 1 分钟一次 = 一天 1440 行），
    不清理会随运行时间无界增长。由调度引擎低频调用（每 6h），不放在
    热路径上。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM schedule_runs WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY schedule_id
                                  ORDER BY created_at DESC
                              ) AS rn
                       FROM schedule_runs
                   ) WHERE rn > ?
               )""",
            (max_count,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def prune_agent_runs(max_count: int = 5000) -> int:
    """删除最旧的 agent_runs（连带级联删除其 tool_calls），返回删除的 run 数。

    每个 Agent 运行插一行 agent_runs、每次工具调用插一行 tool_calls；
    两者只靠 conversation 级联回收，用户长期不删会话时无界增长
    （tool_calls 是最大头）。保留最近 max_count 个 run，由 SSE 事件流
    finally 低频节流触发（每 6h），不放在热路径上。
    注意：tool_calls 经 agent_run_id 外键 ON DELETE CASCADE 级联删除，
    故只删 agent_runs 即可，无需单独 prune tool_calls。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM agent_runs WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  ORDER BY started_at DESC
                              ) AS rn
                       FROM agent_runs
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
    conn = _get_conn()
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


# ── 远程后台任务注册表（agent_jobs）──────────────────────────────
# 终态清单的单一事实源：`update_agent_job_status`（要不要落 finished_at）、
# `get_running_agent_jobs`（哪些行算"还没结束"）、`ops_job.JobManager.update_status`
# （内存侧要不要触发淘汰）三处共用同一份。此前各抄一遍字面量，而 `exited`
# 只在内存侧算终态 —— 库里它 finished_at 恒 NULL 且每次重启都被"恢复"成在跑任务。
JOB_TERMINAL_STATUSES: tuple[str, ...] = ("finished", "failed", "killed", "exited")


def save_agent_job(job_id: str, run_id: str, session_id: str, cmd: str,
                   pid: int | None, log_dir: str, status: str = "running",
                   out_offset: int = 0) -> None:
    """写入/更新一条远程后台任务记录（启动时调用）。

    UPDATE 分支必须把游标与终态字段一起重置：job_id 由进程内的自增序号分配，
    重启后从 1 重新开始，而 agent_jobs 里的**终态**行永远不会被清理
    （_ensure_restored 只捞未终结行，全仓也没有 DELETE FROM agent_jobs），
    于是新任务会复用旧任务的 job_id 撞上这一行。不重置的话
    out_offset 还是上一次读到的字节数 —— job_output 会从那个偏移之后才开始给，
    新任务最前面的输出直接找不回来；exit_code / finished_at 也会把
    「已经退出了」的旧结论挂在一条 running 的行上。
    """
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO agent_jobs
                   (job_id, run_id, session_id, cmd, pid, log_dir, status, out_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   run_id = excluded.run_id,
                   session_id = excluded.session_id,
                   cmd = excluded.cmd,
                   pid = excluded.pid,
                   log_dir = excluded.log_dir,
                   status = excluded.status,
                   out_offset = excluded.out_offset,
                   exit_code = NULL,
                   finished_at = NULL,
                   created_at = excluded.created_at""",
            (job_id, run_id, session_id, cmd, pid, log_dir, status, out_offset),
        )
        conn.commit()
    finally:
        conn.close()


def update_agent_job_status(job_id: str, status: str,
                            exit_code: int | None = None) -> None:
    """更新任务状态（结束/被杀时调用；终态时落 finished_at）。

    只动状态/退出码，不碰 out_offset（运行期由 update_agent_job_offset 维护）。
    """
    conn = _get_conn()
    try:
        terminal = status in JOB_TERMINAL_STATUSES
        if terminal:
            conn.execute(
                """UPDATE agent_jobs SET status = ?, exit_code = ?,
                       finished_at = datetime('now')
                   WHERE job_id = ?""",
                (status, exit_code, job_id),
            )
        else:
            conn.execute(
                "UPDATE agent_jobs SET status = ?, exit_code = ? WHERE job_id = ?",
                (status, exit_code, job_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_agent_job_offset(job_id: str, out_offset: int) -> None:
    """推进 job_output 已读日志的字节偏移（重启后恢复时避免从头重读）。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE agent_jobs SET out_offset = ? WHERE job_id = ?",
            (out_offset, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_running_agent_jobs() -> list[dict]:
    """列出全部未终结的远程任务（backend 重启后恢复注册表用）。

    终态集合取 `JOB_TERMINAL_STATUSES`，与写侧 `update_agent_job_status` 同源 ——
    写侧认终态、这里不认，那条已判死的记录就会在每次重启时被"恢复"成在跑任务。
    `created_at` 只到秒（`_now()`），同秒并列是常态，补 `job_id` 让恢复顺序确定。
    """
    conn = _get_conn()
    try:
        marks = ",".join("?" for _ in JOB_TERMINAL_STATUSES)
        rows = conn.execute(
            f"""SELECT job_id, run_id, session_id, cmd, pid, log_dir, status,
                      out_offset, created_at
               FROM agent_jobs
               WHERE status NOT IN ({marks})
               ORDER BY created_at, job_id""",
            JOB_TERMINAL_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


