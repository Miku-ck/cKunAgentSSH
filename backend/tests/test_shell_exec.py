"""shell_exec fail-closed 回归测试（P1 安全修复）。

shell_exec 只允许远程 SSH 执行（经由 ElectronBridge 或可见终端交付）：
  - 缺少 SSH 会话 → 返回失败，绝不本机执行（fail-closed，与 run_headless 一致）
  - SSH 会话存在但 Bridge 不可用 → 远程分支明确拒绝，不降级本机
  - 正常态（session + bridge）→ 走远程，返回远程结果

回归守护：一旦有人把"本地子进程执行"分支加回 shell_exec.execute，
本测试会失败（旧分支会在本机执行命令）。
"""
import types

import pytest

from agent.electron_bridge import ElectronBridge
from agent.tools.builtin.shell_exec import ShellExecTool


def _ctx(ssh_session_id="", visible_terminal=False):
    return types.SimpleNamespace(
        ssh_session_id=ssh_session_id,
        visible_terminal=visible_terminal,
    )


def _make_tool():
    return ShellExecTool()


async def _fake_remote(self, **kwargs):
    return {"success": True, "output": "remote-ok", "error": "", "exit_code": 0}


@pytest.mark.asyncio
async def test_no_session_refuses_local(monkeypatch):
    """缺少 SSH 会话 → 拒绝本机执行，明确 fail-closed"""
    tool = _make_tool()
    result = await tool.execute(_ctx(ssh_session_id=""), "ls /")
    assert result.success is False
    # 失败消息按代码库约定落在 .error 字段（下游 build_error_feedback 呈现）
    assert "本机" in result.error
    assert "fail-closed" in result.error


@pytest.mark.asyncio
async def test_session_but_bridge_down_refuses(monkeypatch):
    """SSH 会话存在但 Bridge 不可用 → 远程分支拒绝，不降级本机"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: False)
    tool = _make_tool()
    result = await tool.execute(_ctx(ssh_session_id="sess-1"), "df -h")
    assert result.success is False
    assert "未就绪" in result.error  # _execute_remote 的 bridge 不可用提示


@pytest.mark.asyncio
async def test_remote_ok_no_local(monkeypatch):
    """正常态：session + bridge 可用 → 走远程，返回远程结果"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", _fake_remote)
    tool = _make_tool()
    result = await tool.execute(_ctx(ssh_session_id="sess-1"), "df -h")
    assert result.success is True
    assert "remote-ok" in result.output
