"""多渠道机器人：bot_channels 存储与会话身份唯一性。"""

import sqlite3

import database as db


def _channel(persona_id, name="机器人A", app_id="100001"):
    return db.create_bot_channel(
        provider="qq",
        display_name=name,
        persona_id=persona_id,
        app_id=app_id,
        app_secret="secret-raw",
    )


def test_channel_crud(base_db):
    persona = db.create_bot_persona(name="小暖")
    channel = _channel(persona["id"])

    assert channel["provider"] == "qq"
    assert channel["display_name"] == "机器人A"
    assert channel["has_secret"] is True
    assert "secret-raw" not in str(channel)
    assert "app_secret" not in channel

    fetched = db.get_bot_channel(channel["id"])
    assert fetched["app_id"] == "100001"

    updated = db.update_bot_channel(channel["id"], display_name="小号", app_id="100002")
    assert updated["display_name"] == "小号"
    assert updated["app_id"] == "100002"

    listed = db.list_bot_channels()
    assert [c["id"] for c in listed] == [channel["id"]]


def test_secret_round_trips_protected_and_readable(base_db):
    persona = db.create_bot_persona(name="小暖")
    channel = db.create_bot_channel(
        provider="qq",
        display_name="机器人A",
        persona_id=persona["id"],
        app_id="100001",
        app_secret=db.protect_secret("envelope-secret"),
    )
    assert db.get_bot_channel_secret(channel["id"]) == "envelope-secret"
    assert db.get_bot_channel_secret("missing-id") == ""

    db.set_bot_channel_secret(channel["id"], db.protect_secret("轮换后的密钥"))
    assert db.get_bot_channel_secret(channel["id"]) == "轮换后的密钥"

    db.set_bot_channel_secret(channel["id"], "")
    assert db.get_bot_channel(channel["id"])["has_secret"] is False


def test_extra_secret_round_trips_and_stays_out_of_the_channel_dict(base_db):
    persona = db.create_bot_persona(name="小暖")
    channel = db.create_bot_channel(
        provider="wechat",
        display_name="微信A",
        persona_id=persona["id"],
        app_id="wx-app",
        app_secret=db.protect_secret("token"),
        extra_secret=db.protect_secret("aes-key"),
    )

    assert channel["has_secret"] is True
    assert channel["has_extra_secret"] is True
    assert "aes-key" not in str(channel)
    assert db.get_bot_channel_extra_secret(channel["id"]) == "aes-key"

    db.set_bot_channel_extra_secret(channel["id"], "")
    assert db.get_bot_channel(channel["id"])["has_extra_secret"] is False
    # QQ 行没有第三个凭据，行为与老库一致
    assert db.get_bot_channel_extra_secret(_channel(persona["id"])["id"]) == ""


def test_enabled_flag_is_persisted_per_channel(base_db):
    persona = db.create_bot_persona(name="小暖")
    first = _channel(persona["id"])
    second = _channel(persona["id"], name="机器人B", app_id="100002")

    db.set_bot_channel_enabled(first["id"], True)
    assert db.get_bot_channel(first["id"])["enabled"] is True
    assert db.get_bot_channel(second["id"])["enabled"] is False
    assert [c["id"] for c in db.list_enabled_bot_channels()] == [first["id"]]


def test_delete_channel_cascades_conversations_and_messages(base_db):
    persona = db.create_bot_persona(name="小暖")
    channel = _channel(persona["id"])
    conversation = db.create_bot_conversation(
        persona["id"], "qq", "group:openid-1", channel_id=channel["id"]
    )
    db.add_bot_message(conversation["id"], "user", "在吗")

    local = db.create_bot_conversation(persona["id"], channel="local")

    assert db.delete_bot_channel(channel["id"]) is True
    assert db.get_bot_conversation(conversation["id"]) is None
    assert db.get_bot_messages(conversation["id"]) == []
    # 本地陪聊会话不属于任何机器人，不能被牵连
    assert db.get_bot_conversation(local["id"]) is not None


def test_persona_bound_to_channel_cannot_be_deleted(base_db):
    persona = db.create_bot_persona(name="小暖")
    _channel(persona["id"])

    assert db.count_bot_channels(persona_id=persona["id"]) == 1
    assert db.count_bot_channels(persona_id="nope") == 0

    try:
        db.delete_bot_persona(persona["id"])
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("被机器人占用的人格应触发 FK RESTRICT")
    assert db.get_bot_persona(persona["id"]) is not None


def test_same_external_key_under_two_channels_is_two_conversations(base_db):
    warm = db.create_bot_persona(name="小暖")
    cool = db.create_bot_persona(name="阿冷")
    first = _channel(warm["id"], name="机器人A")
    second = _channel(cool["id"], name="机器人B", app_id="100002")

    a = db.get_or_create_bot_conversation(
        warm["id"], "qq", "group:openid-1", channel_id=first["id"]
    )
    b = db.get_or_create_bot_conversation(
        cool["id"], "qq", "group:openid-1", channel_id=second["id"]
    )
    assert a["id"] != b["id"]

    # 同一机器人同一群重复投递只有一条
    again = db.get_or_create_bot_conversation(
        warm["id"], "qq", "group:openid-1", channel_id=first["id"]
    )
    assert again["id"] == a["id"]


def test_persona_change_starts_fresh_conversation_and_keeps_history(base_db):
    warm = db.create_bot_persona(name="小暖")
    cool = db.create_bot_persona(name="阿冷")
    channel = _channel(warm["id"])
    key = "group:openid-1"

    first = db.get_or_create_bot_conversation(
        warm["id"], "qq", key, channel_id=channel["id"]
    )
    db.add_bot_message(first["id"], "user", "旧人格时期的聊天")

    # 改人格：同一群另起一条，旧的留着当历史
    db.update_bot_channel(channel["id"], persona_id=cool["id"])
    second = db.get_or_create_bot_conversation(
        cool["id"], "qq", key, channel_id=channel["id"]
    )
    assert second["id"] != first["id"]
    assert second["persona_id"] == cool["id"]
    assert db.get_bot_conversation(first["id"])["persona_id"] == warm["id"]

    # 改回去：老会话重新命中，历史接着走
    db.update_bot_channel(channel["id"], persona_id=warm["id"])
    resumed = db.get_or_create_bot_conversation(
        warm["id"], "qq", key, channel_id=channel["id"]
    )
    assert resumed["id"] == first["id"]
    assert db.get_bot_messages(first["id"])[0]["content"] == "旧人格时期的聊天"


def test_local_conversations_still_keyed_without_channel(base_db):
    persona = db.create_bot_persona(name="小暖")
    conversation = db.create_bot_conversation(persona["id"], channel="local")
    assert conversation["channel_id"] is None

    channel = _channel(persona["id"])
    qq = db.create_bot_conversation(
        persona["id"], "qq", "group:openid-1", channel_id=channel["id"]
    )
    assert [c["id"] for c in db.list_bot_conversations(channel_id=channel["id"])] == [qq["id"]]

    # 删机器人只清它自己的会话
    assert db.delete_bot_channel(channel["id"]) is True
    assert db.get_bot_conversation(conversation["id"]) is not None
