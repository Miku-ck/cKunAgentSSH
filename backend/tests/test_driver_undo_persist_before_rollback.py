"""driver._emit_done 持久化时序回归（功能 B · run 级一键回滚）。

守护最高危时序（设计文档 §2.1 结论 2 / §2.5）：
- **FAILED run 下 record 发生在 undo_all 之前**——FAILED 分支会把栈执行并
  清空（stirrups.undo_all 尾部 clear），若序列化排在它之后只能拿到空栈；
  本用例以「FAILED 后 undo 行仍完整落库且 status=auto_rolled_back」钉死顺序；
- COMPLETED run 条目 status='pending'；
- 持久化抛异常时 run 主流程不受影响（done 事件照发、undo_recorded=False）；
- done payload 透传 undo_recorded 布尔。
"""
import base64
import os

import pytest

from agent.harness.contracts import RunContext, State, TraceEvent
from agent.harness.driver import Driver
from agent.harness.saddle import FileContentUndo, ShellUndo

import database as db
import undo_store


@pytest.fixture
def undo_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "driver-undo.db"))
    yield
    db._close_all_connections()


class _RecordingStirrups:
    """复刻 stirrups.undo_all 的既有语义：执行 + clear，并记录调用时刻。"""

    def __init__(self):
        self.calls: list[int] = []

    async def undo_all(self, ctx) -> None:
        self.calls.append(len(ctx.undo_stack))
        ctx.undo_stack.clear()


class _StubHarness:
    def __init__(self):
        self.stirrups = _RecordingStirrups()
        self.cost_tracker = None


class _CollectTelemetry:
    def __init__(self):
        self.events: list[TraceEvent] = []

    async def emit(self, ev: TraceEvent) -> None:
        self.events.append(ev)


def _mk_run(run_id: str) -> None:
    """建 agent_runs 行（conversations 先行：agent_runs.conversation_id 挂 FK）。
    headless run 不经 create_agent_run —— 无此行的 run 由存在性闸跳过（另测）。"""
    conv_id = str(db.create_conversation(title="t", mode="ssh")["id"])
    db.create_agent_run(run_id, conv_id, task="t")


def _mk_ctx() -> RunContext:
    ctx = RunContext.new(task="t", conversation_id="conv-1", ssh_session_id="sess-1", environment="test")
    ctx.metadata["target_host"] = "web-01"
    ctx.run_id = "run-driver-1"
    ctx.undo_stack.append(ShellUndo(command="mkdir /tmp/a", reverse_command="rmdir /tmp/a", ssh_session_id="sess-1"))
    ctx.undo_stack.append(
        FileContentUndo(path="/etc/app.conf", ssh_session_id="sess-1",
                        snapshot_b64=base64.b64encode(b"cfg").decode("ascii"), existed=True))
    return ctx


async def _run_done(final_state: State, *, with_db_row: bool = True) -> tuple[Driver, RunContext, _StubHarness, _CollectTelemetry, TraceEvent]:
    driver = Driver()
    ctx = _mk_ctx()
    if with_db_row:
        _mk_run(ctx.run_id)
    harness = _StubHarness()
    telemetry = _CollectTelemetry()
    evs = [ev async for ev in driver._emit_done(telemetry, ctx, 3, final_state, "内容", "", harness)]
    return driver, ctx, harness, telemetry, evs[-1]


@pytest.mark.asyncio
async def test_failed_run_records_before_undo_all(undo_db):
    """FAILED：record 先于 undo_all —— 栈被清空前快照，条目 auto_rolled_back"""
    _, ctx, harness, _, done = await _run_done(State.FAILED)
    # undo_all 确实执行了（原语义不变），且执行时栈里是完整的 2 条
    assert harness.stirrups.calls == [2]
    assert ctx.undo_stack == []
    # 落库发生在清空前：2 条都在
    rows = undo_store.get_undo_actions("run-driver-1")
    assert [r["seq"] for r in rows] == [0, 1]
    assert all(r["status"] == "auto_rolled_back" for r in rows)
    assert all(r["run_status"] == "failed" for r in rows)
    assert done.payload["undo_recorded"] is True


@pytest.mark.asyncio
async def test_completed_run_rows_pending(undo_db):
    """COMPLETED：条目 status='pending'，undo_all 不触发"""
    _, ctx, harness, _, done = await _run_done(State.COMPLETED)
    assert harness.stirrups.calls == []
    assert len(ctx.undo_stack) == 2
    rows = undo_store.get_undo_actions("run-driver-1")
    assert [r["seq"] for r in rows] == [0, 1]
    assert all(r["status"] == "pending" for r in rows)
    assert done.payload["undo_recorded"] is True


@pytest.mark.asyncio
async def test_persistence_failure_does_not_block_done(undo_db, monkeypatch):
    """持久化抛异常：done 事件照发、undo_recorded=False、FAILED 的 undo_all 照常"""
    async def _boom(ctx, final_state):
        raise RuntimeError("db gone")

    monkeypatch.setattr(undo_store, "record_run_undo", _boom)
    driver = Driver()
    ctx = _mk_ctx()
    harness = _StubHarness()
    telemetry = _CollectTelemetry()
    evs = [ev async for ev in driver._emit_done(telemetry, ctx, 1, State.FAILED, "内容", "", harness)]
    assert evs and evs[-1].kind == "done"
    assert evs[-1].payload["undo_recorded"] is False
    assert harness.stirrups.calls == [2]
    assert ctx.undo_stack == []
    # 没有任何行落库
    assert undo_store.get_undo_actions("run-driver-1") == []


@pytest.mark.asyncio
async def test_done_event_still_emitted_without_agent_runs_row(undo_db, caplog):
    """headless run（无 agent_runs 行）在 driver 收尾时优雅跳过，done 照发"""
    ctx = _mk_ctx()
    ctx.run_id = "run-headless-ghost"  # 不建 agent_runs 行
    driver = Driver()
    harness = _StubHarness()
    telemetry = _CollectTelemetry()
    evs = [ev async for ev in driver._emit_done(telemetry, ctx, 1, State.COMPLETED, "内容", "", harness)]
    assert evs and evs[-1].payload["undo_recorded"] is False
    assert harness.stirrups.calls == []
