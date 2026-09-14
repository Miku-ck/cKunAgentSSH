"""FileContentUndo 回归测试（P3 预变更快照，仅限远程）。

守护点：
- file_write / file_edit 的回滚从 NoOp 升级为内容级还原；
- 远端已存在文件 → 快照 base64，reverse 经 ElectronBridge 写回原内容（绝不降级本机）；
- 远端原文件不存在 → reverse 用 rm -f 删除新建文件；
- 桥/会话不可用导致快照失败 → 返回 NoOpUndo（真实写入也会失败，无副作用可撤销）；
- 无 SSH 会话 → 构建即 NoOp、还原即跳过（工具 execute 会 fail-closed 拒绝，
  后端主机绝不被撤销逻辑触碰 —— 原本地快照/还原分支已随 workspace 概念清除）。
"""
import base64

import pytest

from agent.electron_bridge import ElectronBridge
from agent.harness.saddle import FileContentUndo, NoOpUndo, UndoBuilder
import agent.tools.builtin.ops_common as ops_common


async def _fake_run_headless_ok(context, command, timeout=30):
    """返回一段 base64 内容（b"hello world"）"""
    b64 = base64.b64encode(b"hello world").decode("ascii")
    return True, f"{b64}\n[exit_code=0]"


async def _fake_run_headless_missing(context, command, timeout=30):
    return False, "base64: /tmp/nope: No such file or directory\n[exit_code=1]"


async def _fake_run_headless_unavailable(context, command, timeout=30):
    return False, (
        "拒绝执行（fail-closed）：Electron Bridge 未就绪，"
        "本命令不会降级到后端主机本机执行。"
    )


async def _fake_bridge_exec(self, **kwargs):
    _fake_bridge_exec.calls.append(kwargs)
    return {"success": True, "output": "", "error": "", "exit_code": 0}


_fake_bridge_exec.calls = []


class _SshCtx:
    ssh_session_id = "sess-1"


class _NoSessionCtx:
    ssh_session_id = ""


@pytest.mark.asyncio
async def test_remote_snapshot_then_restore_via_bridge(monkeypatch):
    """远端已存在文件 → 快照 base64，reverse 经 bridge 写回原内容"""
    monkeypatch.setattr(ops_common, "run_headless", _fake_run_headless_ok)
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", _fake_bridge_exec)
    _fake_bridge_exec.calls = []

    undo = await UndoBuilder._build_file_undo(
        "file_write", {"path": "/tmp/x.txt"}, _SshCtx()
    )
    assert isinstance(undo, FileContentUndo)
    assert undo.ssh_session_id == "sess-1"
    assert undo.existed is True
    assert undo.snapshot_b64 is not None

    await undo.apply_reverse()
    assert len(_fake_bridge_exec.calls) == 1
    call = _fake_bridge_exec.calls[0]
    assert call["ssh_session_id"] == "sess-1"
    assert call["command"].startswith("printf '%s' '")
    assert call["command"].endswith("| base64 -d > /tmp/x.txt")
    # command = "printf '%s' '<b64>' | base64 -d > /tmp/x.txt" → b64 在第二个引号段
    seg = call["command"].split("'")[3]
    assert base64.b64decode(seg) == b"hello world"


@pytest.mark.asyncio
async def test_remote_new_file_reverse_is_remove(monkeypatch):
    """远端原文件不存在 → reverse 用 rm -f 删除新建的文件"""
    monkeypatch.setattr(ops_common, "run_headless", _fake_run_headless_missing)
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: True)
    monkeypatch.setattr(ElectronBridge, "exec_ssh_command", _fake_bridge_exec)
    _fake_bridge_exec.calls = []

    undo = await UndoBuilder._build_file_undo(
        "file_write", {"path": "/tmp/new.txt"}, _SshCtx()
    )
    assert isinstance(undo, FileContentUndo)
    assert undo.existed is False
    assert undo.snapshot_b64 is None

    await undo.apply_reverse()
    assert _fake_bridge_exec.calls[0]["command"] == "rm -f /tmp/new.txt"


@pytest.mark.asyncio
async def test_remote_snapshot_unavailable_returns_noop(monkeypatch):
    """桥/会话不可用导致快照失败 → 返回 NoOpUndo（真实写入也会失败，无副作用可撤销）"""
    monkeypatch.setattr(ops_common, "run_headless", _fake_run_headless_unavailable)

    undo = await UndoBuilder._build_file_undo(
        "file_write", {"path": "/tmp/x.txt"}, _SshCtx()
    )
    assert isinstance(undo, NoOpUndo)


@pytest.mark.asyncio
async def test_remote_reverse_bridge_down_skips(monkeypatch):
    """远程还原时 Bridge 不可用 → 安全跳过，不抛异常、不降级本机"""
    monkeypatch.setattr(ElectronBridge, "is_available", lambda self: False)
    undo = FileContentUndo(
        path="/tmp/x.txt", ssh_session_id="sess-9",
        snapshot_b64=base64.b64encode(b"data").decode("ascii"), existed=True,
    )
    await undo.apply_reverse()  # 不应抛异常


@pytest.mark.asyncio
async def test_no_session_build_returns_noop():
    """无 SSH 会话 → 快照构建直接 NoOp（不读不写后端主机文件系统）"""
    undo = await UndoBuilder._build_file_undo(
        "file_write", {"path": "C:/Windows/win.ini"}, _NoSessionCtx()
    )
    assert isinstance(undo, NoOpUndo)
    assert undo.reason == "file_write_requires_ssh"


@pytest.mark.asyncio
async def test_no_session_reverse_skips_without_touching_local_fs(tmp_workspace):
    """无会话的 FileContentUndo apply_reverse 直接跳过：绝不本地还原/删除"""
    from pathlib import Path
    target = Path(tmp_workspace) / "keep.txt"
    target.write_text("do not touch")

    undo = FileContentUndo(
        path=str(target), ssh_session_id="",
        snapshot_b64=None, existed=False,  # 若误走本地"删除新建"分支会删掉该文件
    )
    await undo.apply_reverse()
    assert target.exists() and target.read_text() == "do not touch"
