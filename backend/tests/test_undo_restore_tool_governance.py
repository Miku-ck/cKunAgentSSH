"""undo_restore 治理契约回归（功能 B · run 级一键回滚）。

守护点（设计文档 §2.5）：
- **归属校验 fail-closed**：非回滚 run 调用（ctx.run_id 不在 run_rollbacks）→
  拒绝；(run_id, seq) 不属于登记的 source_run_id → 拒绝；
- existed=True 分块还原：首块 ``>`` 截断、后续 ``>>`` 追加、单块 b64
  ≤ 120_000 字符（MAX_ARG_STRLEN 防线），各块解码拼接后与原字节一致，
  ``wc -c`` 回验字节数一致（快照直接读库、不经模型）；
- 回验不符判失败（与 file_write 同一条判据）；
- existed=False → rm -f（删除新建文件）；
- meta.risk_level='dangerous'（进 gate dangerous 每次授权路径）且
  idempotent=False（不参与 Saddle 重试）；
- **快照内容不出现在工具 output/error（模型可见面）**；
- 成功还原后条目 status 翻 rolled_back。

调用形态说明：工具的执行入口经 ``UndoRestoreTool`` 类体别名提供（等价于
直接定义同名入口方法，见工具模块 docstring）；本测试直调
``tool._execute_impl``，另用恒等断言钉住别名接线（registry 经公开入口
调到的就是同一个函数）。
"""
import base64
import os

import pytest

import database as db
import undo_store
from agent.tools import ToolContext
from agent.tools.builtin.undo_restore import UndoRestoreTool, _MAX_B64_CHUNK_CHARS
import agent.tools.builtin.ops_common as ops_common
import agent.tools.builtin.ops_file as ops_file


@pytest.fixture
def undo_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "undo-restore.db"))
    yield
    db._close_all_connections()


def test_execute_alias_binds_to_impl():
    """别名接线：registry 经公开入口调到的就是 _execute_impl 本体。

    经类命名空间（vars）取属性做恒等断言，语义与「公开入口 is
    _execute_impl」完全一致；别名若断裂（改名/丢失）本断言与模块导入
    都会失败。"""
    assert vars(UndoRestoreTool)["execute"] is UndoRestoreTool._execute_impl


def _mk_source_run(run_id: str, rows: list[dict], run_status: str = "completed") -> None:
    conv_id = str(db.create_conversation(title="t", mode="ssh")["id"])
    db.create_agent_run(run_id, conv_id, task="t")
    db.update_agent_run(run_id, run_status)
    assert undo_store._record_rows_sync(run_id, rows, run_status) is True


def _register(source: str, rollback: str) -> None:
    """登记回滚关联。回滚 run 也必须有 agent_runs 行 —— run_rollbacks 双向挂 FK。"""
    conv_id = str(db.create_conversation(title="t", mode="ssh")["id"])
    db.create_agent_run(rollback, conv_id, task="t")
    undo_store.register_rollback(source, rollback)


def _file_row(seq: int, path: str, existed: bool, snapshot: bytes | None) -> dict:
    return {"seq": seq, "kind": "file", "tool": "file_write", "command": "",
            "reverse_command": None, "path": path,
            "snapshot_b64": base64.b64encode(snapshot).decode() if snapshot is not None else None,
            "existed": existed, "reason": None,
            "confidence": "snapshot" if existed else "delete_created",
            "ssh_session_id": "sess-old", "host": "web-01", "environment": "test"}


class _Capture:
    """收集 run_headless 收到的远端命令（这些命令经 SSH 直达目标机）。"""

    def __init__(self):
        self.commands: list[str] = []
        self.verify_calls: list[tuple[str, int]] = []

    async def run_headless(self, context, command, timeout=30, keep_full=False):
        self.commands.append(command)
        return True, ""

    async def verify_ok(self, context, path: str, expected: int, timeout: int):
        self.verify_calls.append((path, expected))
        return False, ""

    async def verify_mismatch(self, context, path: str, expected: int, timeout: int):
        self.verify_calls.append((path, expected))
        return True, f"写入后回验不符：远端 {path} 实际 1 字节，应为 {expected} 字节。"


@pytest.fixture
def tool():
    return UndoRestoreTool()


def _rollback_ctx(caller_run_id: str) -> ToolContext:
    return ToolContext(agent_run_id=caller_run_id, ssh_session_id="sess-1", conversation_id="conv-1")


# ── meta 治理面 ──

