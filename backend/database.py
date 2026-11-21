"""
SQLite 数据库层 — 会话与消息持久化

连接管理：线程本地连接池。每个线程持有一个持久连接，复用避免反复
connect/close 的开销；close() 被包装为 rollback-and-keep-alive，
现有代码的 `finally: conn.close()` 模式无需任何改动。
"""
import logging

logger = logging.getLogger(__name__)

# ── 连接层 → db/_conn.py（P2-3 连接层统一，全库唯一一份线程本地连接池）──
# 收敛前 database.py 与 db/ 包各持一份连接池：init_db 的迁移途中调用池化函数
# （如 _migrate_db → set_app_setting）时，若该函数来自 db 包，就会拿到同线程的
# 第二条连接，与迁移连接形成两个写者 → database is locked（P2-3 试收敛 settings
# 域时的实测故障）。此处改为再导出，database 与 db 包共享同一份 _thread_local /
# _db_path / _all_connections。
import db._conn as _db_conn

from db._conn import (  # noqa: F401  (re-export)
    _PooledConnection,
    _assert_sql_ident,
    _assert_sql_ident_list,
    _close_all_connections,
    _conn_lock,
    _create_connection,
    _get_conn,
    _thread_local,
)


def __getattr__(name: str):
    """module 级属性转发：database._db_path 的唯一来源是 db._conn._db_path。

    直接 `from ... import _db_path` 只是字符串快照，init_db 之后再改路径就漂移；
    读取方（runbook_store 的 getattr(db, "_db_path", "")、get_db_path）统一走这里。
    """
    if name == "_db_path":
        return _db_conn._db_path
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ── 库结构版本（建表脚本短路）───────────────────────────────────────────────
# init_db 的建表脚本原本每次启动都整段重跑（连同迁移实测 117ms，占后端启动初始化
# 的三成）。这里用 SQLite 自带的 user_version 标记建表脚本已跑到哪一版：版本命中且
# 核心表齐全就跳过那 360 行 DDL。迁移与一次性数据修复不在此短路段内（见 init_db）。
# 铁律：改动下方 init_db 的 DDL 或 _migrate_db 的任何迁移，必须 _SCHEMA_VERSION += 1，
# 否则老库永远不会执行新结构（backend/tests/test_startup_critical_path.py 有守卫）。
_SCHEMA_VERSION = 10

# 短路前提的核心表。版本号可能被外部工具清零、或迁移脚本自己写错，仅凭
# user_version 就认定「结构已就绪」，会把缺表的库永久锁在坏状态 —— 再核一遍实表。
_SCHEMA_CORE_TABLES = (
    "conversations",
    "messages",
    "agent_runs",
    "app_settings",
    "bot_personas",
)


def _schema_is_current(conn) -> bool:
    """库结构是否已是当前版本（user_version 命中且核心表齐全）。"""
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        if not row or int(row[0]) != _SCHEMA_VERSION:
            return False
        for table in _SCHEMA_CORE_TABLES:
            _assert_sql_ident(table)
            hit = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if hit is None:
                return False
        return True
    except Exception as e:
        # 探测失败不能直接当“结构已就绪”：宁可多跑一次全量初始化
        logger.warning("init_db: 结构版本探测失败，回退全量建表/迁移: %s", e)
        return False




def _skip_script(_script: str) -> None:
    """占位实现：结构版本已是最新时不执行建表脚本。

    用函数替换 conn.executescript 而不是把 360 行 DDL 包进 if：DDL 文本保持原位，
    以后对比两份建表脚本时不会被无意义的缩进噪音干扰。
    """


