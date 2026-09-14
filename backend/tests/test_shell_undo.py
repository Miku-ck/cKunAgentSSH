"""ShellUndo.apply_reverse 执行隔离回归测试。

背景：原实现 reverse 恒在后端主机本地 create_subprocess_shell 执行，
SSH 模式下原命令跑在远端、回滚却"打错机器"（误删本地同名路径）。

现策略（仅远程，与 shell_exec fail-closed 对齐）：
  - 有 SSH 会话 → 反向命令经 ElectronBridge 下发到同一会话执行
  - Bridge 不可用 → 跳过，绝不降级到本机执行
  - 无 SSH 会话 → 跳过（shell_exec 本身拒绝、原命令从未执行；反向命令
    绝不允许在后端主机运行 —— 原本地回滚分支已删除）

回归守护：防止有人把 apply_reverse 改回"本地执行反向命令"。
"""
import pytest

from agent.electron_bridge import ElectronBridge
from agent.harness.saddle import ShellUndo


async def _fake_bridge_exec(self, **kwargs):
    _fake_bridge_exec.calls.append(kwargs)
    return {"success": True, "output": "", "error": "", "exit_code": 0}


_fake_bridge_exec.calls = []


def _make_shell_recorder():
    calls = []

    async def _fake_shell(cmd, **kwargs):
        calls.append(cmd)
        return None

    return calls, _fake_shell


@pytest.mark.asyncio
async def test_ssh_reverse_routes_via_bridge(monkeypatch):
    """SSH 会话 → 反向命令经 bridge 下发到同一会话，绝不本机执行"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", _fake_bridge_exec)
    _fake_bridge_exec.calls = []
    shell_calls, fake_shell = _make_shell_recorder()
    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_shell)

    undo = ShellUndo(
        command="mkdir /tmp/x",
        reverse_command="rmdir /tmp/x",
        ssh_session_id="sess-1",
    )
    await undo.apply_reverse()

    assert len(_fake_bridge_exec.calls) == 1
    call = _fake_bridge_exec.calls[0]
    assert call["ssh_session_id"] == "sess-1"
    assert call["command"] == "rmdir /tmp/x"
    assert call["timeout"] == 10.0
    # 关键：未在本机起任何子进程
    assert shell_calls == []


@pytest.mark.asyncio
async def test_ssh_reverse_bridge_down_skips_not_local(monkeypatch):
    """Bridge 不可用 → 跳过回滚，绝不降级到本机执行（fail-closed 精神）"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: False)
    shell_calls, fake_shell = _make_shell_recorder()
    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_shell)

    undo = ShellUndo(
        command="mkdir /tmp/x",
        reverse_command="rmdir /tmp/x",
        ssh_session_id="sess-1",
    )
    await undo.apply_reverse()  # 不应抛异常

    assert shell_calls == []


@pytest.mark.asyncio
async def test_no_session_reverse_skips_and_never_local(monkeypatch):
    """无 SSH 会话 → 原命令从未执行（shell_exec 已拒绝），反向命令绝不在后端主机运行。

    回归守护：undo 在 PRE 构建、execute 拒绝后才触发 apply_reverse，
    曾经的本地回滚分支会让拒绝路径的 rmdir 真实跑在后端主机上，
    绕过 shell_exec 的本机执行禁令 —— 该分支已删除。
    """
    shell_calls, fake_shell = _make_shell_recorder()
    monkeypatch.setattr("asyncio.create_subprocess_shell", fake_shell)
    bridge_calls = []

    async def spy_bridge(self, **kwargs):
        bridge_calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", spy_bridge)

    undo = ShellUndo(
        command="mkdir /tmp/x",
        reverse_command="rmdir /tmp/x",
        ssh_session_id="",
    )
    await undo.apply_reverse()

    assert shell_calls == []
    assert bridge_calls == []
