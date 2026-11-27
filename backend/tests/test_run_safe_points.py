from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from agent.harness.contracts import AgentConfig, RunContext, State, ToolCall, ToolResult
from agent.harness.driver import Driver
from agent.harness.safe_point import (
    SafePoint, safe_point_for_run, snapshot_context, validate_message_pairs,
)
from db import run_safe_points as store


@pytest.fixture
def safe_db(tmp_workspace):
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "safe-points.db"
    dbpkg._close_all_connections()
    dbpkg.init_db(str(path))
    store._reset_memo_for_tests()
    try:
        yield SafePoint()
    finally:
        dbpkg._close_all_connections()
        if original:
            dbpkg.init_db(original)
        dbpkg._close_all_connections()
        store._reset_memo_for_tests()


def _ctx():
    return RunContext(
        run_id="run-safe", task="long task", conversation_id="conv-safe",
        system_prompt="system", history=[{"role": "user", "content": "old"}],
        memories=["memory"], config=AgentConfig(), llm_model="model-a",
        ssh_session_id="session-a", environment="prod",
    )


def test_run_safe_point_requires_database_unless_explicitly_enabled():
    assert isinstance(safe_point_for_run(enabled=None, db_path="app.db"), SafePoint)
    assert safe_point_for_run(enabled=False, db_path="app.db") is None
    assert safe_point_for_run(enabled=None, db_path="") is None
    assert isinstance(safe_point_for_run(enabled=True, db_path=""), SafePoint)


@pytest.mark.asyncio
async def test_safe_point_preserves_all_messages_without_truncation(safe_db):
    ctx = _ctx()
    ctx.messages = [
        {"role": "user", "content": "x" * 70000},
        *[{"role": "user", "content": f"message-{i}"} for i in range(30)],
    ]

    await safe_db.save(ctx, "iteration")
    loaded = store.load_safe_point(ctx.run_id)

    assert loaded["version"] == 1
    assert loaded["phase"] == "iteration"
    assert loaded["payload"]["messages"] == ctx.messages
    assert len(loaded["payload"]["messages"]) == 31


@pytest.mark.asyncio
async def test_safe_point_roundtrip_keeps_tool_message_pairs(safe_db):
    ctx = _ctx()
    ctx.messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "file_read", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    ]

    await safe_db.save(ctx, "after_tool")
    payload = store.load_safe_point(ctx.run_id)["payload"]
    validate_message_pairs(payload["messages"])
    assert payload["iteration"] == 0
    assert payload["state"] == "init"
    assert payload["ssh_session_id"] == "session-a"


@pytest.mark.asyncio
async def test_safe_point_redacts_secrets_and_runtime_services(safe_db):
    ctx = _ctx()
    ctx.config.fallback_model_cfg = {"api_key": "secret-key", "model_id": "backup"}
    ctx.config.max_total_tokens = 123
    ctx.metadata.update({
        "cost_tracker": object(), "saddle": object(), "emit_event": object(),
        "plain": "kept", "access_token": "secret-token",
        "cost_snapshot": {"total_tokens": 150},
    })

    await safe_db.save(ctx, "iteration")
    payload = store.load_safe_point(ctx.run_id)["payload"]

    assert payload["config"]["fallback_model_cfg"]["api_key"] == "[redacted]"
    assert payload["config"]["max_total_tokens"] == 123
    assert payload["metadata"]["plain"] == "kept"
    assert payload["metadata"]["access_token"] == "[redacted]"
    assert payload["metadata"]["cost_snapshot"]["total_tokens"] == 150
    assert not {"cost_tracker", "saddle", "emit_event"} & payload["metadata"].keys()


@pytest.mark.asyncio
async def test_stale_safe_point_writer_cannot_overwrite_newer_snapshot(safe_db):
    ctx = _ctx()
    first = await safe_db.save(ctx, "before_tool")
    second = await safe_db.save(ctx, "after_tool")

    assert first["version"] == 1
    assert second["version"] == 2
    stale = store.save_safe_point(
        ctx.run_id, expected_version=1, phase="old", payload={"messages": []},
    )
    assert stale is None
    assert store.load_safe_point(ctx.run_id)["phase"] == "after_tool"


def test_snapshot_excludes_process_monotonic_and_keeps_wall_origin():
    ctx = _ctx()
    ctx.config.max_wall_clock_s = 60
    ctx.metadata["run_started_mono"] = 123.5
    payload = snapshot_context(ctx, "iteration")
    assert "run_started_mono" not in payload["metadata"]
    assert payload["run_started_unix_s"] > 0


