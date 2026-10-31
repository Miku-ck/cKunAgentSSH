

import database as db


def test_default_persona_is_seeded(base_db):
    personas = db.list_bot_personas()
    assert len(personas) == 1
    assert personas[0]["name"] == "cKunBot"
    assert personas[0]["is_builtin"] == 1


def test_default_persona_cannot_be_deleted(base_db):
    # 默认人格同时是列表置顶与渠道迁移兜底：删掉就没有「默认」可回落了。
    seeded = db.list_bot_personas()[0]
    assert seeded["is_builtin"] == 1
    assert db.delete_bot_persona(seeded["id"]) is False
    assert db.get_bot_persona(seeded["id"])["name"] == "cKunBot"


def test_persona_crud_and_copy(base_db):
    persona = db.create_bot_persona(
        name="小暖",
        avatar="🌱",
        personality="温和、幽默",
        background="喜欢听用户讲日常",
        greeting="今天过得怎么样？",
        model_id="model-1",
    )

    assert db.get_bot_persona(persona["id"])["name"] == "小暖"
    updated = db.update_bot_persona(persona["id"], personality="安静、真诚")
    assert updated["personality"] == "安静、真诚"
    assert updated["background"] == "喜欢听用户讲日常"

    copied = db.copy_bot_persona(persona["id"])
    assert copied["id"] != persona["id"]
    assert copied["name"] == "小暖 副本"
    assert copied["personality"] == "安静、真诚"
    assert len(db.list_bot_personas()) == 3

    assert db.delete_bot_persona(persona["id"]) is True
    assert db.get_bot_persona(persona["id"]) is None
    assert db.delete_bot_persona(persona["id"]) is False


EXPANSION_FIELDS = (
    "likes_landmines", "memory_directive", "dialogue_samples", "advanced_play",
)


def test_expansion_personas_default_to_empty(base_db):
    """没填的分区不参与生成：空串而不是 None，前端与提示词都按「空即不注入」处理。"""
    persona = db.create_bot_persona(name="小暖")
    for field in EXPANSION_FIELDS:
        assert persona[field] == "", field


def test_expansion_persona_roundtrips_update_and_copy(base_db):
    persona = db.create_bot_persona(
        name="晚风",
        likes_landmines="喜欢夜里散步；雷区是被催",
        memory_directive="记住我在准备面试，别提前任",
        dialogue_samples="用户：今天好累\n晚风：又熬到几点？",
        advanced_play="可以玩树洞模式，只听不建议",
    )
    assert persona["likes_landmines"] == "喜欢夜里散步；雷区是被催"

    updated = db.update_bot_persona(persona["id"], advanced_play="再加一个互相打卡")
    assert updated["advanced_play"] == "再加一个互相打卡"
    # 局部更新不许把其余三个分区冲成空
    assert updated["likes_landmines"] == "喜欢夜里散步；雷区是被催"
    assert updated["memory_directive"] == "记住我在准备面试，别提前任"
    assert updated["dialogue_samples"].startswith("用户：今天好累")

    copied = db.copy_bot_persona(persona["id"])
    for field in EXPANSION_FIELDS:
        assert copied[field] == updated[field], field


def test_migrate_db_adds_expansion_columns_to_legacy_db(base_db):
    """旧库缺四个列也不能报错，补完后存量人格取空串（行为与升级前一致）。"""
    persona = db.create_bot_persona(name="旧人格", personality="温和")
    conn = db._get_conn()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bot_personas)")}
        for field in EXPANSION_FIELDS:
            if field in cols:
                conn.execute(f"ALTER TABLE bot_personas DROP COLUMN {field}")
        conn.commit()
    finally:
        conn.close()

    db._migrate_db(db._get_conn())
    restored = db.get_bot_persona(persona["id"])
    for field in EXPANSION_FIELDS:
        assert restored[field] == "", field
    # 迁移要能重复跑：init_db 每次都过一遍，第二次再 ALTER 会抛 duplicate column
    db._migrate_db(db._get_conn())
    assert db.get_bot_persona(persona["id"])["personality"] == "温和"


def test_migrate_db_backfills_the_group_mode_column(base_db):
    """旧库补出来的档位列必须落在最保守档：升级不能让人格忽然在群里开口。"""
    persona = db.create_bot_persona(name="旧人格")
    db.update_bot_persona(persona["id"], group_mode="proactive")
    conn = db._get_conn()
    try:
        conn.execute("ALTER TABLE bot_personas DROP COLUMN group_mode")
        conn.commit()
    finally:
        conn.close()

    db._migrate_db(db._get_conn())
    assert db.get_bot_persona(persona["id"])["group_mode"] == "mention_only"


def test_migrate_db_backfills_mention_sender_as_on(base_db):
    """旧库补出来的 @ 开关必须开：升级不能悄悄把回复换到主动消息通道。"""
    persona = db.create_bot_persona(name="旧人格")
    db.update_bot_persona(persona["id"], mention_sender=False)
    conn = db._get_conn()
    try:
        conn.execute("ALTER TABLE bot_personas DROP COLUMN mention_sender")
        conn.commit()
    finally:
        conn.close()

    db._migrate_db(db._get_conn())
    assert db.get_bot_persona(persona["id"])["mention_sender"] == 1


def test_conversation_snapshot_carries_the_mention_preference(base_db):
    """入站管线读会话快照决定走哪条发送通道：这个字段必须进快照。"""
    persona = db.create_bot_persona(name="小暖")
    conv = db.create_bot_conversation(
        persona_id=persona["id"], channel="qq", external_id="group:g1"
    )
    db.update_bot_persona(persona["id"], mention_sender=False)
    assert db.get_bot_conversation(conv["id"])["persona"]["mention_sender"] == 0


