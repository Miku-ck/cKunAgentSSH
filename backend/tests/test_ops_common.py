"""run_headless fail-closed 回归测试（P0 安全修复）。

运维工具（ops_*）只允许远程 SSH 执行：
  - bridge 不可用（即 BRIDGE_PORT_CLOSED 故障态）→ 返回失败，绝不本机执行
  - 缺少 SSH 会话 → 返回失败
  - 正常态（session + bridge）→ 走远程，返回远程结果

回归守护：一旦有人把"本地子进程执行"分支加回 run_headless，
本测试会失败（旧分支会在本机执行 `cat /etc/passwd` 等命令）。
"""
import types

import pytest

from agent.electron_bridge import ElectronBridge
from agent.tools.builtin.ops_common import run_headless


def _ctx(ssh_session_id=""):
    return types.SimpleNamespace(
        ssh_session_id=ssh_session_id
    )


async def _fake_remote(self, **kwargs):
    return {"success": True, "output": "remote-ok", "error": "", "exit_code": 0}


@pytest.mark.asyncio
async def test_bridge_unavailable_refuses_local(monkeypatch):
    """bridge 不可用（故障态）→ 拒绝本机执行，明确 fail-closed"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: False)
    ok, out = await run_headless(_ctx(ssh_session_id="sess-1"), "cat /etc/passwd")
    assert ok is False
    assert "本机" in out          # 明确声明未在本机执行
    assert "fail-closed" in out   # 该拒绝属于 fail-closed 策略


@pytest.mark.asyncio
async def test_no_session_refuses_local(monkeypatch):
    """缺少 SSH 会话 → 拒绝本机执行"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    ok, out = await run_headless(_ctx(ssh_session_id=""), "ls /")
    assert ok is False
    assert "本机" in out


@pytest.mark.asyncio
async def test_remote_ok_no_local(monkeypatch):
    """正常态：session + bridge 可用 → 走远程，返回远程结果"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", _fake_remote)
    ok, out = await run_headless(_ctx(ssh_session_id="sess-1"), "df -h")
    assert ok is True
    assert "remote-ok" in out