def test_meta_is_dangerous_and_non_idempotent(tool):
    """dangerous → gate.py:995-1000 每次授权弹卡；非幂等 → Saddle 不重试"""
    assert tool.name == "undo_restore"
    assert tool.meta.risk_level == "dangerous"
    assert tool.meta.idempotent is False
    assert tool.meta.reversible is True


# ── 归属校验 fail-closed ──

@pytest.mark.asyncio
async def test_reject_caller_not_registered_as_rollback(tool, undo_db):
    """调用 run 未登记为回滚 run → 拒绝（哪怕 (run_id,seq) 真实存在）"""
    cap = _Capture()
    _mk_source_run("src-1", [_file_row(0, "/etc/app.conf", True, b"cfg")])
    result = await tool._execute_impl(context=_rollback_ctx("not-a-rollback-run"), run_id="src-1", seq=0)
    assert result.success is False
    assert "未登记" in result.error
    assert cap.commands == []  # 什么都没执行


@pytest.mark.asyncio
async def test_reject_run_id_mismatch_with_registered_source(tool, undo_db):
    """登记的是 src-1 的回滚，却请求还原 src-2 的条目 → 拒绝"""
    _mk_source_run("src-1", [_file_row(0, "/etc/app.conf", True, b"cfg")])
    _mk_source_run("src-2", [_file_row(0, "/other.conf", True, b"x")])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-2", seq=0)
    assert result.success is False
    assert "不一致" in result.error


@pytest.mark.asyncio
async def test_reject_seq_outside_registered_source(tool, undo_db):
    """(run_id, seq) 不存在 → 拒绝"""
    _mk_source_run("src-1", [_file_row(0, "/etc/app.conf", True, b"cfg")])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=99)
    assert result.success is False
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_reject_shell_item(tool, undo_db):
    """shell 条目不属于本工具：提示按计划用 shell_exec 执行反向命令"""
    _mk_source_run("src-1", [{"seq": 0, "kind": "shell", "tool": "shell_exec",
                              "command": "mkdir /a", "reverse_command": "rmdir /a",
                              "path": None, "snapshot_b64": None, "existed": None,
                              "reason": None, "confidence": "derived",
                              "ssh_session_id": "s1", "host": "h1", "environment": "test"}])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is False
    assert "shell_exec" in result.error


@pytest.mark.asyncio
async def test_reject_without_ssh_session(tool, undo_db):
    _mk_source_run("src-1", [_file_row(0, "/f", True, b"cfg")])
    _register("src-1", "rbk-1")
    ctx = ToolContext(agent_run_id="rbk-1", ssh_session_id="")
    result = await tool._execute_impl(context=ctx, run_id="src-1", seq=0)
    assert result.success is False
    assert "SSH" in result.error


@pytest.mark.asyncio
async def test_reject_missing_snapshot(tool, undo_db):
    """existed=True 但快照缺失（落库超限被拒的降级行）→ fail-closed 拒绝"""
    _mk_source_run("src-1", [_file_row(0, "/f", True, None)])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is False
    assert "快照缺失" in result.error


# ── 分块还原 ──

@pytest.mark.asyncio
async def test_chunked_restore_two_chunks_then_verify(tool, undo_db, monkeypatch):
    """>90KB 快照 → 2 块：首块 > 截断、次块 >> 追加、单块 ≤120_000 b64 字符，
    各块解码拼接 = 原字节；wc -c 回验期望字节数一致后 status 翻 rolled_back"""
    cap = _Capture()
    monkeypatch.setattr(ops_common, "run_headless", cap.run_headless)
    monkeypatch.setattr(ops_file, "_verify_remote_write", cap.verify_ok)

    raw = bytes(range(256)) * 360  # 92_160 字节 → 2 块（90_000 + 2_160）
    _mk_source_run("src-1", [_file_row(0, "/etc/app.conf", True, raw)])
    _register("src-1", "rbk-1")

    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is True, result.error
    # 快照 b64 不出现在模型可见面（output/error）
    full_b64 = base64.b64encode(raw).decode()
    assert full_b64 not in result.output and full_b64 not in result.error

    # 命令形态：2 条还原 + 1 条回验（wc -c 由 _verify_remote_write 经 run_headless 发）
    restore_cmds = [c for c in cap.commands if "base64 -d" in c]
    assert len(restore_cmds) == 2
    first, second = restore_cmds
    assert " > /etc/app.conf" in first and " >> " not in first
    assert " >> /etc/app.conf" in second
    # 单块 b64 ≤ 120_000 字符（MAX_ARG_STRLEN 防线）
    extracted = []
    for c in restore_cmds:
        b64 = c.split("printf '%s' '")[1].split("' | base64 -d")[0]
        extracted.append(b64)
        assert len(b64) <= _MAX_B64_CHUNK_CHARS
        assert (len(b64) % 4) == 0
    # 各块解码拼接 = 原字节（首块截断 + 次块追加的净效果）
    assert base64.b64decode(extracted[0]) + base64.b64decode(extracted[1]) == raw
    # 回验拿到的是完整字节数
    assert cap.verify_calls == [("/etc/app.conf", len(raw))]
    # 条目状态翻 rolled_back
    assert undo_store.get_undo_actions("src-1")[0]["status"] == "rolled_back"
    # 成功输出只带路径与字节数，不带快照内容
    assert "/etc/app.conf" in result.output
    assert str(len(raw)) in result.output


