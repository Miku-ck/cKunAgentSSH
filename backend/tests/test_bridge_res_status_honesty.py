"""bridge res 帧的 status 缺失/畸形不能长成成功。

`bridge_relay` 收 res 帧时原先写的是 `status = int(msg.get("status") or 200)`。
消费侧 `electron_bridge._ws_request` 用 `status >= 400` 判失败 —— 于是「或 200」这一句
把两类失败改写了形态：

  {"type":"res","id":…,"result":{"error":"ssh 连不上"}}   → status=200  当成成功结果
  {"type":"res","id":…,"status":0,…}                      → status=200  当成成功结果
  {"type":"res","id":…,"status":-1,…}                     → status=-1   仍 <400 ⇒ 成功
  {"type":"res","id":…,"status":"boom",…}                 → status=500  这一条本来就是失败侧

缺字段/0/负数/越界一律归 502（网关没拿到上游的合法状态码），与相邻的不可解析分支同侧。
反向锁：合法状态码原样透传，尤其 4xx/5xx 不许被"顺手修正"。3xx 仍按消费侧既有口径透传
（那是另一条已登记的口径问题，不在本批动）。

运行: python -m pytest backend/tests/test_bridge_res_status_honesty.py -v
"""
import asyncio
import json

import pytest

from agent.bridge_relay import BridgeRelay, set_bridge_relay

TOKEN = "test-token-123"


class _FakeWS:
    """只实现 relay 用到的那几个 ws 方法（与 test_bridge_relay 的 FakeWS 独立，避免跨文件耦合）"""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        return await self.inbound.get()

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        pass


async def _handshake(monkeypatch) -> tuple[BridgeRelay, _FakeWS, asyncio.Task]:
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    ws = _FakeWS()
    handler = asyncio.create_task(relay.handle_bridge_ws(ws))
    await ws.inbound.put(json.dumps({"type": "hello", "token": TOKEN}))
    for _ in range(50):
        if relay.connected:
            break
        await asyncio.sleep(0.01)
    assert relay.connected is True, "握手没完成，后面的断言都是空的"
    return relay, ws, handler


async def _roundtrip(relay: BridgeRelay, ws: _FakeWS, reply: dict) -> tuple[int, object]:
    """发一条 req，用给定字段回 res，返回 relay 交给消费侧的 (status, result)"""
    req_task = asyncio.create_task(relay.send_request("ssh_exec", {}, 5.0))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    req = None
    while req is None:
        for raw in ws.sent:
            f = json.loads(raw)
            if f.get("type") == "req":
                req = f
                break
        if req is None:
            if loop.time() >= deadline:
                raise AssertionError("没等到 req 帧")
            await asyncio.sleep(0.01)
    await ws.inbound.put(json.dumps({"type": "res", "id": req["id"], **reply}))
    return await asyncio.wait_for(req_task, 2.0)


def _teardown(handler: asyncio.Task) -> None:
    handler.cancel()
    set_bridge_relay(None)


@pytest.mark.parametrize("reply", [
    pytest.param({"result": {"error": "ssh 连不上"}}, id="缺 status 字段"),
    pytest.param({"status": 0, "result": {"error": "无上游响应"}}, id="status=0"),
    pytest.param({"status": -1, "result": None}, id="status 负数"),
    pytest.param({"status": 70000, "result": None}, id="status 越界"),
])
@pytest.mark.asyncio
async def test_畸形状态码不得伪装成成功(monkeypatch, reply):
    relay, ws, handler = await _handshake(monkeypatch)
    try:
        status, _result = await _roundtrip(relay, ws, reply)
        assert status == 502, f"失败形态被折成 {status}，消费侧 `>=400` 判不出来"
    finally:
        _teardown(handler)


@pytest.mark.asyncio
async def test_不可解析的状态码也在失败侧(monkeypatch):
    relay, ws, handler = await _handshake(monkeypatch)
    try:
        status, _ = await _roundtrip(relay, ws, {"status": "boom", "result": None})
        assert status >= 500, f"非数字状态码落到了成功侧：{status}"
    finally:
        _teardown(handler)


@pytest.mark.parametrize("code", [200, 201, 204, 400, 401, 404, 500, 503])
@pytest.mark.asyncio
async def test_合法状态码原样透传(monkeypatch, code):
    relay, ws, handler = await _handshake(monkeypatch)
    try:
        status, result = await _roundtrip(relay, ws, {"status": code, "result": {"ok": True}})
        assert status == code
        assert result == {"ok": True}
    finally:
        _teardown(handler)


@pytest.mark.asyncio
async def test_字符串数字状态码仍可透传(monkeypatch):
    """status 被序列化成字符串时不该因为收紧就判失败。"""
    relay, ws, handler = await _handshake(monkeypatch)
    try:
        status, _ = await _roundtrip(relay, ws, {"status": "404", "result": None})
        assert status == 404
    finally:
        _teardown(handler)