def init_db(db_path: str) -> None:
    """初始化数据库: 建表 + 启用 WAL + 迁移

    db_path 为绝对路径时直接用；相对路径落在本模块 cwd（脆弱，仅兜底）。
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

    # 唯一路径源是 db._conn._db_path（P2-3 连接层统一后全库共用一份池）。
    # 这里不再写 database._db_path 快照 —— 读它的人（get_db_path /
    # runbook_store）经模块 __getattr__ 转发到 db._conn，避免两处漂移。
    _db_conn._db_path = db_path
    conn = _get_conn()
    try:
        # 建表脚本（数十张表 + 索引）是启动固定成本的大头（实测 ~110ms），结构版本
        # 命中且核心表齐全时整段跳过；下面的增量补列 / 迁移 / 一次性数据修复
        # **不跳** —— 它们修的是 user_version 表达不了的缺损（表还在但列被删、
        # 老数据写反），正是这类库必须能自愈的地方，而它们实测只占 ~8ms。
        _ddl_applied = _schema_is_current(conn)
        _maybe_ddl = _skip_script if _ddl_applied else conn.executescript
        _maybe_ddl("""
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

            CREATE TABLE IF NOT EXISTS bot_personas (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                avatar       TEXT NOT NULL DEFAULT '',
                personality  TEXT NOT NULL DEFAULT '',
                background   TEXT NOT NULL DEFAULT '',
                greeting     TEXT NOT NULL DEFAULT '',
                model_id     TEXT,
                reply_style  TEXT NOT NULL DEFAULT 'moderate',
                enable_thinking INTEGER NOT NULL DEFAULT 0,
                -- 活人感内核（spec：人格内核）：
                -- expression_dna 是表达 DNA 的 JSON（句长/语气词/标点/emoji/口头禅/禁用词），
                -- 其余三项决定「怎么反应」与「关系升温速度」。
                expression_dna      TEXT NOT NULL DEFAULT '{}',
                reaction_style      TEXT NOT NULL DEFAULT 'warm',
                refusal_style       TEXT NOT NULL DEFAULT 'soft',
                address_user        TEXT NOT NULL DEFAULT '',
                relationship_speed  TEXT NOT NULL DEFAULT 'normal',
                -- 人格整页编辑的四个自由文本分区（空串 = 该节不进提示词）。
                likes_landmines     TEXT NOT NULL DEFAULT '',
                memory_directive    TEXT NOT NULL DEFAULT '',
                dialogue_samples    TEXT NOT NULL DEFAULT '',
                advanced_play       TEXT NOT NULL DEFAULT '',
                -- 主动性总开关：默认关（0）。这是「她会主动找你」的总闸，
                -- 打开后由 bot/proactive.py 的意愿函数 + 四重抑制器决定何时开口。
                proactive_enabled   INTEGER NOT NULL DEFAULT 0,
                -- 群参与档位：管的是「在多人场合说到什么程度」，与单聊主动性分开。
                -- mention_only = 只 @ 才答；ambient = @ 后窗口内可接话；
                -- proactive = 再接每群独立 12h 一次的主动开口。默认最保守档，
                -- 升级不会让机器人忽然在群里说话（spec 2026-10-30-qq-group-ambient §5）。
                group_mode          TEXT NOT NULL DEFAULT 'mention_only',
                -- 群里回复要不要 @ 提问者：1 = 照旧带 @（被动回复，平台加的样式），
                -- 0 = 改走不带 msg_id 的主动消息通道。默认开，升级不改既有观感。
                mention_sender      INTEGER NOT NULL DEFAULT 1,
                is_builtin   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 一个机器人账号一行，一行绑一个人格（人格可随时改，见 spec 多渠道机器人）。
            CREATE TABLE IF NOT EXISTS bot_channels (
                id           TEXT PRIMARY KEY,
                provider     TEXT NOT NULL,
                display_name TEXT NOT NULL,
                persona_id   TEXT NOT NULL REFERENCES bot_personas(id) ON DELETE RESTRICT,
                app_id       TEXT NOT NULL DEFAULT '',
                app_secret   TEXT NOT NULL DEFAULT '',
                -- 第三个凭据（微信的 EncodingAESKey）；QQ 留空。同样是 DPAPI 信封。
                extra_secret TEXT NOT NULL DEFAULT '',
                enabled      TEXT NOT NULL DEFAULT '0',
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_conversations (
                id               TEXT PRIMARY KEY,
                title            TEXT NOT NULL DEFAULT '新会话',
                persona_id       TEXT REFERENCES bot_personas(id) ON DELETE SET NULL,
                persona_snapshot TEXT NOT NULL,
                channel          TEXT NOT NULL DEFAULT 'local',
                channel_id       TEXT REFERENCES bot_channels(id) ON DELETE CASCADE,
                external_id      TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 会话身份 = (哪个机器人, 哪个人格, 平台内会话)。改人格后查不到旧行，
            -- 同一群自动另起一条，旧行留作可读历史。
            CREATE UNIQUE INDEX IF NOT EXISTS ix_bot_conversations_external
                ON bot_conversations(channel_id, persona_id, external_id)
                WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_bot_conversations_updated
                ON bot_conversations(channel, updated_at);

            CREATE TABLE IF NOT EXISTS bot_messages (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES bot_conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                external_msg_id TEXT,
                answered_at     TEXT,
                reasoning       TEXT,
                images          TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS ix_bot_messages_conv
                ON bot_messages(conversation_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS ix_bot_messages_external
                ON bot_messages(conversation_id, external_msg_id)
                WHERE external_msg_id IS NOT NULL;

            -- Phase 4 头像图库：集中存上传并压缩后的头像 data URL，供各处头像复用。
            -- 条目存自己的副本（非引用），删图库项不影响已设置的头像。
            CREATE TABLE IF NOT EXISTS avatar_gallery (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL DEFAULT '',
                data_url   TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 关系账本：人格与用户之间的累计熟悉度。
            -- score 只增不减（历史累计），衰减在读时按 last_at 计算，久别重逢会掉档、
            -- 聊两句回档——所以不能在写时把衰减结果落库，否则关系会单向坍缩。
            -- 活人感状态轴（P1）：M 层 trust/intimacy/tension/novelty + S 层
            -- valence/energy/patience，由 bot.state 事件驱动更新；state_at 是
            -- 状态最后写入时刻（衰减读时惰性计算）。score 仍走原口径供 UI 使用。
            CREATE TABLE IF NOT EXISTS bot_relations (
                persona_id  TEXT PRIMARY KEY REFERENCES bot_personas(id) ON DELETE CASCADE,
                score       REAL NOT NULL DEFAULT 0,
                turns       INTEGER NOT NULL DEFAULT 0,
                last_at     TEXT,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                trust       REAL NOT NULL DEFAULT 10,
                intimacy    REAL NOT NULL DEFAULT 5,
                tension     REAL NOT NULL DEFAULT 0,
                novelty     REAL NOT NULL DEFAULT 60,
                valence     REAL NOT NULL DEFAULT 0,
                energy      REAL NOT NULL DEFAULT 0.6,
                patience    REAL NOT NULL DEFAULT 0.8,
                last_event_kind TEXT,
                last_event_at   TEXT,
                last_event_count INTEGER NOT NULL DEFAULT 0,
                state_at    TEXT,
                pending_topic TEXT,
                proactive_cooldown_until TEXT
            );

            -- 主动性（P3）：一次「想找你」的完整记录。action:
            --   send    = 本地渠道已直投（bot_messages 里可见）
            --   push    = QQ 主动消息已由平台收下（delivered_at 同时写入）
            --   queued  = 寄存（主动发被拒或群聊，等对方下一条消息时捎上）
            --   skip    = 过闸失败或平台拒绝（reason 记原因，用于调 θ）
            --   expired = 寄存过期（保留审计行，不参与配额与间隔统计）
            -- checked_at：负反馈检查完成标记（2h 内没被回应 → 冷却翻倍，只查一次）。
            CREATE TABLE IF NOT EXISTS bot_proactive_log (
                id              TEXT PRIMARY KEY,
                persona_id      TEXT NOT NULL REFERENCES bot_personas(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL,
                desire          REAL NOT NULL DEFAULT 0,
                action          TEXT NOT NULL,
                reason          TEXT,
                text            TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT,
                delivered_at    TEXT,
                checked_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_bot_proactive_pending
                ON bot_proactive_log(conversation_id, action, expires_at);
            -- 按人格读日志的两条路径都只能靠它：设置页的「她什么时候想找你」
            -- （routes/bot.py WHERE persona_id=? ORDER BY created_at DESC LIMIT ?）
            -- 与冷场计数（bot/proactive.py WHERE persona_id=? AND reason LIKE 'cold:%'，
            -- 每轮主动性评估都跑一次）。本表每次评估都可能落一行，是 bot 侧增长
            -- 最快的表；persona_id 又是 FK，而 SQLite 不会自动给外键列建索引。
            -- created_at 进索引第二列，让列表查询免排序。
            CREATE INDEX IF NOT EXISTS ix_bot_proactive_persona
                ON bot_proactive_log(persona_id, created_at DESC);

            -- 计划钩子：用户说过的「周末要去看展」，到期后主动问一句。
            -- 独立于记忆行存活：计划记忆 importance 只有 0.4，是维护首批淘汰对象。
            CREATE TABLE IF NOT EXISTS bot_plan_hooks (
                id              TEXT PRIMARY KEY,
                persona_id      TEXT NOT NULL,
                conversation_id TEXT,
                memory_id       TEXT,
                plan_text       TEXT NOT NULL,
                when_text       TEXT NOT NULL,
                due_start       TEXT NOT NULL,
                due_end         TEXT NOT NULL,
                state           TEXT NOT NULL DEFAULT 'pending',
                asked_at        TEXT,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_bot_plan_due
                ON bot_plan_hooks(state, due_end);
            CREATE INDEX IF NOT EXISTS ix_bot_plan_persona
                ON bot_plan_hooks(persona_id, state, plan_text);

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

            -- experts 表不在此处创建：见下方「版本守卫与新增表必须解耦」。
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
                fallback_model_id TEXT DEFAULT '',
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
                expert_id       TEXT,
                expert_name     TEXT,
                expert_snapshot_json TEXT,  -- 服务端生成的策略快照，绝不含密钥/插件 env
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
                -- 通用 harness 化：任务绑定的专家 slug；存量回填 general_ops，新建默认 general
                expert_slug     TEXT NOT NULL DEFAULT 'general',
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
            ("expert_id",         "TEXT"),
            ("expert_name",       "TEXT"),
            ("expert_snapshot_json", "TEXT"),
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
        # schedules 补 expert_slug（旧库）：列不存在才加，幂等。
        cur = conn.execute("PRAGMA table_info(schedules)")
        sched_cols = {row[1] for row in cur.fetchall()}
        if "expert_slug" not in sched_cols:
            conn.execute(
                "ALTER TABLE schedules ADD COLUMN expert_slug TEXT NOT NULL DEFAULT 'general'"
            )
        # 存量定时任务一次性回填 general_ops（保持旧运维行为）。只在版本不命中
        # （_ddl_applied=False，即本次是升级/建库）时跑：已是最新版本的库跳过，
        # 否则会把用户新建的 general 任务每次重启都重置成 general_ops。
        if not _ddl_applied:
            conn.execute(
                "UPDATE schedules SET expert_slug = 'general_ops' WHERE expert_slug = 'general'"
            )
        _migrate_db(conn)
        # experts 建表刻意放在版本化 executescript **之外**：结构版本已是最新时
        # init_db 会整段跳过 executescript，只跑这一片增量区。把新表留在脚本里
        # 会让升级上来的旧库（以及自建精简库的测试）永远没有 experts 表，
        # 随后内置专家种子直接 "no such table" —— 版本守卫与新增表必须解耦。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experts (
                id                TEXT PRIMARY KEY,
                slug              TEXT NOT NULL UNIQUE,
                name              TEXT NOT NULL,
                description       TEXT,
                identity_prompt   TEXT NOT NULL DEFAULT '',
                model_id          TEXT,
                tool_names_json   TEXT,
                plugin_ids_json   TEXT DEFAULT '[]',
                skill_ids_json    TEXT,
                permission_ceiling TEXT NOT NULL DEFAULT 'inherit',
                max_iterations    INTEGER,
                memory_scope      TEXT NOT NULL DEFAULT 'inherit',
                enabled           INTEGER DEFAULT 1,
                is_builtin        INTEGER DEFAULT 0,
                revision          INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_experts_enabled ON experts(enabled)"
        )
        # 插件（MCP server / 外部进程）。与 experts 同理，建表必须在版本守卫之外。
        # env_ciphertext 是整个环境变量 JSON 的 DPAPI 信封，不存任何明文秘密。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plugins (
                id                  TEXT PRIMARY KEY,
                slug                TEXT NOT NULL UNIQUE,
                name                TEXT NOT NULL,
                description         TEXT,
                command             TEXT NOT NULL,
                args_json           TEXT NOT NULL DEFAULT '[]',
                cwd                 TEXT,
                env_ciphertext      TEXT,
                config_rev          INTEGER NOT NULL DEFAULT 1,
                enabled             INTEGER NOT NULL DEFAULT 0,
                consented_at        TEXT,
                timeout_s           INTEGER NOT NULL DEFAULT 30,
                tools_json          TEXT NOT NULL DEFAULT '[]',
                readonly_tools_json TEXT NOT NULL DEFAULT '[]',
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_plugins_enabled ON plugins(enabled)"
        )
        conn.execute(
            "INSERT INTO bot_personas "
            "(id, name, avatar, personality, background, greeting, is_builtin) "
            "SELECT ?, 'cKunBot', '', '真诚、温和、自然，有自己的聊天节奏', "
            "'你是用户的个人机器人伙伴，擅长轻松陪聊和认真倾听', "
            "'嗨，我是 cKunBot。今天想聊点什么？', 1 "
            "WHERE NOT EXISTS (SELECT 1 FROM bot_personas WHERE is_builtin = 1)",
            (_new_id(),),
        )
        # 四个内置专家种子（spec §6.1）。按 slug 判存在（而非 is_builtin）：用户复制出的
        # 自定义专家不该挡住种子落库。局部 import：避免模块加载期拉起 agent 包。
        from agent.expert_policy import (
            builtin_copywriting, builtin_general, builtin_general_ops, builtin_research,
        )
        for _factory in (builtin_general, builtin_general_ops, builtin_research, builtin_copywriting):
            _seed = _factory()
            conn.execute(
                "INSERT INTO experts "
                "(id, slug, name, description, identity_prompt, model_id, tool_names_json, "
                " plugin_ids_json, skill_ids_json, permission_ceiling, max_iterations, "
                " memory_scope, enabled, is_builtin, revision) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM experts WHERE slug = ?)",
                (
                    _new_id(), _seed["slug"], _seed["name"], _seed["description"],
                    _seed["identity_prompt"], _seed["model_id"], _seed["tool_names_json"],
                    _seed["plugin_ids_json"], _seed["skill_ids_json"], _seed["permission_ceiling"],
                    _seed["max_iterations"], _seed["memory_scope"], _seed["enabled"],
                    _seed["is_builtin"], _seed["revision"], _seed["slug"],
                ),
            )
        # 内置运维包升级：旧库 general_ops 行（空身份）→ 代码权威的运维包配置。
        # 只碰 is_builtin=1；用户复制行（is_builtin=0）逐字节不动。每次 init 都跑且幂等
        # （内置配置以代码为权威，API 不可编辑内置行）。
        _ops = builtin_general_ops()
        conn.execute(
            "UPDATE experts SET name = ?, description = ?, identity_prompt = ?, "
            " updated_at = datetime('now') WHERE slug = ? AND is_builtin = 1",
            (_ops["name"], _ops["description"], _ops["identity_prompt"], _ops["slug"]),
        )
        # 审查修复（P2 E9）：_seed_models（含同步 httpx.get）已移至独立异步入口
        # seed_models_async，从 lifespan 调用，避免阻塞事件循环
        # 结构版本最后落盘（与本次 DDL/迁移同一事务）：中途失败则仍为旧版本，
        # 下次启动重新全量初始化，不会出现「版本已升但列没加上」的半迁移态。
        conn.execute(f"PRAGMA user_version = {int(_SCHEMA_VERSION)}")
        conn.commit()
    finally:
        conn.close()


# ── 迁移 / 初始化域 → db/migrate.py（P2-3 拆分，函数体与入口签名不变）──
from db.migrate import (  # noqa: F401  (re-export)
    _fix_model_name_and_id,
    _init_memory_v2,
    _migrate_bot_ambient_lines,
    _migrate_bot_channels,
    _migrate_db,
    _migrate_memory_v1_to_v2,
)
# ── 群聊环境上下文（ambient lines）→ db/ambient.py（P2-3 拆分）──
from db.ambient import (  # noqa: F401  (re-export)
    add_bot_ambient_line,
    list_recent_ambient_lines,
    purge_ambient_lines,
)



# ARCH-002 环解除：_probe_first_model 依赖 netconfig（new_sync_client/redact_*），
# 而 netconfig 启动回读 HTTP 策略时又要 import database —— 函数体搬到独立叶子
# 模块 model_probe（见该文件 docstring）。此处顶层 re-export 保持既有调用点
# （_seed_models 内两处）与测试的 db._probe_first_model 引用/monkeypatch 目标不变；
# 本模块不再出现任何 netconfig 依赖。
from model_probe import _probe_first_model  # noqa: F401  (re-export)


# ── 首启模型种子 → db/seed.py（P2-3 拆分，函数体与入口签名不变）──
from db.seed import (  # noqa: F401  (re-export)
    seed_models_async,
    _insert_seed_model,
)



# ── 基础工具 → db/_util.py（P2-3 收敛，函数签名与入口不变）──
from db._util import _mask_api_key, _new_id, _now  # noqa: F401  (re-export)


# ── 会话 CRUD ──


# ── conversations（P2-3 收敛，函数签名与入口不变）──
from db.conversations import (  # noqa: F401  (re-export)
    create_conversation,
    list_conversations,
    get_conversation,
    update_conversation_status,
    try_set_default_conversation_title,
    delete_conversation,
    touch_conversation,
    add_message,
    get_messages,
    get_recent_messages,
    count_messages_by_role,
    upsert_tool_config,
    list_disabled_tools,
)



# ── 技能 CRUD ──


# skills 全列（四处查询 + 一处 INSERT 共用）。与 _MC_COLS/_SCHEDULE_COLS 同族：
# 各处各抄一份字面量时，加一列只会改到其中几处，其余读法永久拿不到那一列。
# ── 技能 CRUD（P0 阶段 3 复制到 db/skills.py）（P2-3 收敛，函数签名与入口不变）──
from db.skills import (  # noqa: F401  (re-export)
    list_skills,
    list_all_skills,
    count_skills_by_source,
    get_skill,
    get_skill_by_slug,
    create_skill,
    update_skill_enabled,
    upsert_builtin_skill,
    list_builtin_skill_slugs,
    delete_builtin_skill_by_slug,
    delete_skill,
    list_disabled_skills_for_mode,
    upsert_skill_mode_config,
    _SKILLS_COLS,
)



# ── 专家（Expert）CRUD ─────────────────────────────────────────────────
# ── 专家 CRUD（P0 阶段 3 复制到 db/experts.py）（P2-3 收敛，函数签名与入口不变）──
from db.experts import (  # noqa: F401  (re-export)
    _validate_expert_slug,
    list_experts,
    get_expert,
    get_expert_by_slug,
    create_expert,
    update_expert,
    set_expert_enabled,
    delete_expert,
    _EXPERTS_COLS,
    _EXPERT_WRITABLE_FIELDS,
)



# ── 插件（MCP）CRUD ───────────────────────────────────────────────────
# ── 插件 CRUD（P0 阶段 3 复制到 db/plugins.py）（P2-3 收敛，函数签名与入口不变）──
from db.plugins import (  # noqa: F401  (re-export)
    list_plugins,
    get_plugin,
    get_plugin_by_slug,
    create_plugin,
    update_plugin_config,
    consent_plugin,
    set_plugin_enabled,
    set_plugin_tools,
    set_plugin_readonly,
    delete_plugin,
    _PLUGINS_PUBLIC_COLS,
    _PLUGIN_CONFIG_FIELDS,
)



# ── 模型配置 CRUD ──

# ── 凭据信封（Windows DPAPI / 非 Windows keyring）──────────────────────────────
# LLM api_key（models_config.api_key）与网络工具密钥（app_settings: web.tavily_api_key）
# 不再明文落库：Windows 上经 DPAPI（CryptProtectData，当前用户作用域）加密后以
# `dpapi:v1:<base64>` 信封前缀存储 —— 库文件被拖走时拿到的是只有同一 Windows 用户
# 才能解开的密文。非 Windows 上明文存系统凭据库（keyring，可选依赖），DB 行只存
# `keyring:v1:<句柄>` 句柄 —— keyring 不可用时写入侧显式抛错（fail-closed），绝不
# 静默降级成明文落库。
# 读取侧（unprotect_secret）见到任一前缀就解密；无前缀按旧明文原样返回
# （历史行兼容，该值下次经写入侧保存时自动升级为信封）。
# 原子性口径：本信封只作用于 SQLite 行值，落库走单条 INSERT/UPDATE 事务 ——
# 不引入任何设置文件写盘，CONTEXT.md「半写损坏」的 tmp+rename 由 DB 事务天然保证。
# ── 凭据信封 / 系统凭据库（P0 阶段 3 复制到 db/_secrets.py）（P2-3 收敛，函数签名与入口不变）──
from db._secrets import (  # noqa: F401  (re-export)
    SecretStorageError,
    _keyring_import,
    _keyring_set,
    _keyring_get,
    _keyring_delete,
    _dpapi_transform,
    protect_secret,
    unprotect_secret,
    _KEYRING_ENVELOPE_PREFIX,
    _KEYRING_SERVICE,
    _KEYRING_USERNAME_PREFIX,
    _SECRET_ENVELOPE_PREFIX,
        _SECRET_PROTECTION_PLATFORM,
)



# models_config 全列（三处查询共用）。此前各处各写一份字面量，
# get_default_model 那份漏了 reasoning_tokens —— 读默认模型的调用方拿到 None，
# 思考预算退化为「不限制」。
# ── 模型配置 CRUD + fallback 链（P0 阶段 3 复制到 db/models_config.py）（P2-3 收敛，函数签名与入口不变）──
from db.models_config import (  # noqa: F401  (re-export)
    _resolve_model_pk,
    _assert_fallback_chain_ok,
    _normalize_fallback_ref,
    list_models_config,
    get_model_config,
    create_model_config,
    update_model_config,
    delete_model_config,
    set_default_model,
    get_default_model,
    resolve_fallback_model_cfg,
    _DEFAULT_COLUMNS,
    _FALLBACK_CHAIN_MAX_DEPTH,
    _MC_COLS,
)



# ── cKunBot 人格与会话 ──


# 整页编辑的四个自由文本分区：读写、提示词注入、会话快照刷新都按这一份清单走。
# ── bot 域 → db/bot_core.py + db/bot_social.py（P2-3 拆分，入口与签名不变）──
from db.bot_core import (  # noqa: F401  (re-export)
    _PERSONA_TEXT_FIELDS,
    _bot_persona_dict,
    create_bot_persona,
    list_bot_personas,
    get_bot_persona,
    update_bot_persona,
    revise_persona_snapshots,
    copy_bot_persona,
    delete_bot_persona,
    _BOT_CHANNEL_SELECT,
    _bot_channel_dict,
    create_bot_channel,
    list_bot_channels,
    list_enabled_bot_channels,
    get_bot_channel,
    get_bot_channel_persona_id,
    get_bot_channel_secret,
    set_bot_channel_secret,
    get_bot_channel_extra_secret,
    set_bot_channel_extra_secret,
    update_bot_channel,
    set_bot_channel_enabled,
    delete_bot_channel,
    count_bot_channels,
    _persona_snapshot_payload,
    _bot_conversation_dict,
    create_bot_conversation,
    _find_bot_conversation_by_external,
    get_or_create_bot_conversation,
    list_bot_conversations,
    get_bot_conversation,
    delete_bot_conversation,
    get_bot_memory_injection,
    set_bot_memory_injection,
)


from db.bot_social import (  # noqa: F401  (re-export)
    add_plan_hook,
    due_plan_hook,
    mark_plan_asked,
    expire_plan_hooks,
    add_bot_proactive_log,
    get_pending_proactive,
    mark_proactive_delivered,
    last_user_message_at,
    has_wakeup_period,
    expire_stale_proactive,
    list_proactive_targets,
    get_proactive_target,
    latest_proactive_row,
    proactive_sent_today,
    last_proactive_at,
    last_proactive_push_failure_at,
    proactive_sent_today_for_conversation,
    last_proactive_at_for_conversation,
    list_unchecked_proactive,
    mark_proactive_checked,
    bot_conversation_has_user_reply_since,
    set_bot_proactive_cooldown,
    update_bot_conversation_display,
    create_bot_gallery_item,
    list_bot_gallery,
    delete_bot_gallery_item,
    _loads_images,
    add_bot_message,
    _BOT_TITLE_MAX,
    _bot_conversation_title,
    _title_bot_conversation_from_first_message,
    get_bot_messages,
    get_bot_message_by_external_id,
    mark_bot_message_answered,
    get_recent_bot_messages,
    get_bot_relation,
    list_bot_relations,
    bump_bot_relation,
    update_bot_state,
)


# ── Agent Runs CRUD ──


# ── agent_runs（P2-3 收敛，函数签名与入口不变）──
from db.agent_runs import (  # noqa: F401  (re-export)
    create_agent_run,
    update_agent_run,
    mark_stale_agent_runs_failed,
    reset_stale_in_progress_conversations,
    get_agent_run,
    _run_filters,
    get_agent_runs,
    count_agent_runs,
    add_tool_call,
    update_tool_call,
    get_tool_calls,
    _RUN_LIST_COLUMNS,
)



# ── 应用设置 key-value CRUD → db/settings.py（P2-3 收敛，函数签名与入口不变）──
# P0 阶段 3 已把该域复制到 db/settings.py 并逐字同步（当时保留副本是过渡态）；
# 连接层统一后此处收敛为再导出，公共入口 database.get_app_setting 等保持可用
# （调用方多经模块属性访问，测试 monkeypatch database.get_app_setting 语义不变）。
from db.settings import (  # noqa: F401  (re-export)
    bool_app_setting,
    count_rows,
    diag_counts,
    get_app_setting,
    get_conv_retention,
    get_http_policy,
    get_memory_settings,
    get_permission_policy,
    is_int_app_setting_set,
    list_app_settings,
    set_app_setting,
    update_app_settings_bulk,
    _float_app_setting,
    _int_app_setting,
    _DIAG_COUNT_TABLES,
)


def get_db_path() -> str:
    """当前生效的数据库路径（init_db 之后才有值；未初始化返回空串）。"""
    return _db_conn._db_path

# ── Agent 策略 CRUD（reflection / evolved_config）──


# ── Agent 策略 CRUD（P0 阶段 3 复制到 db/agent_strategies.py）（P2-3 收敛，函数签名与入口不变）──
from db.agent_strategies import (  # noqa: F401  (re-export)
    add_agent_strategy,
    get_agent_strategies,
    update_agent_strategy_enabled,
    delete_agent_strategy,
)



# ── 定时任务（schedules） ───────────────────────────────────────────────────────

# ── schedules（P2-3 收敛，函数签名与入口不变）──
from db.schedules import (  # noqa: F401  (re-export)
    list_schedules,
    get_schedule,
    list_enabled_schedules,
    create_schedule,
    update_schedule,
    set_schedule_next_run,
    mark_schedule_run,
    set_schedule_last_abnormal,
    delete_schedule,
    create_schedule_run,
    update_schedule_run,
    fail_running_schedule_runs,
    list_schedule_runs,
    prune_schedule_runs,
    clear_schedule_runs,
    has_running_schedule_run,
    _SCHEDULE_COLS,
)



# ── 保留清理 → db/prune.py（P2-3 拆分，函数体与入口签名不变）──
from db.prune import (  # noqa: F401  (re-export)
    prune_agent_runs,
    prune_conversations,
)



# ── 远程后台任务注册表 → db/agent_jobs.py（P2-3 拆分，函数体与入口签名不变）──
from db.agent_jobs import (  # noqa: F401  (re-export)
    JOB_TERMINAL_STATUSES,
    save_agent_job,
    update_agent_job_status,
    update_agent_job_offset,
    get_running_agent_jobs,
)