def test_snapshot_keeps_original_wall_origin_across_checkpoints(monkeypatch):
    import agent.harness.safe_point as safe_point_mod

    ctx = _ctx()
    ctx.metadata["run_started_mono"] = 100.0
    monkeypatch.setattr(safe_point_mod.time, "monotonic", lambda: 130.0)
    monkeypatch.setattr(safe_point_mod.time, "time", lambda: 1000.0)
    first = snapshot_context(ctx, "before_tool")

    monkeypatch.setattr(safe_point_mod.time, "monotonic", lambda: 145.0)
    monkeypatch.setattr(safe_point_mod.time, "time", lambda: 1015.0)
    second = snapshot_context(ctx, "after_tool")

    assert first["run_started_unix_s"] == 970.0
    assert second["run_started_unix_s"] == 970.0


def test_snapshot_captures_live_cost_tracker():
    class Tracker:
        def snapshot(self, iterations=0):
            from agent.cost_tracker import CostSnapshot
            return CostSnapshot(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    ctx = _ctx()
    ctx.metadata["cost_tracker"] = Tracker()
    payload = snapshot_context(ctx, "before_tool")
    assert payload["metadata"]["cost_snapshot"]["total_tokens"] == 15
    assert "cost_tracker" not in payload["metadata"]


@pytest.mark.asyncio
async def test_state_transition_persists_safe_point(safe_db):
    ctx = _ctx()
    ctx.metadata["safe_point"] = safe_db

    await ctx.transition(State.THINKING, event="start")

    loaded = store.load_safe_point(ctx.run_id)
    assert loaded["phase"] == "state"
    assert loaded["payload"]["state"] == "thinking"
    assert loaded["payload"]["state_history"][-1]["event"] == "start"


@pytest.mark.asyncio
async def test_driver_safe_point_contains_completed_tool_pair(safe_db):
    class Telemetry:
        async def emit(self, event):
            return None

    class Bridle:
        def build_tool_result_message(self, call_id, text, priority="normal"):
            return {"role": "tool", "tool_call_id": call_id, "content": text}

    class Reins:
        async def check(self, point, ctx):
            from agent.harness.contracts import ReinDecision
            return ReinDecision()

    class Saddle:
        async def execute(self, call, ctx, auto_rollback=True):
            return ToolResult(success=True, output="contents"), None

    class Harness:
        bridle = Bridle()
        reins = Reins()
        saddle = Saddle()
        cost_tracker = None

    ctx = _ctx()
    ctx.metadata["safe_point"] = safe_db
    call = ToolCall(id="call-safe", name="file_read", args={})
    ctx.messages = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": call.id, "type": "function",
        "function": {"name": call.name, "arguments": "{}"},
    }]}]

    async for _ in Driver()._execute_serial_call(call, ctx, Harness(), Telemetry(), 0):
        pass

    saved = store.load_safe_point(ctx.run_id)
    assert saved["phase"] == "after_tool"
    assert saved["payload"]["tool_call_id"] == call.id
    assert saved["payload"]["messages"][-1]["tool_call_id"] == call.id
    assert saved["payload"]["span_stack"] == []
    validate_message_pairs(saved["payload"]["messages"])


@pytest.mark.asyncio
async def test_iteration_boundary_is_persisted_before_llm_call(safe_db):
    class RecordingSafePoint(SafePoint):
        def __init__(self):
            super().__init__()
            self.phases = []

        async def save(self, ctx, phase, **extra):
            saved = await super().save(ctx, phase, **extra)
            self.phases.append(phase)
            return saved

    class Telemetry:
        async def emit(self, event):
            return None

    class Bridle:
        async def assemble(self, ctx):
            return list(ctx.messages)

    class Reins:
        async def check(self, point, ctx):
            return __import__(
                "agent.harness.contracts", fromlist=["ReinDecision"]
            ).ReinDecision()

    class Saddle:
        def schemas(self):
            return []

    class Stirrups:
        async def call_llm(self, ctx, messages, tools):
            assert safe_db_phase_contains(
                store.load_safe_point(ctx.run_id), "iteration", ctx.iteration,
            )
            from agent.harness.contracts import LLMOutput
            return LLMOutput(content="done")

    class Harness:
        telemetry = Telemetry()
        bridle = Bridle()
        reins = Reins()
        saddle = Saddle()
        stirrups = Stirrups()
        cost_tracker = None

    recording = RecordingSafePoint()
    ctx = _ctx()
    ctx.config.enable_intention_guide = False
    ctx.config.max_iterations = 1
    ctx.config.completion_guard_enabled = False
    ctx.metadata["safe_point"] = recording

    async for _ in Driver().drive(ctx, Harness()):
        pass

    assert recording.phases[0] == "state"
    assert recording.phases.index("iteration") < recording.phases.index("state", 1)
    assert ctx.state == State.COMPLETED


