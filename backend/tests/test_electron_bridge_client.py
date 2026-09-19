"""ElectronBridge — WS-only 传输语义回归（阶段 2 收尾）。

守护的核心语义：
- WS 已连接 → 走 WS，业务请求正常往返
- WS 未连接 → 短等待后 fail-closed（探活返回 WS_LOST）
- WS 在途断线 → fail-closed 抛 WS_LOST，绝不重发同一请求（远程命令非幂等）
- WS 4xx/5xx → 映射为 HTTP_ERROR（业务失败语义）
- env 未注入 token → NOT_CONFIGURED
"""
import pytest

from agent import bridge_relay
from agent.bridge_relay import BridgeWsLostError
from agent.electron_bridge import (
    BridgeError,
    BridgeErrorCode,
    ElectronBridge,
)

TOKEN = "tok-123"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", TOKEN)
    return ElectronBridge()


class FakeRelay:
    def __init__(self, connected: bool = True, result=(200, {"success": True}), lost: bool = False):
        self.connected = connected
        self._result = result
        self._lost = lost
        self.calls: list[tuple] = []

    async def send_request(self, op, payload, timeout):
        self.calls.append((op, payload, timeout))
        if self._lost:
            raise BridgeWsLostError("simulated drop")
        return self._result

    async def wait_ready(self, timeout):
        return self.connected


def _use_relay(monkeypatch, relay):
    monkeypatch.setattr(bridge_relay, "get_bridge_relay", lambda: relay)


@pytest.mark.asyncio
async def test_ws_request_roundtrip(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "output": "ok"}))
    _use_relay(monkeypatch, relay)
    out = await env.exec_ssh_command("sess-1", "ls")
    assert out == {"success": True, "output": "ok"}
    op, payload, timeout = relay.calls[0]
    assert op == "ssh_exec"
    assert payload["sshSessionId"] == "sess-1"
    assert payload["command"] == "ls"
    # 超时语义：SSH 超时 + 5s 余量
    assert timeout == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_connect_and_disconnect_ops(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "sessionId": "s9", "name": "n"}))
    _use_relay(monkeypatch, relay)
    await env.connect_ssh("conn-1")
    await env.disconnect_ssh("s9")
    assert relay.calls[0][0] == "ssh_connect"
    assert relay.calls[0][1] == {"connectionId": "conn-1"}
    assert relay.calls[1][0] == "ssh_disconnect"
    assert relay.calls[1][1] == {"sessionId": "s9"}


@pytest.mark.asyncio
async def test_probe_ws_connected_returns_none(env, monkeypatch):
    _use_relay(monkeypatch, FakeRelay(connected=True))
    assert await env.probe() is None
    assert await env.ping() is True


@pytest.mark.asyncio
async def test_probe_ws_not_connected_returns_ws_lost(env, monkeypatch):
    """WS 未连接 → 短等待后 fail-closed 返回 WS_LOST（HTTP 兜底已移除）。"""
    _use_relay(monkeypatch, FakeRelay(connected=False))
    monkeypatch.setattr("agent.electron_bridge._WS_PROBE_WAIT_S", 0.1)
    assert await env.probe() is BridgeErrorCode.WS_LOST
    assert await env.ping() is False


@pytest.mark.asyncio
async def test_inflight_ws_lost_no_retry(env, monkeypatch):
    """WS 在途断线 → fail-closed 抛 WS_LOST，绝不重发（防双执行）。"""
    _use_relay(monkeypatch, FakeRelay(connected=True, lost=True))
    with pytest.raises(BridgeError) as ei:
        await env.exec_ssh_command("sess-1", "reboot")
    assert ei.value.code is BridgeErrorCode.WS_LOST


@pytest.mark.asyncio
async def test_ws_4xx_maps_http_error(env, monkeypatch):
    _use_relay(
        monkeypatch,
        FakeRelay(result=(400, {"success": False, "error": "Missing command"})),
    )
    with pytest.raises(BridgeError) as ei:
        await env.exec_ssh_command("sess-1", "")
    assert ei.value.code is BridgeErrorCode.HTTP_ERROR
    assert "Missing command" in str(ei.value)


@pytest.mark.asyncio
async def test_probe_not_configured(monkeypatch):
    """env 未注入 → 不碰任何通道，直接 NOT_CONFIGURED（既有行为回归）。"""
    monkeypatch.delenv("ELECTRON_BRIDGE_TOKEN", raising=False)
    b = ElectronBridge()
    assert await b.probe() is BridgeErrorCode.NOT_CONFIGURED
