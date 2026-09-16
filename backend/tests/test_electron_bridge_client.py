"""ElectronBridge — WS 优先 / HTTP 兜底传输选择回归（阶段 1）。

守护的核心语义：
- WS 已连接 → 走 WS，绝不打 HTTP
- WS 未连接 → 短等待后回落 HTTP（现有行为）
- WS 在途断线 → fail-closed 抛 WS_LOST，绝不回 HTTP 重发同一请求
- WS 4xx/5xx → 映射为 HTTP_ERROR（与 HTTP 通道语义对齐）
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
    monkeypatch.setenv("ELECTRON_BRIDGE_PORT", "30000")
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


def _forbid_http(monkeypatch):
    """守卫：HTTP 兜底通道一旦被调用即失败（测试期望走 WS）。"""

    def _boom_client(self, timeout):
        raise AssertionError("HTTP 兜底通道被调用（测试期望走 WS）")

    async def _boom_post(self, path, payload, http_timeout):
        raise AssertionError("HTTP 兜底通道被调用（测试期望走 WS）")

    async def _boom_probe(self, timeout):
        raise AssertionError("HTTP 探活被调用（测试期望 WS 已连接）")

    monkeypatch.setattr(ElectronBridge, "_new_client", _boom_client)
    monkeypatch.setattr(ElectronBridge, "_post", _boom_post)
    monkeypatch.setattr(ElectronBridge, "_probe_http", _boom_probe)


@pytest.mark.asyncio
async def test_ws_preferred_when_connected(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "output": "ok"}))
    _use_relay(monkeypatch, relay)
    _forbid_http(monkeypatch)
    out = await env.exec_ssh_command("sess-1", "ls")
    assert out == {"success": True, "output": "ok"}
    op, payload, timeout = relay.calls[0]
    assert op == "ssh_exec"
    assert payload["sshSessionId"] == "sess-1"
    assert payload["command"] == "ls"
    # 超时语义：SSH 超时 + 5s 余量（与 HTTP 通道一致）
    assert timeout == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_connect_and_disconnect_ops(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "sessionId": "s9", "name": "n"}))
    _use_relay(monkeypatch, relay)
    _forbid_http(monkeypatch)
    await env.connect_ssh("conn-1")
    await env.disconnect_ssh("s9")
    assert relay.calls[0][0] == "ssh_connect"
    assert relay.calls[0][1] == {"connectionId": "conn-1"}
    assert relay.calls[1][0] == "ssh_disconnect"
    assert relay.calls[1][1] == {"sessionId": "s9"}


@pytest.mark.asyncio
async def test_probe_ws_connected_returns_none(env, monkeypatch):
    _use_relay(monkeypatch, FakeRelay(connected=True))
    _forbid_http(monkeypatch)
    assert await env.probe() is None
    assert await env.ping() is True


@pytest.mark.asyncio
async def test_probe_falls_back_to_http_after_short_wait(env, monkeypatch):
    """WS 未连接 → 等短预算后回落 HTTP 探活（不等待真实 5s）。"""
    _use_relay(monkeypatch, FakeRelay(connected=False))
    # 模块级常量：按模块路径 patch，缩短真实 1s 等待为 0.1s
    monkeypatch.setattr("agent.electron_bridge._WS_PROBE_WAIT_S", 0.1)
    probed: list[float] = []

    async def _fake_probe(self, timeout):
        probed.append(timeout)
        return BridgeErrorCode.PORT_CLOSED

    monkeypatch.setattr(ElectronBridge, "_probe_http", _fake_probe)
    result = await env.probe(timeout=3.0)
    assert result is BridgeErrorCode.PORT_CLOSED
    assert probed == [3.0]


@pytest.mark.asyncio
async def test_inflight_ws_lost_no_http_retry(env, monkeypatch):
    """WS 在途断线 → fail-closed 抛 WS_LOST，绝不回 HTTP 重发（防双执行）。"""
    _use_relay(monkeypatch, FakeRelay(connected=True, lost=True))
    _forbid_http(monkeypatch)
    with pytest.raises(BridgeError) as ei:
        await env.exec_ssh_command("sess-1", "reboot")
    assert ei.value.code is BridgeErrorCode.WS_LOST


@pytest.mark.asyncio
async def test_ws_4xx_maps_http_error(env, monkeypatch):
    _use_relay(
        monkeypatch,
        FakeRelay(result=(400, {"success": False, "error": "Missing command"})),
    )
    _forbid_http(monkeypatch)
    with pytest.raises(BridgeError) as ei:
        await env.exec_ssh_command("sess-1", "")
    assert ei.value.code is BridgeErrorCode.HTTP_ERROR
    assert "Missing command" in str(ei.value)


@pytest.mark.asyncio
async def test_probe_not_configured(monkeypatch):
    """env 未注入 → 不碰任何通道，直接 NOT_CONFIGURED（既有行为回归）。"""
    monkeypatch.delenv("ELECTRON_BRIDGE_PORT", raising=False)
    monkeypatch.delenv("ELECTRON_BRIDGE_TOKEN", raising=False)
    b = ElectronBridge()
    assert await b.probe() is BridgeErrorCode.NOT_CONFIGURED
