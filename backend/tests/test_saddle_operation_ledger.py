import asyncio
from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from agent.harness.contracts import AgentConfig, RunContext, ToolCall, ToolResult as HarnessToolResult
from agent.harness.driver import Driver
from agent.harness.operation_ledger import OperationLedger, ledger_for_run, operation_id_for
from agent.harness.safe_point import SafePoint
from agent.harness.saddle import Saddle
from agent.tools import BaseTool, ToolMeta, ToolRegistry, ToolResult
from db import operation_ledger as ledger_store


@pytest.fixture
def ledger_db(tmp_workspace):
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "saddle-operations.db"
    dbpkg._close_all_connections()
    dbpkg.init_db(str(path))
    ledger_store._reset_memo_for_tests()
    try:
        yield OperationLedger()
    finally:
        dbpkg._close_all_connections()
        if original:
            dbpkg.init_db(original)
        dbpkg._close_all_connections()
        ledger_store._reset_memo_for_tests()


def _context(ledger):
    return RunContext(
        run_id="run-1", task="write", conversation_id="conv-1",
        system_prompt="", history=[], memories=[],
        config=AgentConfig(max_wall_clock_s=0, auto_rollback_on_tool_failure=False),
        metadata={"operation_ledger": ledger},
    )


class WriteTool(BaseTool):
    name = "write_once"
    meta = ToolMeta(idempotent=False, reversible=False)

    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome
        self.attempts = 0
        self.contexts = []

    async def execute(self, context, **kwargs):
        self.attempts += 1
        self.contexts.append(context)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _saddle(tool):
    registry = ToolRegistry()
    registry.register(tool)
    return Saddle(registry=registry)


def test_run_ledger_is_enabled_when_database_is_configured():
    assert isinstance(ledger_for_run(enabled=None, db_path="app.db"), OperationLedger)


def test_run_ledger_can_only_be_disabled_explicitly():
    assert ledger_for_run(enabled=False, db_path="app.db") is None
    assert ledger_for_run(enabled=None, db_path="") is None
    assert isinstance(ledger_for_run(enabled=True, db_path=""), OperationLedger)


@pytest.mark.asyncio
async def test_write_is_executing_before_dispatch_and_confirmed_after_success(ledger_db):
    tool = WriteTool(ToolResult(success=True, output="written"))

    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="write_once", args={"value": 1}), _context(ledger_db)
    )

    assert result.success
    operation = ledger_store.list_operations("run-1")[0]
    assert operation["status"] == "succeeded"
    assert operation["confirmation_status"] == "confirmed"
    assert operation["output"] == "written"
    assert tool.contexts[0].operation_id == operation["operation_id"]


@pytest.mark.asyncio
async def test_saddle_persists_safe_point_before_tool_dispatch(ledger_db):
    from db import run_safe_points
    run_safe_points._reset_memo_for_tests()
    safe_point = SafePoint()
    tool = WriteTool(ToolResult(success=True, output="written"))
    ctx = _context(ledger_db)
    ctx.metadata["safe_point"] = safe_point

    await _saddle(tool).execute(
        ToolCall(id="call-safe", name="write_once", args={}), ctx,
    )

    saved = run_safe_points.load_safe_point(ctx.run_id)
    assert saved["phase"] == "before_tool"
    assert saved["payload"]["tool_call_id"] == "call-safe"
    assert saved["payload"]["operation_id"]


@pytest.mark.asyncio
async def test_unwritable_safe_point_blocks_write_dispatch(ledger_db):
    class UnavailableSafePoint:
        async def save(self, ctx, phase, **extra):
            raise OSError("disk full")

    tool = WriteTool(ToolResult(success=True, output="should not run"))
    ctx = _context(ledger_db)
    ctx.metadata["safe_point"] = UnavailableSafePoint()

    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-safe", name="write_once", args={}), ctx,
    )

    assert not result.success
    assert tool.attempts == 0
    operation = ledger_store.list_operations(ctx.run_id)[0]
    assert operation["status"] == "not_executed"


@pytest.mark.asyncio
async def test_non_idempotent_exception_is_unknown_and_never_replayed(ledger_db):
    tool = WriteTool(ConnectionError("connection reset after dispatch"))
    call = ToolCall(id="call-1", name="write_once", args={"value": 1})
    saddle = _saddle(tool)

    first, _ = await saddle.execute(call, _context(ledger_db))
    second, _ = await saddle.execute(call, _context(ledger_db))

    assert not first.success and not second.success
    assert tool.attempts == 1
    operation = ledger_store.list_operations("run-1")[0]
    assert operation["status"] == "unknown"
    assert operation["confirmation_status"] == "pending"
    assert "connection reset" in operation["error"]


