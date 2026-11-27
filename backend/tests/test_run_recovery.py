"""长任务启动对账分类：真实 SQLite 证据驱动，未知副作用禁止自动恢复。"""
from __future__ import annotations

from pathlib import Path

import pytest

import database as db
import db._conn as _conn_mod
from agent.run_recovery import (
    classify_recovery, complete_terminal_tool_calls, pending_tool_call_ids,
)
from db import operation_ledger, run_safe_points


@pytest.fixture
def recovery_db(tmp_path):
    original = _conn_mod._db_path
    db._close_all_connections()
    db.init_db(str(Path(tmp_path) / "recovery.db"))
    conv = db.create_conversation("recovery")
    cid = conv["id"] if isinstance(conv, dict) else str(conv)
    run_safe_points._reset_memo_for_tests()
    operation_ledger._reset_memo_for_tests()
    try:
        yield cid
    finally:
        db._close_all_connections()
        if original:
            db.init_db(original)
        db._close_all_connections()
        run_safe_points._reset_memo_for_tests()
        operation_ledger._reset_memo_for_tests()


def _run(cid: str, run_id: str = "run-r") -> dict:
    db.create_agent_run(run_id, cid, "long task", model_used="model-r")
    return db.get_agent_run(run_id)


def _safe(cid: str, run_id: str = "run-r", *, phase="after_tool", messages=None,
          iteration=2, state="thinking", config=None) -> dict:
    run_safe_points.save_safe_point(
        run_id, expected_version=0, phase=phase,
        payload={
            "schema_version": 1, "run_id": run_id, "conversation_id": cid,
            "task": "long task", "state": state, "iteration": iteration,
            "messages": messages if messages is not None else [
                {"role": "user", "content": "long task"},
                {"role": "assistant", "content": "done"},
            ],
            "config": config or {"max_iterations": 10}, "phase": phase,
        },
    )
    return run_safe_points.load_safe_point(run_id)


def _operation(run_id: str, call_id: str, status: str) -> dict:
    operation_ledger.create_operation(
        operation_id=f"op-{call_id}", run_id=run_id, tool_call_id=call_id,
        tool_name="shell_exec", arguments={"command": "true"},
    )
    current = operation_ledger.get_operation(f"op-{call_id}")
    transitions = {
        "executing": ("prepared", "executing"),
        "succeeded": ("prepared", "executing"),
        "unknown": ("prepared", "executing"),
        "awaiting_delivery": ("prepared", "executing"),
    }
    if status in transitions:
        source, target = transitions[status]
        operation_ledger.transition_operation(
            f"op-{call_id}", from_status=source,
            from_version=current["version"], to_status=target,
        )
        current = operation_ledger.get_operation(f"op-{call_id}")
    if status in ("succeeded", "unknown", "awaiting_delivery"):
        operation_ledger.transition_operation(
            f"op-{call_id}", from_status="executing",
            from_version=current["version"], to_status=status,
        )
    return operation_ledger.get_operation(f"op-{call_id}")


def _pending_messages(call_id="call-r"):
    return [
        {"role": "user", "content": "long task"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "shell_exec", "arguments": "{}"},
        }]},
    ]


def test_complete_safe_point_and_terminal_operations_auto_resume(recovery_db):
    run = _run(recovery_db)
    safe = _safe(recovery_db, messages=_pending_messages())
    operations = [_operation(run["id"], "call-r", "succeeded")]
    decision = classify_recovery(run, safe, operations)
    assert decision.kind == "auto"
    assert decision.safe_point == safe


@pytest.mark.parametrize("status", ["unknown", "executing", "awaiting_delivery", "prepared"])
def test_unresolved_side_effects_require_reconciliation(recovery_db, status):
    run = _run(recovery_db)
    decision = classify_recovery(
        run, _safe(recovery_db), [_operation(run["id"], "call-r", status)],
    )
    assert decision.kind == "reconcile"
    assert "禁止自动重放" in decision.reason


def test_waiting_approval_requires_human_not_automatic_execution(recovery_db):
    run = _run(recovery_db)
    decision = classify_recovery(run, _safe(recovery_db, phase="waiting_approval"), [])
    assert decision.kind == "human"


def test_visible_delivery_requires_human(recovery_db):
    run = _run(recovery_db)
    decision = classify_recovery(run, _safe(recovery_db, phase="awaiting_visible_result"), [])
    assert decision.kind == "human"


def test_missing_safe_point_must_fail(recovery_db):
    decision = classify_recovery(_run(recovery_db), None, [])
    assert decision.kind == "fail"


@pytest.mark.parametrize("field,value", [
    ("conversation_id", "other-conversation"),
    ("task", "other task"),
    ("state", "not-a-state"),
])
def test_inconsistent_safe_point_must_fail(recovery_db, field, value):
    run = _run(recovery_db)
    safe = _safe(recovery_db)
    safe["payload"][field] = value
    assert classify_recovery(run, safe, []).kind == "fail"


def test_orphan_tool_message_must_fail(recovery_db):
    run = _run(recovery_db)
    safe = _safe(recovery_db, messages=[{"role": "tool", "tool_call_id": "orphan", "content": "x"}])
    assert classify_recovery(run, safe, []).kind == "fail"


def test_pending_tool_call_without_ledger_requires_reconciliation(recovery_db):
    run = _run(recovery_db)
    safe = _safe(recovery_db, messages=_pending_messages())
    assert classify_recovery(run, safe, []).kind == "reconcile"


def test_exhausted_iteration_budget_must_fail(recovery_db):
    run = _run(recovery_db)
    assert classify_recovery(run, _safe(recovery_db, iteration=10), []).kind == "fail"


def test_last_zero_based_iteration_is_already_exhausted(recovery_db):
    run = _run(recovery_db)
    safe = _safe(recovery_db, iteration=2, config={"max_iterations": 3})
    assert classify_recovery(run, safe, []).kind == "fail"


def test_iteration_boundary_has_not_consumed_its_budget(recovery_db):
    run = _run(recovery_db)
    safe = _safe(recovery_db, phase="iteration", iteration=2, config={"max_iterations": 3})
    assert classify_recovery(run, safe, []).kind == "auto"


def test_terminal_run_is_not_revived(recovery_db):
    run = _run(recovery_db)
    db.update_agent_run(run["id"], status="completed")
    assert classify_recovery(db.get_agent_run(run["id"]), _safe(recovery_db), []).kind == "ignore"


def test_pending_ids_ignore_completed_tool_pairs():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "done"},
    ]
    assert pending_tool_call_ids(messages) == ["b"]


def test_complete_terminal_tool_calls_replays_only_ledger_results():
    messages = _pending_messages()
    operations = [
        {"tool_call_id": "call-r", "status": "succeeded", "output": "actual output"},
    ]
    completed = complete_terminal_tool_calls(messages, operations)
    assert completed[-1] == {
        "role": "tool", "tool_call_id": "call-r", "content": "actual output",
    }
    assert messages == _pending_messages()


def test_complete_terminal_tool_calls_refuses_unknown_side_effect():
    with pytest.raises(ValueError, match="未知副作用"):
        complete_terminal_tool_calls(_pending_messages(), [
            {"tool_call_id": "call-r", "status": "unknown"},
        ])


def test_complete_terminal_tool_calls_marks_proven_not_executed():
    completed = complete_terminal_tool_calls(_pending_messages(), [
        {"tool_call_id": "call-r", "status": "not_executed", "error": "未执行"},
    ])
    assert "未执行" in completed[-1]["content"]
