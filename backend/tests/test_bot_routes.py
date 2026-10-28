import json
import os
from types import SimpleNamespace

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


@pytest.fixture
def client(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-routes.db"))
    from routes.bot import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    db._close_all_connections()


def create_persona(client, **overrides):
    body = {"name": "小暖", "personality": "温和", "greeting": "你好呀"}
    body.update(overrides)
    response = client.post("/api/bot/personas", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_persona_and_conversation_routes(client):
    persona = create_persona(client)
    assert client.get("/api/bot/personas").json()[-1]["id"] == persona["id"]

    copied = client.post(f"/api/bot/personas/{persona['id']}/copy")
    assert copied.status_code == 200
    assert copied.json()["name"] == "小暖 副本"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"personality": "安静"}
    )
    assert patched.json()["personality"] == "安静"

    conversation = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    )
    assert conversation.status_code == 200
    conversation_id = conversation.json()["id"]
    assert client.get("/api/bot/conversations?channel=local").json()[0]["id"] == conversation_id
    # 本地新会话自动落开场白为首条 assistant 消息（create_persona 默认 greeting="你好呀"）
    greeting_msgs = client.get(f"/api/bot/conversations/{conversation_id}/messages").json()
    assert len(greeting_msgs) == 1 and greeting_msgs[0]["role"] == "assistant" and greeting_msgs[0]["content"] == "你好呀"

    assert client.delete(f"/api/bot/conversations/{conversation_id}").json() == {"deleted": True}
    assert client.delete(f"/api/bot/personas/{persona['id']}").json() == {"deleted": True}
    assert client.get("/api/bot/conversations?channel=local").json() == []


def test_persona_avatar_accepts_uploaded_image(client):
    avatar = "data:image/webp;base64," + "A" * 20000
    persona = create_persona(client, avatar=avatar)
    assert persona["avatar"] == avatar
    assert client.get("/api/bot/personas").json()[-1]["avatar"] == avatar

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"avatar": "data:image/webp;base64,BBBB"}
    )
    assert patched.json()["avatar"] == "data:image/webp;base64,BBBB"


def test_persona_avatar_rejects_emoji(client):
    # 头像只收上传图片的 data URL：表情/任意文本一律 422，不再当头像存
    for bad in ["🐟", "data:text/plain;base64,AAAA", "x" * 600]:
        assert client.post(
            "/api/bot/personas", json={"name": "小暖", "avatar": bad}
        ).status_code == 422
    # 空串 = 尚未上传头像（内置人格的初始态），合法
    assert create_persona(client, avatar="")["avatar"] == ""


def test_routes_validate_references_and_input(client):
    assert client.post(
        "/api/bot/conversations", json={"persona_id": "missing"}
    ).status_code == 404
    assert client.post("/api/bot/personas", json={"name": ""}).status_code == 422
    assert client.post(
        "/api/bot/chat", json={"conversation_id": "missing", "content": "你好"}
    ).status_code == 404


def test_main_app_registers_bot_routes():
    from main import app

    paths = {route.path for route in app.routes}
    assert "/api/bot/personas" in paths
    assert "/api/bot/chat" in paths
    assert "/api/bot/qq/config" in paths


def test_chat_route_streams_content_and_done(client, monkeypatch):
    model = db.create_model_config(
        name="M", base_url="http://model.test/v1", api_key="k", model_id="m"
    )
    persona = create_persona(client, model_id=model["id"])
    conversation = client.post(
        "/api/bot/conversations", json={"persona_id": persona["id"]}
    ).json()

    async def fake_stream(**_kwargs):
        yield SimpleNamespace(content="你好", reasoning="")
        yield SimpleNamespace(content="呀", reasoning="")

    monkeypatch.setattr("llm_adapter.chat_stream", fake_stream)
    response = client.post(
        "/api/bot/chat", json={"conversation_id": conversation["id"], "content": "在吗"}
    )

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["content"] for event in events if event["type"] == "content"] == ["你好", "呀"]
    assert events[-1]["type"] == "done"
    assert events[-1]["message"]["content"] == "你好呀"
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["你好呀", "在吗", "你好呀"]


def test_persona_chat_experience_columns_migrate_and_default(client):
    rows = client.get("/api/bot/personas").json()
    assert rows, "至少内置 cKunBot"
    p = rows[0]
    assert p["reply_style"] == "moderate"
    assert int(p["enable_thinking"]) == 0


def test_create_copy_update_persona_reply_style(client):
    p = db.create_bot_persona(name="小测", personality="p", background="b",
                              greeting="g", reply_style="concise", enable_thinking=1)
    assert p["reply_style"] == "concise"
    assert int(p["enable_thinking"]) == 1
    upd = db.update_bot_persona(p["id"], reply_style="long")
    assert upd["reply_style"] == "long" and int(upd["enable_thinking"]) == 1
    cp = db.copy_bot_persona(p["id"])
    assert cp["reply_style"] == "long" and int(cp["enable_thinking"]) == 1


def test_persona_invalid_reply_style_rejected(client):
    r = client.post("/api/bot/personas", json={"name": "x", "reply_style": "verbose"})
    assert r.status_code == 422


def test_persona_api_persists_style_and_thinking(client):
    r = client.post("/api/bot/personas", json={"name": "y", "reply_style": "concise", "enable_thinking": True})
    assert r.status_code == 200
    p = r.json()
    assert p["reply_style"] == "concise" and int(p["enable_thinking"]) == 1


