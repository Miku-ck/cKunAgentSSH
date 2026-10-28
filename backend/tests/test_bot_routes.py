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
    assert client.get(f"/api/bot/conversations/{conversation_id}/messages").json() == []

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
    assert [m["content"] for m in db.get_bot_messages(conversation["id"])] == ["在吗", "你好呀"]
