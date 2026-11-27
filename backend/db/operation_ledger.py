"""副作用操作账本 — 执行前登记、版本化闭合、未知结果显式对账。

每个非只读工具调用对应一个稳定 operation_id。状态和 confirmation_status 分开：
执行结果可知时自动确认；超时/取消/断链留下的 unknown 必须由人或对账器显式收口。
所有写操作使用 status + version 的 CAS，避免恢复过程覆盖较新的观测。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

import db._conn as _conn_mod
from db._conn import _get_conn
from db._util import _now

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS = {
    "prepared": {"executing", "not_executed"},
    "executing": {"succeeded", "failed", "unknown", "awaiting_delivery"},
    "awaiting_delivery": {"executing", "succeeded", "failed", "unknown", "not_executed"},
    "unknown": set(),
    "succeeded": set(),
    "failed": set(),
    "not_executed": set(),
}
_RESOLUTIONS = {"succeeded", "failed", "not_executed"}
_ensured_paths: set[str] = set()
_ensure_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS agent_operations (
    operation_id        TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    conversation_id     TEXT NOT NULL DEFAULT '',
    tool_call_id        TEXT NOT NULL DEFAULT '',
    iteration           INTEGER NOT NULL DEFAULT 0,
    tool_name           TEXT NOT NULL,
    arguments_json      TEXT NOT NULL DEFAULT '{}',
    idempotent          INTEGER NOT NULL DEFAULT 0,
    reversible          INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'prepared',
    version             INTEGER NOT NULL DEFAULT 1,
    confirmation_status TEXT NOT NULL DEFAULT 'pending',
    confirmed_by        TEXT NOT NULL DEFAULT '',
    confirmation_note   TEXT NOT NULL DEFAULT '',
    output              TEXT NOT NULL DEFAULT '',
    error               TEXT NOT NULL DEFAULT '',
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_operations_run
    ON agent_operations(run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_agent_operations_unresolved
    ON agent_operations(status, run_id);
"""


def ensure_tables() -> None:
    key = str(getattr(_conn_mod, "_db_path", ""))
    if key in _ensured_paths:
        return
    with _ensure_lock:
        if key in _ensured_paths:
            return
        conn = _get_conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
        _ensured_paths.add(key)


def _row_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["arguments"] = json.loads(data.pop("arguments_json"))
    data["idempotent"] = bool(data["idempotent"])
    data["reversible"] = bool(data["reversible"])
    return data


def create_operation(
    *,
    operation_id: str,
    run_id: str,
    conversation_id: str = "",
    tool_call_id: str = "",
    iteration: int = 0,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    idempotent: bool = False,
    reversible: bool = False,
) -> dict[str, Any]:
    """登记未执行操作；同一 ID 的重入必须携带完全相同的事实。"""
    ensure_tables()
    arguments_json = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO agent_operations
            (operation_id, run_id, conversation_id, tool_call_id, iteration,
             tool_name, arguments_json, idempotent, reversible, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (operation_id, run_id, conversation_id, tool_call_id, iteration,
             tool_name, arguments_json, int(idempotent), int(reversible), now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    finally:
        conn.close()
    operation = _row_dict(row)
    if operation is None:
        raise RuntimeError("操作账本登记失败")
    immutable = (operation["run_id"], operation["conversation_id"],
                 operation["tool_call_id"], operation["iteration"], operation["tool_name"])
    expected = (run_id, conversation_id, tool_call_id, iteration, tool_name)
    if immutable != expected or operation["arguments"] != (arguments or {}):
        raise ValueError(f"operation_id 已绑定不同调用: {operation_id}")
    return operation


def get_operation(operation_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def list_operations(run_id: str) -> list[dict[str, Any]]:
    ensure_tables()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_operations WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def transition_operation(
    operation_id: str,
    *,
    from_status: str,
    from_version: int,
    to_status: str,
    output: str = "",
    error: str = "",
    duration_ms: int = 0,
    note: str = "",
) -> bool:
    """按当前状态和版本闭合一步；False 表示竞争者已推进或操作不存在。"""
    if from_status not in _ALLOWED_TRANSITIONS or to_status not in _ALLOWED_TRANSITIONS[from_status]:
        raise ValueError(f"非法操作状态转移: {from_status} -> {to_status}")
    ensure_tables()
    confirmed = "confirmed" if to_status in {"succeeded", "failed", "not_executed"} else "pending"
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE agent_operations
            SET status = ?, version = version + 1, confirmation_status = ?,
                confirmation_note = ?, output = ?, error = ?, duration_ms = ?, updated_at = ?
            WHERE operation_id = ? AND status = ? AND version = ?""",
            (to_status, confirmed, note, output, error, duration_ms, _now(),
             operation_id, from_status, from_version),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def reconcile_operation(
    operation_id: str,
    *,
    resolution: str,
    note: str,
    actor: str,
) -> bool:
    """把 unknown 显式对账为已成功、已失败或证明未执行。"""
    if resolution not in _RESOLUTIONS:
        raise ValueError(f"非法对账结果: {resolution}")
    ensure_tables()
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE agent_operations
            SET status = ?, version = version + 1, confirmation_status = 'confirmed',
                confirmed_by = ?, confirmation_note = ?, updated_at = ?
            WHERE operation_id = ? AND status = 'unknown'""",
            (resolution, actor, note, _now(), operation_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _reset_memo_for_tests() -> None:
    with _ensure_lock:
        _ensured_paths.clear()
