"""bridge_relay — WS 服务端中枢单元测试（内存 FakeWS，不依赖真实网络/uvicorn）。

覆盖：hello 鉴权（通过/拒绝）、req/res 往返、等待超时、断线失效、
新连接替换旧连接（含旧连接在途请求失败）、ping/pong、端点无 relay 兜底。
"""
import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from agent import bridge_relay as br
from agent.bridge_relay import (
    CLOSE_REPLACED,
    CLOSE_TOKEN_REJECTED,
    CLOSE_TRY_LATER,
    BridgeRelay,
    BridgeWsLostError,
    bridge_ws_endpoint,
    set_bridge_relay,
)

TOKEN = "test-token-123"


class FakeWS:
    """模拟 starlette WebSocket 被 relay 使用的 API 子集。

    - inbound 队列：测试侧 put 字符串 = 对端发来一帧；put None = 对端断开
    - sent 列表：捕获所有 send_text 的原始 JSON
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        item = await self.inbound.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code

    def frames(self) -> list[dict]:
        return [json.loads(s) for s in self.sent]


async def _wait_frame(ws: FakeWS, matcher, timeout: float = 2.0) -> dict:
    """轮询捕获帧直到 matcher 命中（避免 task 间事件时序假设）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        for s in ws.sent:
            try:
                f = json.loads(s)
            except json.JSONDecodeError:
                continue
            if matcher(f):
                return f
        if loop.time() >= deadline:
            raise AssertionError(f"等待帧超时: {ws.sent}")
        await asyncio.sleep(0.01)


def _hello(token: str = TOKEN) -> str:
    return json.dumps({"type": "hello", "token": token})


