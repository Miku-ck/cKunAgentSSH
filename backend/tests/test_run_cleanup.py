"""终态清理必须保留未收口副作用的对账现场。"""
import pytest

from db import operation_ledger, run_safe_points
from agent.run_cleanup import cleanup_terminal_safe_point


def _point(run_id, status="running"):
    run_safe_points.save_safe_point(
        run_id, expected_version=0, phase="iteration",
        payload={"run_id": run_id, "schema_version": 1, "state": status},
    )


def test_terminal_cleanup_deletes_complete_safe_point(db_db):
    run_id = "clean"
    _point(run_id)
    assert cleanup_terminal_safe_point(run_id, "completed") is True
    assert run_safe_points.load_safe_point(run_id) is None


def test_terminal_cleanup_preserves_unknown_side_effect(db_db):
    run_id = "unknown"
    _point(run_id)
    operation_ledger.create_operation(
        operation_id="op-unknown", run_id=run_id, tool_name="shell_exec", arguments={"command": "write"},
    )
    operation_ledger.transition_operation(
        "op-unknown", from_status="prepared", from_version=1, to_status="executing",
    )
    operation_ledger.transition_operation(
        "op-unknown", from_status="executing", from_version=2, to_status="unknown",
    )
    assert cleanup_terminal_safe_point(run_id, "failed") is False
    assert run_safe_points.load_safe_point(run_id) is not None


def test_human_help_always_preserves_safe_point(db_db):
    run_id = "human"
    _point(run_id, "human_help")
    assert cleanup_terminal_safe_point(run_id, "human_help") is False
    assert run_safe_points.load_safe_point(run_id) is not None


def test_cleanup_storage_failure_preserves_safe_point(db_db, monkeypatch):
    run_id = "broken"
    _point(run_id)
    def fail(_run_id):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(operation_ledger, "list_operations", fail)
    assert cleanup_terminal_safe_point(run_id, "completed") is False
    assert run_safe_points.load_safe_point(run_id) is not None


@pytest.fixture
def db_db(tmp_path):
    import database as db
    db.init_db(str(tmp_path / "cleanup.db"))
    operation_ledger._reset_memo_for_tests()
    run_safe_points._reset_memo_for_tests()
    yield
    operation_ledger._reset_memo_for_tests()
    run_safe_points._reset_memo_for_tests()