@pytest.mark.asyncio
async def test_verify_mismatch_fails(tool, undo_db, monkeypatch):
    """wc -c 回验不符 → 判失败（不许把不确定冒充已验证）"""
    cap = _Capture()
    monkeypatch.setattr(ops_common, "run_headless", cap.run_headless)
    monkeypatch.setattr(ops_file, "_verify_remote_write", cap.verify_mismatch)
    _mk_source_run("src-1", [_file_row(0, "/f", True, b"cfg")])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is False
    assert "回验不符" in result.error


@pytest.mark.asyncio
async def test_chunk_command_failure_reports_position(tool, undo_db, monkeypatch):
    """第 2 块失败：如实报告块序号与残留风险，不谎称已还原"""
    cap = _Capture()

    async def fail_on_second(context, command, timeout=30, keep_full=False):
        cap.commands.append(command)
        if " >> " in command:
            return False, "bridge timeout"
        return True, ""

    monkeypatch.setattr(ops_common, "run_headless", fail_on_second)
    monkeypatch.setattr(ops_file, "_verify_remote_write", cap.verify_ok)
    raw = b"z" * 95_000
    _mk_source_run("src-1", [_file_row(0, "/f", True, raw)])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is False
    assert "第 2/" in result.error and "残留" in result.error


# ── existed=False → rm -f ──

@pytest.mark.asyncio
async def test_nonexistent_file_restored_by_delete(tool, undo_db, monkeypatch):
    """原文件不存在 → 反向是 rm -f，同一工具同一卡"""
    cap = _Capture()
    monkeypatch.setattr(ops_common, "run_headless", cap.run_headless)
    _mk_source_run("src-1", [_file_row(0, "/tmp/new.txt", existed=False, snapshot=None)])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is True, result.error
    assert cap.commands == ["rm -f /tmp/new.txt"]
    assert undo_store.get_undo_actions("src-1")[0]["status"] == "rolled_back"


@pytest.mark.asyncio
async def test_empty_snapshot_restores_empty_file(tool, undo_db, monkeypatch):
    """0 字节快照：首块（空）也要发出 —— 截断语义保证远端真成空文件"""
    cap = _Capture()
    monkeypatch.setattr(ops_common, "run_headless", cap.run_headless)
    monkeypatch.setattr(ops_file, "_verify_remote_write", cap.verify_ok)
    _mk_source_run("src-1", [_file_row(0, "/f", True, b"")])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is True
    restore_cmds = [c for c in cap.commands if "base64 -d" in c]
    assert len(restore_cmds) == 1
    assert cap.verify_calls == [("/f", 0)]


@pytest.mark.asyncio
async def test_mark_undone_failure_does_not_flip_success(tool, undo_db, monkeypatch):
    """远端还原已成功后 mark_undone 抛错（如 SQLite busy）：簿记失败不得把
    工具/审计/模型看到的结论翻成失败（否则模型会重试或误报）——仅告警。"""
    cap = _Capture()
    monkeypatch.setattr(ops_common, "run_headless", cap.run_headless)

    def _boom(rid, sq):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(undo_store, "mark_undone", _boom)
    _mk_source_run("src-1", [_file_row(0, "/f", True, b"cfg")])
    _register("src-1", "rbk-1")
    result = await tool._execute_impl(context=_rollback_ctx("rbk-1"), run_id="src-1", seq=0)
    assert result.success is True, result.error
    assert "已还原 /f" in result.output
    # 状态保持 pending（簿记未落），重复还原同一快照幂等无害
    assert undo_store.get_undo_actions("src-1")[0]["status"] == "pending"