@pytest.mark.asyncio
async def test_hello_ok_and_roundtrip(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        await ws.inbound.put(_hello())
        ack = await _wait_frame(ws, lambda f: f.get("type") == "hello-ack")
        assert ack["ok"] is True
        assert relay.connected is True

        req_task = asyncio.create_task(relay.send_request("ping", {}, 5.0))
        req = await _wait_frame(ws, lambda f: f.get("type") == "req")
        assert req["op"] == "ping"
        assert req["payload"] == {}
        assert isinstance(req["id"], str)

        await ws.inbound.put(
            json.dumps({"type": "res", "id": req["id"], "status": 200, "result": {"pong": True}})
        )
        status, result = await asyncio.wait_for(req_task, 2.0)
        assert status == 200
        assert result == {"pong": True}

        # 对端断开 → connected 复位
        ws.inbound.put_nowait(None)
        await asyncio.wait_for(handler, 2.0)
        assert relay.connected is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_hello_bad_token_rejected(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        await ws.inbound.put(_hello("wrong-token"))
        await asyncio.wait_for(handler, 2.0)
        assert ws.close_code == CLOSE_TOKEN_REJECTED
        assert relay.connected is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_first_frame_not_hello_rejected(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        # 首帧不是 hello（直接发 req）→ 拒绝
        await ws.inbound.put(json.dumps({"type": "req", "id": "1", "op": "ping", "payload": {}}))
        await asyncio.wait_for(handler, 2.0)
        assert ws.close_code == CLOSE_TOKEN_REJECTED
        assert relay.connected is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_send_request_without_connection(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    with pytest.raises(BridgeWsLostError):
        await relay.send_request("ping", {}, 1.0)


@pytest.mark.asyncio
async def test_send_request_timeout(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        await ws.inbound.put(_hello())
        await _wait_frame(ws, lambda f: f.get("type") == "hello-ack")
        # 主进程不回应 → 等待超时
        with pytest.raises(BridgeWsLostError, match="timeout"):
            await relay.send_request("ssh_exec", {}, 0.2)
        ws.inbound.put_nowait(None)
        await asyncio.wait_for(handler, 2.0)
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_new_connection_replaces_old(monkeypatch):
    """新者胜：新连接 hello 后旧连接被关闭，旧连接在途请求失败（不双发前提）。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws1, ws2 = FakeWS(), FakeWS()
        h1 = asyncio.create_task(relay.handle_bridge_ws(ws1))
        await ws1.inbound.put(_hello())
        await _wait_frame(ws1, lambda f: f.get("type") == "hello-ack")

        # 旧连接上发出一条在途请求
        pending = asyncio.create_task(relay.send_request("ssh_exec", {"command": "ls"}, 5.0))
        req1 = await _wait_frame(ws1, lambda f: f.get("type") == "req")

        # 新连接接入并完成 hello
        h2 = asyncio.create_task(relay.handle_bridge_ws(ws2))
        await ws2.inbound.put(_hello())
        await _wait_frame(ws2, lambda f: f.get("type") == "hello-ack")

        assert ws1.close_code == CLOSE_REPLACED
        # 旧连接在途请求被失败化（绝不转移到新连接重发）
        with pytest.raises(BridgeWsLostError, match="replaced"):
            await pending
        assert relay.connected is True

        # 新连接正常工作，req 落到新 socket
        pend2 = asyncio.create_task(relay.send_request("ping", {}, 5.0))
        req2 = await _wait_frame(ws2, lambda f: f.get("type") == "req" and f.get("id") != req1["id"])
        await ws2.inbound.put(
            json.dumps({"type": "res", "id": req2["id"], "status": 200, "result": {}})
        )
        status, _ = await asyncio.wait_for(pend2, 2.0)
        assert status == 200

        ws1.inbound.put_nowait(None)
        ws2.inbound.put_nowait(None)
        await asyncio.wait_for(h1, 2.0)
        await asyncio.wait_for(h2, 2.0)
        assert relay.connected is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_stale_teardown_does_not_kill_new_connection_requests(monkeypatch):
    """竞态回归：旧连接被替换后真实断开，其 teardown 不得失败化新连接的在途请求。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws1, ws2 = FakeWS(), FakeWS()
        h1 = asyncio.create_task(relay.handle_bridge_ws(ws1))
        await ws1.inbound.put(_hello())
        await _wait_frame(ws1, lambda f: f.get("type") == "hello-ack")

        # 新连接接入并完成 hello（新者胜，ws1 被关闭标记）
        h2 = asyncio.create_task(relay.handle_bridge_ws(ws2))
        await ws2.inbound.put(_hello())
        await _wait_frame(ws2, lambda f: f.get("type") == "hello-ack")
        assert ws1.close_code == CLOSE_REPLACED

        # 现实场景：被替换的旧 socket 随即真正断开，h1 进入 teardown
        ws1.inbound.put_nowait(None)
        await asyncio.wait_for(h1, 2.0)
        assert relay.connected is True  # 新连接不受影响

        # teardown 完成后在新连接上发请求，必须正常往返
        pend2 = asyncio.create_task(relay.send_request("ssh_exec", {"command": "ls"}, 5.0))
        req2 = await _wait_frame(ws2, lambda f: f.get("type") == "req")
        await ws2.inbound.put(
            json.dumps({"type": "res", "id": req2["id"], "status": 200, "result": {"ok": True}})
        )
        status, result = await asyncio.wait_for(pend2, 2.0)
        assert status == 200
        assert result == {"ok": True}

        ws2.inbound.put_nowait(None)
        await asyncio.wait_for(h2, 2.0)
        assert relay.connected is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_wait_ready_event(monkeypatch):
    """wait_ready：未就绪时 await 事件，hello 后立即返回；断开后事件清空。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        # 未连接：预算内等不到 → False（且耗时受预算约束）
        assert await relay.wait_ready(0.05) is False

        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        # 并发等待与 hello 竞速：hello 完成后等待方应立即返回 True
        waiter = asyncio.create_task(relay.wait_ready(2.0))
        await asyncio.sleep(0.02)
        await ws.inbound.put(_hello())
        await _wait_frame(ws, lambda f: f.get("type") == "hello-ack")
        assert await asyncio.wait_for(waiter, 2.0) is True
        # 已就绪：同步立即返回
        assert await relay.wait_ready(0.0) is True

        # 断开：事件清空，再次等待超时返回 False
        ws.inbound.put_nowait(None)
        await asyncio.wait_for(handler, 2.0)
        assert await relay.wait_ready(0.05) is False
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_ping_pong(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    relay = BridgeRelay()
    set_bridge_relay(relay)
    try:
        ws = FakeWS()
        handler = asyncio.create_task(relay.handle_bridge_ws(ws))
        await ws.inbound.put(_hello())
        await _wait_frame(ws, lambda f: f.get("type") == "hello-ack")
        await ws.inbound.put(json.dumps({"type": "ping"}))
        await _wait_frame(ws, lambda f: f.get("type") == "pong")
        ws.inbound.put_nowait(None)
        await asyncio.wait_for(handler, 2.0)
    finally:
        set_bridge_relay(None)


@pytest.mark.asyncio
async def test_endpoint_without_relay_closes(monkeypatch):
    """lifespan 未启动（relay 为 None）时端点兜底：accept 后立即 1013。"""
    set_bridge_relay(None)
    ws = FakeWS()
    await bridge_ws_endpoint(ws)
    assert ws.accepted is True
    assert ws.close_code == CLOSE_TRY_LATER