def test_update_persona_revises_open_conversation_snapshots(base_db):
    """人格是「当前怎么说话」而不是开会话那一刻的存档：改完正在聊的会话也要跟着变。"""
    persona = db.create_bot_persona(name="小暖", personality="温和", background="靠海小城")
    conversation = db.create_bot_conversation(persona["id"], channel="local")
    assert db.get_bot_conversation(conversation["id"])["persona"]["personality"] == "温和"

    db.update_bot_persona(
        persona["id"], personality="克制", likes_landmines="雷区是被催", advanced_play="可以玩树洞"
    )
    refreshed = db.get_bot_conversation(conversation["id"])["persona"]
    assert refreshed["personality"] == "克制"
    assert refreshed["background"] == "靠海小城"
    assert refreshed["likes_landmines"] == "雷区是被催"
    assert refreshed["advanced_play"] == "可以玩树洞"
    # 头像仍走读时关联：快照里不该复制一份 data URL
    assert refreshed["avatar"] == ""

    # 人格删掉后历史会话照旧可渲染：快照已落盘，删除不回写
    other = db.create_bot_persona(name="另一位")
    doomed = db.create_bot_conversation(other["id"], channel="local")
    db.delete_bot_persona(other["id"])
    assert db.get_bot_conversation(doomed["id"])["persona"]["name"] == "另一位"


def test_conversation_keeps_persona_snapshot_after_delete(base_db):
    persona = db.create_bot_persona(name="小暖", personality="温和")
    conversation = db.create_bot_conversation(persona["id"], channel="local")
    db.add_bot_message(conversation["id"], "user", "你好")
    db.add_bot_message(conversation["id"], "assistant", "我在呢")

    assert db.delete_bot_persona(persona["id"]) is True
    stored = db.get_bot_conversation(conversation["id"])
    assert stored["persona_id"] is None
    assert stored["persona"]["name"] == "小暖"
    assert stored["persona"]["personality"] == "温和"
    # 人格没了，实时头像自然为空 —— 前端回退首字母，不该残留一份旧头像副本
    assert stored["persona"]["avatar"] == ""
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["你好", "我在呢"]

    assert db.delete_bot_conversation(conversation["id"]) is True
    assert db.get_bot_messages(conversation["id"]) == []


def test_snapshot_strips_avatar_but_resolves_it_live(base_db):
    avatar = "data:image/webp;base64," + "A" * 4000
    persona = db.create_bot_persona(name="小暖", avatar=avatar)
    conversation = db.create_bot_conversation(persona["id"], channel="local")

    raw = db._get_conn().execute(
        "SELECT persona_snapshot FROM bot_conversations WHERE id = ?", (conversation["id"],)
    ).fetchone()["persona_snapshot"]
    # 快照里不许有头像：否则 100 条会话就是 1MB 级别的列表接口
    assert "base64" not in raw
    # 500 → 600：proactive_enabled 字段入库后基础快照 522 字符；这条守卫防的是
    # 头像级（KB/MB）字段混进快照，不是逐字节钉死，小幅放宽即可
    assert len(raw) < 600

    # 但读出来的 persona 要带实时头像，聊天头部才画得出人脸
    listed = db.list_bot_conversations()[0]
    assert listed["persona"]["avatar"] == avatar
    assert listed["persona"]["name"] == "小暖"
    assert listed["preview"] is None or isinstance(listed["preview"], str)

    # 改头像立刻反映到旧会话（引用而非副本）
    db.update_bot_persona(persona["id"], avatar="")
    assert db.get_bot_conversation(conversation["id"])["persona"]["avatar"] == ""


def test_qq_conversation_external_id_is_unique_per_channel(base_db):
    persona = db.create_bot_persona(name="QQ 小暖")
    first = db.get_or_create_bot_conversation(
        persona["id"], channel="qq", external_id="group:group-openid"
    )
    second = db.get_or_create_bot_conversation(
        persona["id"], channel="qq", external_id="group:group-openid"
    )

    assert second["id"] == first["id"]
    assert len(db.list_bot_conversations(channel="qq")) == 1


def test_first_user_message_becomes_conversation_title(base_db):
    persona = db.create_bot_persona(name="小暖")
    conversation = db.create_bot_conversation(persona["id"])
    assert conversation["title"] == "小暖"

    db.add_bot_message(conversation["id"], "user", "  今天  有点累\n后面还有一句  ")
    assert db.get_bot_conversation(conversation["id"])["title"] == "今天 有点累 后面还有一句"

    db.add_bot_message(conversation["id"], "user", "第二条不该覆盖")
    assert db.get_bot_conversation(conversation["id"])["title"] == "今天 有点累 后面还有一句"


def test_long_first_message_title_is_truncated(base_db):
    persona = db.create_bot_persona(name="小暖")
    conversation = db.create_bot_conversation(persona["id"])

    db.add_bot_message(conversation["id"], "user", "字" * 40)

    assert db.get_bot_conversation(conversation["id"])["title"] == "字" * 24 + "…"


def test_mark_bot_message_answered_is_idempotent(base_db):
    persona = db.create_bot_persona(name="QQ 小暖")
    conversation = db.create_bot_conversation(
        persona["id"], channel="qq", external_id="c2c:user-1"
    )
    db.add_bot_message(conversation["id"], "user", "你好", "event-1")

    stored = db.get_bot_message_by_external_id(conversation["id"], "event-1")
    assert stored["answered_at"] is None

    assert db.mark_bot_message_answered(conversation["id"], "event-1") is True
    assert db.get_bot_message_by_external_id(conversation["id"], "event-1")["answered_at"]
    assert db.mark_bot_message_answered(conversation["id"], "event-1") is False
    assert db.mark_bot_message_answered(conversation["id"], "event-missing") is False
