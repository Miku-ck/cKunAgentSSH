"""机器人渠道接口的契约：凭据永不回显、状态各报各的、级联删除。"""

import json
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db
from bot.provider import ChannelAlreadyConnectedError, ChannelConnectError

OFFLINE = {"state": "offline", "detail": ""}


class FakeRuntime:
    def __init__(self):
        self.statuses = {}
        self.started = []
        self.stopped = []
        self.fail_with = None

    def supports(self, provider_name):
        return provider_name == "qq"

    def status(self, channel_id):
        return self.statuses.get(channel_id, dict(OFFLINE))

    async def start(self, channel, *, wait_ready=True):
        if self.fail_with is not None:
            raise self.fail_with
        self.started.append((channel["id"], wait_ready))
        self.statuses[channel["id"]] = {"state": "online", "detail": ""}
        return self.statuses[channel["id"]]

    async def stop(self, channel_id):
        self.stopped.append(channel_id)
        self.statuses.pop(channel_id, None)
        return dict(OFFLINE)


@pytest.fixture
def client(tmp_workspace, monkeypatch):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "channels-routes.db"))
    conn = db._get_conn()
    conn.execute("INSERT INTO bot_personas (id, name) VALUES ('persona-1', '一号')")
    conn.execute("INSERT INTO bot_personas (id, name) VALUES ('persona-2', '二号')")
    conn.commit()

    runtime = FakeRuntime()
    monkeypatch.setattr("bot.channel_runtime.get_channel_runtime", lambda: runtime)
    from routes.bot_channels import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        test_client.runtime = runtime
        yield test_client
    db._close_all_connections()


