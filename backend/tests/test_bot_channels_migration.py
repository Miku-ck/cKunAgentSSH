"""老单例 QQ 配置 → bot_channels 的一次性迁移。"""

import database as db

LEGACY_CONTEXT = "ctx-abc"


def _seed_legacy_single_bot(base_db):
    """把新库伪装成「只有一个 QQ 机器人、配置散在 app_settings」的旧库。"""
    persona = db.create_bot_persona(name="小暖")
    db.set_app_setting("bot.qq.app_id", "100001")
    db.set_app_setting("bot.qq.app_secret", db.protect_secret("旧密钥"))
    db.set_app_setting("bot.qq.persona_id", persona["id"])
    db.set_app_setting("bot.qq.context_id", LEGACY_CONTEXT)
    db.set_app_setting("bot.qq.enabled", "1")

    conversation = db.create_bot_conversation(
        persona["id"], "qq", f"group:openid-1:{LEGACY_CONTEXT}"
    )
    db.add_bot_message(conversation["id"], "user", "历史消息")

    # 旧库的唯一索引是 (channel, external_id)，伪装出来好让迁移去重建它
    conn = db._get_conn()
    conn.execute("DROP INDEX IF EXISTS ix_bot_conversations_external")
    conn.execute(
        "CREATE UNIQUE INDEX ix_bot_conversations_external "
        "ON bot_conversations(channel, external_id) WHERE external_id IS NOT NULL"
    )
    conn.commit()
    return persona, conversation


def _index_columns(conn, index_name):
    return [row[2] for row in conn.execute(f"PRAGMA index_info({index_name})")]


def test_legacy_config_becomes_one_channel_row(base_db):
    persona, _ = _seed_legacy_single_bot(base_db)
    db._migrate_db(db._get_conn())

    channels = db.list_bot_channels()
    assert len(channels) == 1
    channel = channels[0]
    assert channel["provider"] == "qq"
    assert channel["persona_id"] == persona["id"]
    assert channel["app_id"] == "100001"
    # 旧库里开的机，迁移后仍应保持开机自动恢复
    assert channel["enabled"] is True
    assert channel["display_name"] == "小暖"
    assert db.get_bot_channel_secret(channel["id"]) == "旧密钥"


def test_conversations_are_backfilled_and_context_id_stripped(base_db):
    _, conversation = _seed_legacy_single_bot(base_db)
    db._migrate_db(db._get_conn())

    channel = db.list_bot_channels()[0]
    stored = db.get_bot_conversation(conversation["id"])
    assert stored["channel_id"] == channel["id"]
    assert stored["external_id"] == "group:openid-1"
    assert db.get_bot_messages(conversation["id"])[0]["content"] == "历史消息"

    # 迁移后同一群的再次投递必须命中同一条，不能因为剥离而新建
    again = db.get_or_create_bot_conversation(
        channel["persona_id"], "qq", "group:openid-1", channel_id=channel["id"]
    )
    assert again["id"] == conversation["id"]


def test_unique_index_is_rebuilt_onto_channel_scoped_identity(base_db):
    _seed_legacy_single_bot(base_db)
    conn = db._get_conn()
    assert _index_columns(conn, "ix_bot_conversations_external") == ["channel", "external_id"]

    db._migrate_db(conn)
    assert _index_columns(conn, "ix_bot_conversations_external") == [
        "channel_id",
        "persona_id",
        "external_id",
    ]


def test_migrated_channel_is_not_resurrected_after_deletion(base_db):
    """哨兵的作用：用户删光机器人后，遗留的 bot.qq.* 键不能再把它变回来。"""
    _seed_legacy_single_bot(base_db)
    conn = db._get_conn()
    db._migrate_db(conn)
    db.delete_bot_channel(db.list_bot_channels()[0]["id"])

    db._migrate_db(conn)
    assert db.list_bot_channels() == []


def test_old_bot_channels_table_gains_the_extra_secret_column(base_db):
    conn = db._get_conn()
    conn.execute("ALTER TABLE bot_channels DROP COLUMN extra_secret")
    conn.commit()
    assert "extra_secret" not in {row[1] for row in conn.execute("PRAGMA table_info(bot_channels)")}

    db._migrate_bot_channels(conn)

    assert "extra_secret" in {row[1] for row in conn.execute("PRAGMA table_info(bot_channels)")}


def test_fresh_database_without_legacy_config_creates_nothing(base_db):
    db._migrate_db(db._get_conn())
    assert db.list_bot_channels() == []
