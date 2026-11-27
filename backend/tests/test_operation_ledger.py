from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from db import operation_ledger as ledger


@pytest.fixture
def ledger_db(tmp_workspace):
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "operations.db"
    dbpkg._close_all_connections()
    dbpkg.init_db(str(path))
    ledger._reset_memo_for_tests()
    try:
        yield str(path)
    finally:
        dbpkg._close_all_connections()
        if original:
            dbpkg.init_db(original)
        dbpkg._close_all_connections()
        ledger._reset_memo_for_tests()


def _create(**overrides):
    values = {
        "operation_id": "op-1",
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "tool_call_id": "call-1",
        "iteration": 2,
        "tool_name": "file_write",
        "arguments": {"path": "notes.txt", "content": "hello"},
        "idempotent": True,
    }
    values.update(overrides)
    return ledger.create_operation(**values)


def test_new_operation_is_prepared_and_unconfirmed(ledger_db):
    operation = _create()

    assert operation["status"] == "prepared"
    assert operation["version"] == 1
    assert operation["confirmation_status"] == "pending"
    assert operation["arguments"] == {"path": "notes.txt", "content": "hello"}
    assert ledger.get_operation("op-1") == operation


def test_success_requires_versioned_state_transition(ledger_db):
    _create()
    assert ledger.transition_operation(
        "op-1", from_status="prepared", from_version=1, to_status="executing"
    )
    assert ledger.transition_operation(
        "op-1", from_status="executing", from_version=2, to_status="succeeded",
        output="written", duration_ms=17,
    )

    operation = ledger.get_operation("op-1")
    assert operation["status"] == "succeeded"
    assert operation["version"] == 3
    assert operation["confirmation_status"] == "confirmed"
    assert operation["output"] == "written"
    assert operation["duration_ms"] == 17


def test_stale_writer_cannot_overwrite_unknown_with_success(ledger_db):
    _create()
    assert ledger.transition_operation(
        "op-1", from_status="prepared", from_version=1, to_status="executing"
    )
    assert ledger.transition_operation(
        "op-1", from_status="executing", from_version=2, to_status="unknown",
        error="connection reset after dispatch",
    )

    assert not ledger.transition_operation(
        "op-1", from_status="executing", from_version=2, to_status="succeeded"
    )
    operation = ledger.get_operation("op-1")
    assert operation["status"] == "unknown"
    assert operation["confirmation_status"] == "pending"


def test_unknown_requires_explicit_reconciliation(ledger_db):
    _create()
    ledger.transition_operation(
        "op-1", from_status="prepared", from_version=1, to_status="executing"
    )
    ledger.transition_operation(
        "op-1", from_status="executing", from_version=2, to_status="unknown",
        error="timeout",
    )

    assert ledger.reconcile_operation(
        "op-1", resolution="not_executed", note="verified target file absent", actor="operator"
    )
    operation = ledger.get_operation("op-1")
    assert operation["status"] == "not_executed"
    assert operation["confirmation_status"] == "confirmed"
    assert operation["confirmed_by"] == "operator"
    assert operation["confirmation_note"] == "verified target file absent"
    assert not ledger.reconcile_operation("op-1", resolution="succeeded", note="again", actor="operator")


def test_illegal_transition_is_rejected(ledger_db):
    _create()
    with pytest.raises(ValueError):
        ledger.transition_operation(
            "op-1", from_status="prepared", from_version=1, to_status="succeeded"
        )
    assert ledger.get_operation("op-1")["status"] == "prepared"


def test_reusing_operation_id_with_different_payload_is_rejected(ledger_db):
    _create()
    with pytest.raises(ValueError):
        _create(arguments={"path": "other.txt", "content": "hello"})
    assert ledger.get_operation("op-1")["tool_name"] == "file_write"


def test_run_operations_are_ordered_by_creation(ledger_db):
    _create(operation_id="op-1")
    _create(operation_id="op-2", tool_call_id="call-2", tool_name="file_edit")

    assert [op["operation_id"] for op in ledger.list_operations("run-1")] == ["op-1", "op-2"]
    assert ledger.list_operations("other-run") == []
