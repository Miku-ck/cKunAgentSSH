"""微信公网回调路由：离线渠道 ack、坏报文 400、超大报文 413。"""

import fastapi
import pytest
from fastapi.testclient import TestClient

from bot.wechat_channel import WeChatPayloadError


class FakeHandle:
    def __init__(self, error=None, reply=None):
        self.error = error
        self.reply = reply or {"errcode": 0, "errmsg": "ok"}
        self.bodies = []

    async def accept(self, body):
        self.bodies.append(body)
        if self.error:
            raise self.error
        return dict(self.reply)


class FakeRuntime:
    def __init__(self):
        self.handles = {}

    def handle(self, channel_id):
        return self.handles.get(channel_id)


@pytest.fixture
def client(monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr("bot.channel_runtime.get_channel_runtime", lambda: runtime)
    from routes.wechat_callback import router

    app = fastapi.FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        test_client.runtime = runtime
        yield test_client


def test_known_channel_acknowledges_and_forwards_the_body(client):
    handle = FakeHandle()
    client.runtime.handles["ch-1"] = handle

    response = client.post("/bot/wechat/ch-1", json={"encrypted": "abc"})

    assert response.status_code == 200
    assert response.json() == {"errcode": 0, "errmsg": "ok"}
    assert handle.bodies == [{"encrypted": "abc"}]


def test_offline_channel_is_acknowledged_so_the_platform_stops_retrying(client):
    response = client.post("/bot/wechat/missing", json={"encrypted": "abc"})

    assert response.status_code == 200
    assert response.json() == {"errcode": 0, "errmsg": "ok"}


def test_bad_payload_is_reported_as_400(client):
    client.runtime.handles["ch-1"] = FakeHandle(error=WeChatPayloadError("解密失败"))

    response = client.post("/bot/wechat/ch-1", json={"encrypted": "bad"})

    assert response.status_code == 400
    assert "解密失败" in response.json()["detail"]


def test_broken_json_is_400(client):
    response = client.post(
        "/bot/wechat/ch-1", content=b"not-json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_oversized_body_is_rejected_before_decryption(client):
    payload = b'{"encrypted": "' + b"a" * (70 * 1024) + b'"}'
    response = client.post("/bot/wechat/ch-1", content=payload)

    assert response.status_code == 413
