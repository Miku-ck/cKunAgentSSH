"""bridge kill/取消通道 — run 取消时终止远端命令的回归测试（C-review M9 落地）。

守护点：
- exec_ssh_command 携带 exec_id 时，payload 带上 execId（主进程据它登记 exec 通道）；
- kill_exec 走 ssh_kill op，payload 只含 execId；
- ops_common.exec_remote：正常完成不触发 kill；run 取消（CancelledError）时
  以同一 exec_id 先 kill 再传播取消；kill 本身失败只留痕、绝不吞取消；
- 取消传播语义：CancelledError 必须原样冒出（run 的 finally 清理依赖它）。

运行: python -m pytest backend/tests/test_bridge_exec_kill.py -v
"""
import asyncio

import pytest

from agent import bridge_relay
from agent.electron_bridge import BridgeError, BridgeErrorCode, ElectronBridge
from agent.tools.builtin import ops_common


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", "tok-123")
    return ElectronBridge()


class FakeRelay:
    def __init__(self, result=(200, {"success": True})):
        self.connected = True
        self._result = result
        self.calls: list[tuple] = []

    async def send_request(self, op, payload, timeout):
        self.calls.append((op, payload, timeout))
        return self._result

    async def wait_ready(self, timeout):
        return self.connected


def _use_relay(monkeypatch, relay):
    monkeypatch.setattr(bridge_relay, "get_bridge_relay", lambda: relay)


# ── 客户端层：execId 上行与 ssh_kill op ──────────────────────────────────────

@pytest.mark.asyncio
async def test_exec_payload_carries_exec_id(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "output": "ok"}))
    _use_relay(monkeypatch, relay)
    await env.exec_ssh_command("sess-1", "ls", exec_id="a" * 32)
    op, payload, _timeout = relay.calls[0]
    assert op == "ssh_exec"
    assert payload["execId"] == "a" * 32


@pytest.mark.asyncio
async def test_exec_without_exec_id_omits_field(env, monkeypatch):
    """不带 exec_id 的调用方（零改动面）payload 不出现该字段。"""
    relay = FakeRelay()
    _use_relay(monkeypatch, relay)
    await env.exec_ssh_command("sess-1", "ls")
    assert "execId" not in relay.calls[0][1]


@pytest.mark.asyncio
async def test_kill_exec_uses_ssh_kill_op(env, monkeypatch):
    relay = FakeRelay(result=(200, {"success": True, "found": True, "signaled": True}))
    _use_relay(monkeypatch, relay)
    out = await env.kill_exec("b" * 32)
    op, payload, _timeout = relay.calls[0]
    assert op == "ssh_kill"
    assert payload == {"execId": "b" * 32}
    assert out["signaled"] is True


# ── exec_remote：取消路径先 kill 再传播 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_run_kills_remote_command(monkeypatch):
    """run 取消 → 以发起时的同一 exec_id 调 kill_exec，然后取消继续传播。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", "tok-123")
    bridge = ElectronBridge()
    kills: list[str] = []

    async def fake_exec(ssh_session_id, command, timeout=30.0, exec_id=None):
        assert exec_id and len(exec_id) == 32
        kills.append("__pending__:" + exec_id)
        raise asyncio.CancelledError()

    async def fake_kill(exec_id, timeout=5.0):
        kills.append(exec_id)
        return {"found": True, "signaled": True, "pending": False}

    monkeypatch.setattr(bridge, "exec_ssh_command", fake_exec)
    monkeypatch.setattr(bridge, "kill_exec", fake_kill)
    monkeypatch.setattr(ops_common, "ElectronBridge", lambda: bridge)

    with pytest.raises(asyncio.CancelledError):
        await ops_common.exec_remote("sess-1", "sleep 999", 30)
    # kill 用的是发起 exec 的同一句柄，且发生在取消传播之前
    assert len(kills) == 2 and kills[1] == kills[0].split(":", 1)[1]


@pytest.mark.asyncio
async def test_kill_failure_does_not_swallow_cancellation(monkeypatch):
    """kill 通道本身失败（桥断开等）只留痕 —— 取消语义绝不被吞。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", "tok-123")
    bridge = ElectronBridge()

    async def fake_exec(ssh_session_id, command, timeout=30.0, exec_id=None):
        raise asyncio.CancelledError()

    async def fake_kill(exec_id, timeout=5.0):
        raise BridgeError(BridgeErrorCode.WS_LOST, "bridge gone")

    monkeypatch.setattr(bridge, "exec_ssh_command", fake_exec)
    monkeypatch.setattr(bridge, "kill_exec", fake_kill)
    monkeypatch.setattr(ops_common, "ElectronBridge", lambda: bridge)

    with pytest.raises(asyncio.CancelledError):
        await ops_common.exec_remote("sess-1", "sleep 999", 30)


@pytest.mark.asyncio
async def test_normal_completion_does_not_kill(monkeypatch):
    """正常完成的 exec 不触发 kill（kill 只属于取消路径）。"""
    monkeypatch.setenv("ELECTRON_BRIDGE_TOKEN", "tok-123")
    bridge = ElectronBridge()
    kill_calls: list[str] = []

    async def fake_exec(ssh_session_id, command, timeout=30.0, exec_id=None):
        return {"success": True, "output": "ok", "error": "", "exit_code": 0}

    async def fake_kill(exec_id, timeout=5.0):
        kill_calls.append(exec_id)
        return {"found": True, "signaled": True, "pending": False}

    monkeypatch.setattr(bridge, "exec_ssh_command", fake_exec)
    monkeypatch.setattr(bridge, "kill_exec", fake_kill)
    monkeypatch.setattr(ops_common, "ElectronBridge", lambda: bridge)

    ok, output, exit_code, err = await ops_common.exec_remote("sess-1", "ls", 30)
    assert ok is True and exit_code == 0 and err == ""
    assert kill_calls == []


@pytest.mark.asyncio
async def test_bridge_unavailable_returns_none_without_kill(monkeypatch):
    """桥未配置 → 返回 None（fail-closed 文案由调用方给），无任何 kill 尝试。"""
    monkeypatch.delenv("ELECTRON_BRIDGE_TOKEN", raising=False)
    assert await ops_common.exec_remote("sess-1", "ls", 30) is None


# ── 契约：exec_id 形态（electron 侧按 uuid4 hex 32 守卫）─────────────────────

@pytest.mark.asyncio
async def test_exec_id_is_uuid4_hex32(env, monkeypatch):
    """ops_common 生成的句柄必须是 32 位十六进制 —— electron 侧 dispatch
    按 ^[0-9a-f]{32}$ 守卫，形态漂移会直接 400。"""
    import re

    from agent.tools.builtin import ops_common as oc

    relay = FakeRelay(result=(200, {"success": True, "output": "ok", "exit_code": 0}))
    _use_relay(monkeypatch, relay)
    ok, _output, _exit_code, _err = await oc.exec_remote("sess-1", "ls", 30)
    assert ok is True
    payload = relay.calls[0][1]
    assert re.fullmatch(r"[0-9a-f]{32}", payload["execId"])
