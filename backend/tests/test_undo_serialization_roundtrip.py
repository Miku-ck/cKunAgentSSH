"""undo 序列化往返回归（功能 B · run 级一键回滚）。

守护点：
- 三种 Undo 编解码保真：snapshot_b64 / existed / reverse_command=None 语义不丢；
- confidence 派生矩阵：existed=False→delete_created、reverse=None→manual、
  snapshot 缺失（existed=True 但无内容）→ manual（fail-closed）；
- 元字符路径不被 reverse 携带（saddle._shell_reverse 拒绝 → manual，序列化
  层不得把 None 反向"修复"成命令）；
- undo_from_dict 对残缺/未知 kind 返回 None（调用方不得当 NoOpUndo 放行）。
"""
import base64

from agent.harness.contracts import NoOpUndo, RunContext
from agent.harness.saddle import FileContentUndo, ShellUndo, UndoBuilder
from agent.harness.undo_serial import (
    CONF_DELETE_CREATED,
    CONF_DERIVED,
    CONF_MANUAL,
    CONF_SNAPSHOT,
    derive_confidence,
    snapshot_stack,
    undo_from_dict,
    undo_to_dict,
)


class _SshCtx:
    """snapshot_stack 只按鸭子类型读 ssh_session_id/environment/metadata。"""
    ssh_session_id = "sess-1"
    environment = "test"
    metadata = {"target_host": "web-01"}


# ── confidence 派生矩阵 ──

def test_confidence_matrix():
    b64 = base64.b64encode(b"hello").decode("ascii")
    assert derive_confidence(FileContentUndo(path="/a", ssh_session_id="s", snapshot_b64=b64, existed=True)) == CONF_SNAPSHOT
    # 0 字节文件的空串快照也是内容级证据（还原目标是空文件）
    assert derive_confidence(FileContentUndo(path="/a", ssh_session_id="s", snapshot_b64="", existed=True)) == CONF_SNAPSHOT
    assert derive_confidence(FileContentUndo(path="/a", ssh_session_id="s", snapshot_b64=None, existed=False)) == CONF_DELETE_CREATED
    # existed=True 却没有快照：内容级还原无从谈起，宁可少回滚不可错回滚
    assert derive_confidence(FileContentUndo(path="/a", ssh_session_id="s", snapshot_b64=None, existed=True)) == CONF_MANUAL
    assert derive_confidence(ShellUndo(command="mkdir /a", reverse_command="rmdir /a", ssh_session_id="s")) == CONF_DERIVED
    assert derive_confidence(ShellUndo(command="ls", reverse_command=None, ssh_session_id="s")) == CONF_MANUAL
    assert derive_confidence(NoOpUndo(reason="readonly")) == CONF_MANUAL
    # 未知形态 fail-closed
    assert derive_confidence(object()) == CONF_MANUAL


# ── 编解码保真 ──

def test_shell_roundtrip_none_reverse_stays_none():
    """reverse_command=None（无反向）往返后必须仍是 None，不许变空串/命令"""
    undo = ShellUndo(command="apt install -y x", reverse_command=None, ssh_session_id="s1")
    d = undo_to_dict(undo)
    assert d["kind"] == "shell"
    assert d["reverse_command"] is None
    assert d["command"] == "apt install -y x"
    restored = undo_from_dict({**d, "ssh_session_id": "s1"})
    assert isinstance(restored, ShellUndo)
    assert restored.reverse_command is None
    assert restored.command == "apt install -y x"


def test_file_roundtrip_snapshot_and_existed_semantics():
    b64 = base64.b64encode(b"cfg-content").decode("ascii")
    undo = FileContentUndo(path="/etc/app.conf", ssh_session_id="s1", snapshot_b64=b64, existed=True)
    d = undo_to_dict(undo)
    assert d["existed"] is True
    assert d["snapshot_b64"] == b64
    restored = undo_from_dict({**d, "ssh_session_id": "s1", "snapshot_b64": b64})
    assert isinstance(restored, FileContentUndo)
    assert base64.b64decode(restored.snapshot_b64) == b"cfg-content"
    assert restored.existed is True

    # existed=False（原文件不存在，反向 rm -f）：snapshot None 语义往返不丢
    undo2 = FileContentUndo(path="/tmp/new.txt", ssh_session_id="s1", snapshot_b64=None, existed=False)
    d2 = undo_to_dict(undo2)
    assert d2["existed"] is False and d2["snapshot_b64"] is None
    restored2 = undo_from_dict({**d2, "ssh_session_id": "s1", "snapshot_b64": None})
    assert isinstance(restored2, FileContentUndo)
    assert restored2.existed is False and restored2.snapshot_b64 is None


def test_noop_roundtrip_keeps_reason():
    d = undo_to_dict(NoOpUndo(reason="file_snapshot_unavailable"))
    assert d["kind"] == "noop" and d["reason"] == "file_snapshot_unavailable"
    restored = undo_from_dict(d)
    assert isinstance(restored, NoOpUndo)
    assert restored.reason == "file_snapshot_unavailable"


def test_unknown_kind_roundtrip_fails_closed():
    """未知 kind / 残缺行 → None，不得伪装成 NoOpUndo（语义是『只读无副作用』）"""
    assert undo_from_dict({"kind": "mystery"}) is None
    assert undo_from_dict({}) is None


def test_meta_char_path_yields_no_reverse_and_manual_confidence():
    """saddle 的元字符拒绝（B-5 加固）经序列化层不被破坏：reverse=None、manual"""
    cmd = "mkdir 'a; rm -rf /tmp'"
    reverse = UndoBuilder._shell_reverse(cmd)
    assert reverse is None  # 元字符路径在源头就没有反向命令
    undo = ShellUndo(command=cmd, reverse_command=reverse, ssh_session_id="s1")
    d = undo_to_dict(undo)
    assert d["reverse_command"] is None
    assert d["confidence"] == CONF_MANUAL


# ── snapshot_stack：栈快照的 seq/tool/host/environment 形态 ──

def test_snapshot_stack_rows_shape_and_order():
    ctx = RunContext.new(task="t", ssh_session_id="sess-1", environment="test")
    ctx.metadata["target_host"] = "web-01"
    ctx.tool_calls_log.append({"tool": "shell_exec", "args": {}, "iteration": 0})
    ctx.tool_calls_log.append({"tool": "file_write", "args": {}, "iteration": 1})
    ctx.undo_stack.append(ShellUndo(command="mkdir /a", reverse_command="rmdir /a", ssh_session_id="sess-1"))
    ctx.undo_stack.append(FileContentUndo(path="/f", ssh_session_id="sess-1",
                                          snapshot_b64=base64.b64encode(b"x").decode(), existed=True))
    rows = snapshot_stack(ctx)
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[0]["tool"] == "shell_exec"
    assert rows[1]["tool"] == "file_write"
    assert all(r["host"] == "web-01" for r in rows)
    assert all(r["environment"] == "test" for r in rows)
    assert rows[0]["confidence"] == CONF_DERIVED
    assert rows[1]["confidence"] == CONF_SNAPSHOT
    # 快照内容原样进入行 dict（落库层决定去留），不丢不转义
    assert rows[1]["snapshot_b64"] == base64.b64encode(b"x").decode()


def test_snapshot_stack_empty_stack_is_empty():
    assert snapshot_stack(RunContext.new(task="t")) == []
