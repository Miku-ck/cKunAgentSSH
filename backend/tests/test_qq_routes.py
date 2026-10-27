import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


class FakeManager:
    def __init__(self):
        self.status = {"state": "offline", "detail": ""}
        self.started = []
        self.stopped = False

    async def start(self, appid, secret, persona_id, context_id):
        self.started.append((appid, secret, persona_id, context_id))
        self.status = {"state": "online", "detail": ""}

    async def stop(self):
        self.stopped = True
        self.status = {"state": "offline", "detail": ""}


@pytest.fixture
def qq_client(tmp_workspace, monkeypatch):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "qq-routes.db"))
    conn = db._get_conn()
    conn.execute("INSERT INTO bot_personas (id, name) VALUES ('persona-1', '一号')")
    conn.execute("INSERT INTO bot_personas (id, name) VALUES ('persona-2', '二号')")
    conn.commit()
    manager = FakeManager()
    monkeypatch.setattr("bot.qq_manager.get_qq_manager", lambda: manager)
    from routes.bot_qq import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        client.manager = manager
        yield client
    db._close_all_connections()


def save_config(client, **overrides):
    body = {"app_id": "app-id", "app_secret": "app-secret", "persona_id": "persona-1"}
    body.update(overrides)
    response = client.put("/api/bot/qq/config", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_config_masks_and_preserves_secret_and_rotates_context_on_persona_change(qq_client):
    first = save_config(qq_client)
    assert first["app_id"] == "app-id"
    assert first["has_secret"] is True
    assert "app_secret" not in first
    assert db.get_app_setting("bot.qq.app_secret") == "app-secret"
    raw = db._get_conn().execute(
        "SELECT value FROM app_settings WHERE key = 'bot.qq.app_secret'"
    ).fetchone()[0]
    assert raw != "app-secret"

    unchanged = save_config(qq_client, app_secret=None)
    assert unchanged["context_id"] == first["context_id"]
    assert db.get_app_setting("bot.qq.app_secret") == "app-secret"

    changed = save_config(qq_client, app_secret=None, persona_id="persona-2")
    assert changed["persona_id"] == "persona-2"
    assert changed["context_id"] != first["context_id"]


def test_connect_uses_saved_config_and_disconnect_stops_manager(qq_client):
    config = save_config(qq_client)
    response = qq_client.post("/api/bot/qq/connect")

    assert response.status_code == 200
    assert response.json()["state"] == "online"
    assert qq_client.manager.started == [
        ("app-id", "app-secret", "persona-1", config["context_id"])
    ]
    assert db.get_app_setting("bot.qq.enabled") == "1"

    assert qq_client.post("/api/bot/qq/disconnect").json()["state"] == "offline"
    assert qq_client.manager.stopped is True
    assert db.get_app_setting("bot.qq.enabled") == "0"


def test_connect_requires_complete_config(qq_client):
    response = qq_client.post("/api/bot/qq/connect")
    assert response.status_code == 400
    assert qq_client.manager.started == []
