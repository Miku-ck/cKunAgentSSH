import os

import pytest

import database as db


@pytest.fixture
def base_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot.db"))
    yield
    db._close_all_connections()


def test_default_persona_is_seeded(base_db):
    personas = db.list_bot_personas()
    assert len(personas) == 1
    assert personas[0]["name"] == "cKunBot"
    assert personas[0]["is_builtin"] == 1


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
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["你好", "我在呢"]

    assert db.delete_bot_conversation(conversation["id"]) is True
    assert db.get_bot_messages(conversation["id"]) == []


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