def create(client, **overrides):
    body = {
        "display_name": "机器人A",
        "persona_id": "persona-1",
        "app_id": "app-id",
        "app_secret": "app-secret",
    }
    body.update(overrides)
    response = client.post("/api/bot/channels", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_created_channel_masks_its_secret(client):
    channel = create(client)

    assert channel["provider"] == "qq"
    assert channel["display_name"] == "机器人A"
    assert channel["app_id"] == "app-id"
    assert channel["has_secret"] is True
    assert channel["status"] == OFFLINE
    assert channel["conversation_count"] == 0
    assert "app_secret" not in channel
    assert "app-secret" not in json.dumps(channel, ensure_ascii=False)

    stored = db.get_bot_channel(channel["id"])
    assert "app-secret" not in str(stored)
    assert db.get_bot_channel_secret(channel["id"]) == "app-secret"

    listed = json.dumps(client.get("/api/bot/channels").json(), ensure_ascii=False)
    assert "app-secret" not in listed


def test_first_creation_requires_a_secret(client):
    response = client.post(
        "/api/bot/channels",
        json={"display_name": "A", "persona_id": "persona-1", "app_id": "x", "app_secret": ""},
    )
    assert response.status_code == 400
    assert "AppSecret" in response.json()["detail"]


def test_unknown_persona_or_provider_is_rejected(client):
    assert client.post(
        "/api/bot/channels",
        json={
            "display_name": "A",
            "persona_id": "nope",
            "app_id": "x",
            "app_secret": "s",
        },
    ).status_code == 404

    response = client.post(
        "/api/bot/channels",
        json={
            "provider": "telegram",
            "display_name": "A",
            "persona_id": "persona-1",
            "app_id": "x",
            "app_secret": "s",
        },
    )
    # 可注册的渠道类型由运行时决定，不是请求模型里写死的枚举
    assert response.status_code == 400
    assert "telegram" in response.json()["detail"]


def test_channels_are_listed_with_their_own_status(client):
    first = create(client)
    second = create(client, display_name="机器人B", app_id="app-id-2", persona_id="persona-2")
    client.runtime.statuses[first["id"]] = {"state": "online", "detail": ""}

    body = client.get("/api/bot/channels").json()

    # created_at 只到秒、id 随机，同秒建行按 id 决胜：顺序稳定但不等于创建顺序，
    # 这里只要求「两个都在、各自带自己的状态」。
    by_id = {c["id"]: c for c in body}
    assert set(by_id) == {first["id"], second["id"]}
    assert by_id[first["id"]]["status"]["state"] == "online"
    assert by_id[second["id"]]["status"] == OFFLINE


def test_connect_records_enabled_and_disconnect_clears_it(client):
    channel = create(client)

    response = client.post(f"/api/bot/channels/{channel['id']}/connect")

    assert response.status_code == 200
    assert response.json()["state"] == "online"
    assert client.runtime.started == [(channel["id"], True)]
    assert db.get_bot_channel(channel["id"])["enabled"] is True

    assert client.post(f"/api/bot/channels/{channel['id']}/disconnect").json()["state"] == "offline"
    assert client.runtime.stopped == [channel["id"]]
    assert db.get_bot_channel(channel["id"])["enabled"] is False


def test_connect_without_a_secret_says_so(client):
    channel = create(client)
    db.set_bot_channel_secret(channel["id"], "")

    response = client.post(f"/api/bot/channels/{channel['id']}/connect")

    assert response.status_code == 400
    assert client.runtime.started == []


def test_connect_conflict_returns_409_and_does_not_enable(client):
    channel = create(client)
    client.runtime.fail_with = ChannelAlreadyConnectedError("机器人A 已在运行")

    response = client.post(f"/api/bot/channels/{channel['id']}/connect")

    assert response.status_code == 409
    assert response.json()["detail"] == "机器人A 已在运行"
    assert db.get_bot_channel(channel["id"])["enabled"] is False


def test_fatal_connect_failure_clears_enabled_so_boot_stops_retrying(client):
    channel = create(client)
    db.set_bot_channel_enabled(channel["id"], True)
    client.runtime.fail_with = ChannelConnectError("AppID 或 AppSecret 不正确", fatal=True)

    response = client.post(f"/api/bot/channels/{channel['id']}/connect")

    assert response.status_code == 400
    assert "AppID 或 AppSecret 不正确" in response.json()["detail"]
    assert db.get_bot_channel(channel["id"])["enabled"] is False


def test_retryable_connect_failure_keeps_enabled(client):
    channel = create(client)
    db.set_bot_channel_enabled(channel["id"], True)
    client.runtime.fail_with = ChannelConnectError("连接 QQ 服务器超时，请检查网络后重试")

    response = client.post(f"/api/bot/channels/{channel['id']}/connect")

    assert response.status_code == 400
    assert db.get_bot_channel(channel["id"])["enabled"] is True


def test_edit_keeps_the_secret_when_none_is_sent_and_rotates_when_one_is(client):
    channel = create(client)

    unchanged = client.put(
        f"/api/bot/channels/{channel['id']}", json={"display_name": "小号", "app_secret": None}
    ).json()
    assert unchanged["display_name"] == "小号"
    assert unchanged["has_secret"] is True
    assert db.get_bot_channel_secret(channel["id"]) == "app-secret"

    rotated = client.put(
        f"/api/bot/channels/{channel['id']}", json={"app_secret": "轮换后的密钥"}
    ).json()
    assert rotated["has_secret"] is True
    assert db.get_bot_channel_secret(channel["id"]) == "轮换后的密钥"


def test_persona_can_be_edited_but_not_while_connected(client):
    channel = create(client)

    changed = client.put(
        f"/api/bot/channels/{channel['id']}", json={"persona_id": "persona-2"}
    ).json()
    assert changed["persona_id"] == "persona-2"

    client.post(f"/api/bot/channels/{channel['id']}/connect")
    response = client.put(
        f"/api/bot/channels/{channel['id']}", json={"persona_id": "persona-1"}
    )

    assert response.status_code == 409
    assert "断开" in response.json()["detail"]


def test_delete_stops_the_connection_and_reports_the_history_it_removed(client):
    channel = create(client)
    conversation = db.create_bot_conversation(
        "persona-1", "qq", "group:g-1", channel_id=channel["id"]
    )
    db.add_bot_message(conversation["id"], "user", "在吗")
    client.post(f"/api/bot/channels/{channel['id']}/connect")

    response = client.delete(f"/api/bot/channels/{channel['id']}")

    assert response.json() == {"deleted": True, "conversation_count": 1}
    assert client.runtime.stopped == [channel["id"]]
    assert db.get_bot_channel(channel["id"]) is None
    assert db.get_bot_conversation(conversation["id"]) is None
    assert db.get_bot_messages(conversation["id"]) == []


def test_missing_channel_is_404(client):
    assert client.put("/api/bot/channels/nope", json={"display_name": "x"}).status_code == 404
    assert client.post("/api/bot/channels/nope/connect").status_code == 404
    assert client.post("/api/bot/channels/nope/disconnect").status_code == 404
    assert client.delete("/api/bot/channels/nope").status_code == 404


def test_channel_count_is_capped(client):
    from routes.bot_channels import MAX_BOT_CHANNELS

    for index in range(MAX_BOT_CHANNELS):
        create(client, display_name=f"机器人{index}", app_id=f"app-{index}")

    response = client.post(
        "/api/bot/channels",
        json={
            "display_name": "超出的",
            "persona_id": "persona-1",
            "app_id": "app-overflow",
            "app_secret": "s",
        },
    )
    assert response.status_code == 400
    assert str(MAX_BOT_CHANNELS) in response.json()["detail"]