@pytest.mark.asyncio
async def test_non_idempotent_transient_failure_is_unknown_without_retry(ledger_db):
    tool = WriteTool(ToolResult(
        success=False, output="", error="timeout", error_kind="transient", retryable=True,
    ))

    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="write_once", args={}), _context(ledger_db)
    )

    assert not result.success
    assert tool.attempts == 1
    assert ledger_store.list_operations("run-1")[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_pending_visible_delivery_is_not_success(ledger_db):
    tool = WriteTool(ToolResult(
        success=False, output="", pending_delivery={"tool_id": "delivery-1", "command": "touch x"},
    ))

    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="write_once", args={}), _context(ledger_db)
    )

    operation = ledger_store.list_operations("run-1")[0]
    assert result.pending_delivery
    assert operation["status"] == "awaiting_delivery"
    assert operation["confirmation_status"] == "pending"
    assert result.pending_delivery["operation_id"] == operation["operation_id"]


@pytest.mark.asyncio
async def test_readonly_tool_does_not_create_operation(ledger_db):
    class ReadTool(WriteTool):
        name = "file_read"

    tool = ReadTool(ToolResult(success=True, output="contents"))
    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="file_read", args={}), _context(ledger_db)
    )

    assert result.success
    assert ledger_store.list_operations("run-1") == []


@pytest.mark.asyncio
async def test_unwritable_ledger_blocks_dispatch(ledger_db):
    class UnavailableLedger:
        async def prepare(self, **kwargs):
            raise OSError("disk full")

    tool = WriteTool(ToolResult(success=True, output="should not run"))
    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="write_once", args={}), _context(UnavailableLedger())
    )

    assert not result.success
    assert tool.attempts == 0
    assert "disk full" in result.error


@pytest.mark.asyncio
async def test_idempotent_tool_retries_share_one_operation(ledger_db):
    class IdempotentWriteTool(WriteTool):
        meta = ToolMeta(idempotent=True, reversible=False)

        async def execute(self, context, **kwargs):
            self.attempts += 1
            self.contexts.append(context)
            if self.attempts == 1:
                return ToolResult(success=False, output="", error="503", error_kind="transient")
            return ToolResult(success=True, output="written")

    tool = IdempotentWriteTool(None)
    result, _ = await _saddle(tool).execute(
        ToolCall(id="call-1", name="write_once", args={}), _context(ledger_db)
    )

    assert result.success
    assert tool.attempts == 2
    operations = ledger_store.list_operations("run-1")
    assert len(operations) == 1
    assert operations[0]["status"] == "succeeded"
    assert tool.contexts[0].operation_id == tool.contexts[1].operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("success", "operation_status"), [
    (True, "succeeded"), (False, "not_executed"), (False, "unknown"),
])
async def test_visible_delivery_closes_operation_from_real_outcome(
    ledger_db, success, operation_status,
):
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

    class Harness:
        bridle = Bridle()
        reins = Reins()

    ctx = _context(ledger_db)
    call = ToolCall(id="call-visible", name="shell_exec", args={"command": "touch x"})
    operation = await ledger_db.prepare(call=call, ctx=ctx, idempotent=False, reversible=True)
    assert await ledger_db.transition(operation, to_status="executing")
    operation.update(status="executing", version=operation["version"] + 1)
    assert await ledger_db.transition(operation, to_status="awaiting_delivery")
    ctx.tool_calls_log.append({"tool": call.name, "args": call.args, "success": None})

    await Driver()._finish_visible(
        ctx, Harness(), Telemetry(),
        HarnessToolResult(success=False, output="", pending_delivery={"operation_id": operation["operation_id"]}),
        call, 0, result_text="terminal result", success=success,
        operation_status=operation_status,
    )

    closed = ledger_store.get_operation(operation_id_for(call, ctx))
    assert closed["status"] == operation_status
    assert closed["confirmation_status"] == ("pending" if operation_status == "unknown" else "confirmed")


@pytest.mark.asyncio
async def test_cancelled_non_idempotent_write_is_unknown(ledger_db):
    tool = WriteTool(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _saddle(tool).execute(
            ToolCall(id="call-1", name="write_once", args={}), _context(ledger_db)
        )

    assert ledger_store.list_operations("run-1")[0]["status"] == "unknown"