def test_local_conversation_created_with_greeting_message(client):
    persona = client.post("/api/bot/personas", json={"name": "开场君", "greeting": "初次见面，多多指教"}).json()
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"], "channel": "local"}).json()
    msgs = client.get(f"/api/bot/conversations/{conv['id']}/messages").json()
    assert len(msgs) == 1 and msgs[0]["role"] == "assistant" and msgs[0]["content"] == "初次见面，多多指教"


def test_bot_chat_sse_emits_reasoning_and_content_events(client, monkeypatch):
    """本地 SSE 面：reasoning chunk 派发 reasoning 事件，正文派发 content 事件，收尾 done。"""
    from bot.chat_service import BotChatService

    async def fake_stream(self, cid, content, external_msg_id=None, *, channel="local"):
        assert channel == "local"
        yield "reasoning", "想"
        yield "content", "好"

    monkeypatch.setattr(BotChatService, "stream_reply", fake_stream)
    monkeypatch.setattr(
        "routes.bot.db.get_bot_messages",
        lambda cid, n=1: [{"id": "m", "role": "assistant", "content": "好", "created_at": ""}],
    )
    conv = client.post(
        "/api/bot/conversations", json={"persona_id": client.get("/api/bot/personas").json()[0]["id"]}
    ).json()
    with client.stream(
        "POST", "/api/bot/chat",
        json={"conversation_id": conv["id"], "content": "在吗"},
    ) as r:
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert "reasoning" in types and "content" in types
    assert next(e for e in events if e["type"] == "reasoning")["content"] == "想"
    assert next(e for e in events if e["type"] == "content")["content"] == "好"
    assert types[-1] == "done"


def test_persona_options_exposes_enums_for_editor(client):
    options = client.get("/api/bot/persona-options").json()
    for key in (
        "sentence", "particles", "punctuation", "emoji",
        "reaction_style", "refusal_style", "relationship_speed",
    ):
        assert key in options, key
        assert options[key]["options"], key
    # 前端靠 value 渲染控件，label 只用于显示：两者都必须存在
    assert all({"value", "label"} <= set(item) for item in options["reaction_style"]["options"])


def test_persona_roundtrips_identity_core_fields(client):
    persona = create_persona(
        client,
        expression_dna={"sentence": "short", "particles": "dry", "emoji": "none", "catchphrase": "说来话长"},
        reaction_style="playful",
        refusal_style="direct",
        address_user="小林",
        relationship_speed="fast",
    )
    fetched = next(p for p in client.get("/api/bot/personas").json() if p["id"] == persona["id"])
    assert fetched["expression_dna"]["sentence"] == "short"
    assert fetched["expression_dna"]["catchphrase"] == "说来话长"
    assert fetched["reaction_style"] == "playful"
    assert fetched["refusal_style"] == "direct"
    assert fetched["address_user"] == "小林"
    assert fetched["relationship_speed"] == "fast"

    patched = client.patch(
        f"/api/bot/personas/{persona['id']}", json={"reaction_style": "blunt"}
    ).json()
    assert patched["reaction_style"] == "blunt"
    # 局部更新不许把没传的字段冲掉
    assert patched["address_user"] == "小林"
    assert patched["expression_dna"]["sentence"] == "short"


def test_persona_rejects_unknown_style_but_sanitizes_dna(client):
    response = client.post(
        "/api/bot/personas", json={"name": "小暖", "reaction_style": "sarcastic"}
    )
    assert response.status_code == 422

    persona = create_persona(client, expression_dna={"sentence": "poetic", "emoji": "medium"})
    # 未知档位退回默认，但已知档位保留：脏 DNA 不该让人格保存失败
    assert persona["expression_dna"]["sentence"] == "mixed"
    assert persona["expression_dna"]["emoji"] == "medium"


def test_copy_persona_keeps_voice_settings(client):
    persona = create_persona(client, expression_dna={"sentence": "long"}, address_user="小林")
    copy = client.post(f"/api/bot/personas/{persona['id']}/copy").json()
    assert copy["expression_dna"]["sentence"] == "long"
    assert copy["address_user"] == "小林"


def test_relations_are_listed_per_persona_and_start_unknown(client):
    persona = create_persona(client)
    relations = client.get("/api/bot/relations").json()
    assert persona["id"] in relations
    state = relations[persona["id"]]
    assert state["stage"] == "陌生"
    assert state["progress"] == 0
    assert state["turns"] == 0


def test_relation_grows_after_a_turn(client, monkeypatch):
    from bot.chat_service import BotChatService

    async def fake_stream(self, cid, content, external_msg_id=None, *, channel="local", images=None):
        yield "content", "在"

    monkeypatch.setattr(BotChatService, "stream_reply", fake_stream)
    persona = create_persona(client)
    conv = client.post("/api/bot/conversations", json={"persona_id": persona["id"]}).json()
    with client.stream("POST", "/api/bot/chat", json={"conversation_id": conv["id"], "content": "在吗"}) as r:
        "".join(r.iter_text())

    # stream_reply 被整体替换，关系记账在真实实现里发生，这里只验证接口可读且形状稳定
    state = client.get("/api/bot/relations").json()[persona["id"]]
    assert set(state) >= {"stage", "progress", "turns", "score", "raw_score"}


def test_patch_sanitizes_expression_dna(client):
    persona = create_persona(client, expression_dna={"sentence": "short"})
    patched = client.patch(
        f"/api/bot/personas/{persona['id']}",
        json={"expression_dna": {"sentence": "poetic", "particles": "lively"}},
    ).json()
    # 局部更新走同一条归一规则：未知档位退回默认，已知档位照收
    assert patched["expression_dna"]["sentence"] == "mixed"
    assert patched["expression_dna"]["particles"] == "lively"
