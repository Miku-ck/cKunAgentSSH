"""数据库迁移域（P2-3 从 database.py 拆出，函数体与入口签名不变）。

init_db（仍在 database.py）建表后调用本模块的 _migrate_db；_migrate_db 再串起
各子迁移。所有函数收 conn 参数，跑在 init_db 的同一事务连接上。
"""
import json
import logging
import sqlite3

from db import _conn as _conn_mod
from db import _util as _util_mod
from db import bot_social as _bot_social_mod
from db import settings as _settings_mod

logger = logging.getLogger(__name__)


def _migrate_db(conn: sqlite3.Connection) -> None:
    """数据库迁移：为旧表添加新字段"""
    # 头像只收上传图片的 data URL：旧库存的表情/文本头像清空，
    # 展示位回到未上传时的纯色占位圈（不做文字回退）
    conn.execute(
        "UPDATE bot_personas SET avatar = '' "
        "WHERE avatar <> '' AND avatar NOT LIKE 'data:image/%'"
    )
    # bot_messages.answered_at：QQ 事件幂等键从「已收到」改成「已送达」。
    # 旧库补列时，带 external_msg_id 的历史消息一律视为已答复 ——
    # 否则升级后平台重投旧事件会把历史对话再答一遍。
    cursor = conn.execute("PRAGMA table_info(bot_messages)")
    bot_message_cols = {row[1] for row in cursor.fetchall()}
    if "answered_at" not in bot_message_cols:
        conn.execute("ALTER TABLE bot_messages ADD COLUMN answered_at TEXT")
        conn.execute(
            "UPDATE bot_messages SET answered_at = created_at "
            "WHERE external_msg_id IS NOT NULL"
        )
    # bot_messages.reasoning：本地陪聊开启思考时，随正式回复一并落库回显。
    if "reasoning" not in bot_message_cols:
        conn.execute("ALTER TABLE bot_messages ADD COLUMN reasoning TEXT")
    # bot_messages.images：看图回复（Phase 2），用户消息内联 data URL 列表（JSON）。
    if "images" not in bot_message_cols:
        conn.execute("ALTER TABLE bot_messages ADD COLUMN images TEXT")
    _migrate_bot_channels(conn)
    _migrate_bot_ambient_lines(conn)
    # bot_conversations：QQ 会话本地显示覆盖两列（Phase 4 头像体系）。
    # 仅展示层，QQ 真实群名/群头像不采集不改写；NULL = 未设置，走默认回落。
    cursor = conn.execute("PRAGMA table_info(bot_conversations)")
    conversation_cols = {row[1] for row in cursor.fetchall()}
    if "display_name" not in conversation_cols:
        conn.execute("ALTER TABLE bot_conversations ADD COLUMN display_name TEXT")
    if "display_avatar" not in conversation_cols:
        conn.execute("ALTER TABLE bot_conversations ADD COLUMN display_avatar TEXT")
    # 防复读账本（P2）：该会话最近一次注入的记忆 id 集合 + 注入时间。
    # JSON {"ids": [...], "at": "YYYY-MM-DD HH:MM:SS"}；7 天窗口外自然解禁。
    if "last_memory_ids" not in conversation_cols:
        conn.execute("ALTER TABLE bot_conversations ADD COLUMN last_memory_ids TEXT")
    # bot_relations：活人感状态轴（P1）旧库补齐。默认值即全新关系的开局档，
    # 存量人格迁移后从「有点好奇、心态开放」起步，不会突然变冷淡或自来熟。
    cursor = conn.execute("PRAGMA table_info(bot_relations)")
    relation_cols = {row[1] for row in cursor.fetchall()}
    for col, ddl in (
        ("trust", "REAL NOT NULL DEFAULT 10"),
        ("intimacy", "REAL NOT NULL DEFAULT 5"),
        ("tension", "REAL NOT NULL DEFAULT 0"),
        ("novelty", "REAL NOT NULL DEFAULT 60"),
        ("valence", "REAL NOT NULL DEFAULT 0"),
        ("energy", "REAL NOT NULL DEFAULT 0.6"),
        ("patience", "REAL NOT NULL DEFAULT 0.8"),
        ("last_event_kind", "TEXT"),
        ("last_event_at", "TEXT"),
        ("last_event_count", "INTEGER NOT NULL DEFAULT 0"),
        ("state_at", "TEXT"),
        ("pending_topic", "TEXT"),
        ("proactive_cooldown_until", "TEXT"),
    ):
        if col not in relation_cols:
            _conn_mod._assert_sql_ident(col)
            conn.execute(f"ALTER TABLE bot_relations ADD COLUMN {col} {ddl}")
    # bot_personas：chat-experience 两列（旧库补齐，新库建表已含）。
    cursor = conn.execute("PRAGMA table_info(bot_personas)")
    persona_cols = {row[1] for row in cursor.fetchall()}
    if "reply_style" not in persona_cols:
        conn.execute(
            "ALTER TABLE bot_personas ADD COLUMN reply_style TEXT NOT NULL DEFAULT 'moderate'"
        )
    if "enable_thinking" not in persona_cols:
        conn.execute(
            "ALTER TABLE bot_personas ADD COLUMN enable_thinking INTEGER NOT NULL DEFAULT 0"
        )
    # 活人感内核五列：旧库补齐，新库建表已含。默认值即「不放大也不失真」的中性档，
    # 存量人格迁移后行为与迁移前一致，不因为升级突然变得话多或自来熟。
    for col, ddl in (
        ("expression_dna", "TEXT NOT NULL DEFAULT '{}'"),
        ("reaction_style", "TEXT NOT NULL DEFAULT 'warm'"),
        ("refusal_style", "TEXT NOT NULL DEFAULT 'soft'"),
        ("address_user", "TEXT NOT NULL DEFAULT ''"),
        ("relationship_speed", "TEXT NOT NULL DEFAULT 'normal'"),
        # 整页编辑的四个自由文本分区：默认空串，迁移后老一个字也不会多注入。
        ("likes_landmines", "TEXT NOT NULL DEFAULT ''"),
        ("memory_directive", "TEXT NOT NULL DEFAULT ''"),
        ("dialogue_samples", "TEXT NOT NULL DEFAULT ''"),
        ("advanced_play", "TEXT NOT NULL DEFAULT ''"),
        ("proactive_enabled", "INTEGER NOT NULL DEFAULT 0"),
        # 旧库补出来的档位列必须落在最保守档：ALTER 的 DEFAULT 会回填存量行。
        ("group_mode", "TEXT NOT NULL DEFAULT 'mention_only'"),
        # 补出来的 @ 开关必须开：升级不能悄悄把群回复换到主动消息通道上，
        # 那条通道要群主开「主动推送」权限，换过去等于让回复整批失败。
        ("mention_sender", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if col not in persona_cols:
            _conn_mod._assert_sql_ident(col)
            conn.execute(f"ALTER TABLE bot_personas ADD COLUMN {col} {ddl}")
    # 会话标题：早期会话沿用创建时的人格名，侧栏与 QQ 记录区无法区分。
    # 每个会话取首条用户消息当标题；标题已不是人格名的（迁移过或用户改过）不动。
    for row in conn.execute(
        "SELECT id, title, persona_snapshot FROM bot_conversations"
    ).fetchall():
        try:
            persona_name = json.loads(row["persona_snapshot"] or "{}").get("name", "")
        except (TypeError, ValueError):
            persona_name = ""
        if row["title"] and row["title"] != persona_name:
            continue
        first = conn.execute(
            "SELECT content FROM bot_messages WHERE conversation_id = ? AND role = 'user' "
            "ORDER BY rowid ASC LIMIT 1",
            (row["id"],),
        ).fetchone()
        title = _bot_social_mod._bot_conversation_title(first["content"]) if first else ""
        if title:
            conn.execute(
                "UPDATE bot_conversations SET title = ? WHERE id = ?",
                (title, row["id"]),
            )
    # 会话快照瘦身（一次性）：旧快照把头像 data URL 整个复制进每个会话，
    # 100 个会话就是 1-2MB，且每次拉列表都要传。头像改为读时关联实时人格，
    # 这里把存量快照里的头像清空；已经清过的行不重复写。
    for row in conn.execute("SELECT id, persona_snapshot FROM bot_conversations").fetchall():
        try:
            snapshot = json.loads(row["persona_snapshot"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(snapshot, dict) and snapshot.get("avatar"):
            snapshot["avatar"] = ""
            conn.execute(
                "UPDATE bot_conversations SET persona_snapshot = ? WHERE id = ?",
                (json.dumps(snapshot, ensure_ascii=False), row["id"]),
            )
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
            "fallback_model_id": "TEXT DEFAULT ''",
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
                _conn_mod._assert_sql_ident(col)
                conn.execute(f"ALTER TABLE models_config ADD COLUMN {col} {col_def}")
                cols.add(col)

        # 修正 name 与 model_id 的值（它们可能正好反了）。
        # 必须在上面那段补列之后：这个函数自己 SELECT id, name, model_id，
        # 而 name 正是补列循环里的一项 —— 原先它在循环之前跑，一个没有 name 列的
        # 旧库会在 init_db 里抛 OperationalError，而 init_db 只有 try/finally、
        # main.py 也不接 → 后端直接起不来（不是某个接口 500，是整个服务不启动）。
        _fix_model_name_and_id(conn)

        # ── supports_tool_calling 存量基线迁移（一次性）──
        # 三列此前「只写不读」（前端只渲染徽标），建表 DEFAULT 0 且种子硬编码 0。
        # 能力门上线后 0 有了真实语义（预判去 tools）—— 若按字面执行，所有存量/种子
        # 模型立即失能工具。用 app_settings 守卫做一次性升级：存量 0 → 1（=「维持
        # 现状」），此后用户在 UI 显式设 0 才真正生效。种子插入处已同步改 1。
        baseline_key = "migration.tool_capability_baseline"
        # 判存在必须用同一 conn 直查 —— 不能走 get_app_setting()：它 finally 里
        # conn.close()，而池化 close 是 rollback-and-keep-alive（见 _PooledConnection），
        # 会把上面 _fix_model_name_and_id 刚写入、尚未 commit 的闸门标记连同互换
        # 修正一起回滚（init_db 的 commit 在整个 try 末尾，中间任何 close 都是回滚点）。
        if not conn.execute(
            "SELECT 1 FROM app_settings WHERE key = ?", (baseline_key,)
        ).fetchone():
            conn.execute(
                "UPDATE models_config SET supports_tool_calling = 1 WHERE supports_tool_calling = 0"
            )
            _settings_mod.set_app_setting(baseline_key, "done")
def _migrate_bot_channels(conn: sqlite3.Connection) -> None:
    """单例 QQ 配置 → `bot_channels` 一行（spec 2026-10-28-bot-multi-channel）。

    旧库把凭据散在 `app_settings` 的 `bot.qq.*` 键上，会话身份靠往 `external_id`
    尾部追加 `context_id` 来区分机器人。两者都由 `channel_id` 列取代。
    """
    # 微信渠道的第三个凭据（EncodingAESKey）；旧库补列，QQ 行留空。
    channel_cols = {row[1] for row in conn.execute("PRAGMA table_info(bot_channels)")}
    if "extra_secret" not in channel_cols:
        conn.execute(
            "ALTER TABLE bot_channels ADD COLUMN extra_secret TEXT NOT NULL DEFAULT ''"
        )

    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(bot_conversations)")}
    if "channel_id" not in conv_cols:
        conn.execute(
            "ALTER TABLE bot_conversations ADD COLUMN channel_id TEXT "
            "REFERENCES bot_channels(id) ON DELETE CASCADE"
        )

    index_cols = [
        row[2] for row in conn.execute("PRAGMA index_info('ix_bot_conversations_external')")
    ]
    if index_cols and index_cols != ["channel_id", "persona_id", "external_id"]:
        conn.execute("DROP INDEX ix_bot_conversations_external")
        conn.execute(
            "CREATE UNIQUE INDEX ix_bot_conversations_external "
            "ON bot_conversations(channel_id, persona_id, external_id) "
            "WHERE external_id IS NOT NULL"
        )

    if conn.execute(
        "SELECT 1 FROM app_settings WHERE key = 'bot.channels.legacy_migrated'"
    ).fetchone():
        return

    def legacy(key: str) -> str:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (f"bot.qq.{key}",)
        ).fetchone()
        return (row["value"] if row else "") or ""

    # 没有旧凭据就是新库：什么都不做，也不落哨兵（此后代码不再写 bot.qq.* 键）。
    if not legacy("app_id"):
        return

    persona_id = legacy("persona_id")
    persona = conn.execute(
        "SELECT id, name FROM bot_personas WHERE id = ?", (persona_id,)
    ).fetchone()
    if not persona:
        # 绑过的人格没了就退到内置人格，至少让原来能聊的机器人升级后还能连。
        persona = conn.execute(
            "SELECT id, name FROM bot_personas ORDER BY is_builtin DESC, created_at ASC LIMIT 1"
        ).fetchone()
    if not persona:
        return

    channel_id = _util_mod._new_id()
    conn.execute(
        "INSERT INTO bot_channels (id, provider, display_name, persona_id, app_id, "
        "app_secret, enabled, created_at) VALUES (?, 'qq', ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            persona["name"] or "QQ 机器人",
            persona["id"],
            legacy("app_id"),
            # 旧值已是 DPAPI 信封（get_app_setting 读时还原），原样搬列即可。
            legacy("app_secret"),
            "1" if legacy("enabled") == "1" else "0",
            _util_mod._now(),
        ),
    )
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('bot.channels.legacy_migrated', '1', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = ?",
        (_util_mod._now(), _util_mod._now()),
    )

    for row in conn.execute(
        "SELECT id, persona_id, external_id FROM bot_conversations "
        "WHERE channel = 'qq' AND external_id IS NOT NULL ORDER BY updated_at ASC, rowid ASC"
    ).fetchall():
        parts = row["external_id"].split(":")
        # 旧格式是 "{kind}:{openid}:{context_id}"，openid 不含冒号，取前两段即纯平台身份。
        external_id = ":".join(parts[:2]) if len(parts) >= 3 else row["external_id"]
        # 人格改走 A→B→A 时同一群会留下 context_id 不同的多行，剥离后同源：
        # 把较早的并进较新的那条，否则这一步会撞唯一索引、整个后端起不来。
        twin = conn.execute(
            "SELECT id FROM bot_conversations "
            "WHERE channel_id = ? AND persona_id IS ? AND external_id = ? AND id != ?",
            (channel_id, row["persona_id"], external_id, row["id"]),
        ).fetchone()
        if twin:
            conn.execute(
                "UPDATE bot_messages SET conversation_id = ? WHERE conversation_id = ?",
                (twin["id"], row["id"]),
            )
            conn.execute("DELETE FROM bot_conversations WHERE id = ?", (row["id"],))
            continue
        conn.execute(
            "UPDATE bot_conversations SET channel_id = ?, external_id = ? WHERE id = ?",
            (channel_id, external_id, row["id"]),
        )
def _migrate_bot_ambient_lines(conn: sqlite3.Connection) -> None:
    """群聊环境上下文落库表（ambient 持久化，M9-5）。

    独立表而非给 bot_messages 加列：ambient 上下文的维度是 (channel_id,
    group_openid)，跨人格共享；bot_messages 强绑 conversation_id（per-persona），
    塞进去要么按人格复制、要么伪造会话行，还会污染幂等/answered_at 语义，
    且 external_msg_id 唯一索引会与主路幂等冲突。CREATE IF NOT EXISTS 天然幂等，
    每 boot 照常执行（与 answered_at 补列同区）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bot_ambient_lines ("
        " id TEXT PRIMARY KEY,"
        " channel_id TEXT NOT NULL,"
        " group_openid TEXT NOT NULL,"
        " speaker TEXT NOT NULL DEFAULT '',"
        " content TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_bot_ambient_lines_key "
        "ON bot_ambient_lines(channel_id, group_openid, created_at)"
    )
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
    now = _util_mod._now()
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
                (mid, n, _util_mod._now(), r["id"])
            )
            swapped += 1
    # 置标记：这次跑过（哪怕一行都没改）就再也不跑。闸门放在猜测之后而不是之前，
    # 老库升级后仍能补上那一次真正的修复。
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES('models.name_id_fixed','1')"
    )
    if swapped:
        logger.info("models_config: 一次性修正 name/model_id 互换 %d 行", swapped)