def safe_db_phase_contains(saved, expected_phase, expected_iteration):
    return (
        saved is not None
        and saved["phase"] == expected_phase
        and saved["payload"]["iteration"] == expected_iteration
    )


@pytest.mark.asyncio
async def test_approval_wait_persists_full_snapshot(safe_db, monkeypatch):
    from types import SimpleNamespace

    from agent.harness.contracts import ReinDecision, ReinPoint
    from agent.harness.reins import approval_wait

    ctx = _ctx()
    ctx.metadata["safe_point"] = safe_db
    ctx.tool_calls_log.append({
        "tool": "shell_exec", "args": {"command": "systemctl restart nginx"},
        "iteration": 0, "success": None,
    })
    ctx.messages = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "approval-call", "type": "function",
        "function": {"name": "shell_exec", "arguments": "{}"},
    }]}]

    class Reins:
        async def check(self, point, ctx):
            assert point == ReinPoint.PRE_TOOL_CALL
            return ReinDecision()

    class Harness:
        reins = Reins()

    async def wait_registered(waiter, timeout):
        saved = store.load_safe_point(ctx.run_id)
        assert saved["phase"] == "waiting_approval"
        assert saved["payload"]["approval_key"] == "approval-key"
        assert saved["payload"]["messages"] == ctx.messages
        return approval_wait.ALLOWED

    monkeypatch.setattr(approval_wait, "wait_registered", wait_registered)
    decision = ReinDecision(
        intercepted=True, approval_key="approval-key",
        intercept_reason="需要审批",
    )

    redone, events = await Driver()._finish_approval(
        ctx, Harness(), decision,
        (SimpleNamespace(key="approval-key"), "signature"),
    )

    assert not redone.intercepted
    assert events == []


@pytest.mark.asyncio
async def test_visible_delivery_wait_and_completion_persist_safe_points(
    safe_db, monkeypatch,
):
    from types import SimpleNamespace

    import agent.pending_delivery as pd
    from agent.harness.contracts import ReinDecision

    ctx = _ctx()
    ctx.metadata["safe_point"] = safe_db
    ctx.tool_calls_log.append({
        "tool": "shell_exec", "args": {"command": "echo ok"},
        "iteration": 0, "success": None,
    })
    call = ToolCall(id="visible-call", name="shell_exec", args={"command": "echo ok"})
    ctx.messages = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": call.id, "type": "function",
        "function": {"name": call.name, "arguments": "{}"},
    }]}]

    class Telemetry:
        async def emit(self, event):
            return None

    class Bridle:
        def build_tool_result_message(self, call_id, text, priority="normal"):
            return {"role": "tool", "tool_call_id": call_id, "content": text}

    class Reins:
        async def check(self, point, ctx):
            return ReinDecision()

    class Harness:
        telemetry = Telemetry()
        bridle = Bridle()
        reins = Reins()

    async def wait(tool_id):
        saved = store.load_safe_point(ctx.run_id)
        assert saved["phase"] == "awaiting_visible_result"
        assert saved["payload"]["tool_call_id"] == call.id
        assert saved["payload"]["messages"] == ctx.messages
        return None

    monkeypatch.setattr(pd.PendingDeliveries, "timeout_of", lambda tool_id: 5)
    monkeypatch.setattr(pd.PendingDeliveries, "wait", wait)
    monkeypatch.setattr(
        pd.PendingDeliveries, "get",
        lambda tool_id: SimpleNamespace(rejected=True),
    )
    monkeypatch.setattr(pd.PendingDeliveries, "remove", lambda tool_id: None)
    result = ToolResult(
        success=True, output="", duration_ms=1,
        pending_delivery={
            "tool_id": "delivery-call", "command": "echo ok",
            "session_id": "pty-1", "requires_confirm": False,
        },
    )

    events = [
        event async for event in Driver()._emit_and_await_visible(
            ctx, Harness(), Telemetry(), result, call, 0,
        )
    ]

    assert [event.kind for event in events] == ["tool_deliver", "tool_call_result"]
    assert events[-1].payload["success"] is False
    saved = store.load_safe_point(ctx.run_id)
    assert saved["phase"] == "after_tool"
    validate_message_pairs(saved["payload"]["messages"])


def test_restore_rejects_orphan_tool_message():
    with pytest.raises(ValueError, match="孤儿"):
        validate_message_pairs([{"role": "tool", "tool_call_id": "missing", "content": "x"}])
