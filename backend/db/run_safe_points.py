"""完整运行安全点 — 独立于澄清快照，不截断 messages。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

import db._conn as _conn_mod
from db._conn import _get_conn
from db._util import _now

logger = logging.getLogger(__name__)
_ensured_paths: set[str] = set()
_ensure_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS run_safe_points (
    run_id          TEXT PRIMARY KEY,
    version         INTEGER NOT NULL,
    phase           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_safe_points_updated
    ON run_safe_points(updated_at);
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


def save_safe_point(
    run_id: str,
    *,
    expected_version: int,
    phase: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    ensure_tables()
    conn = _get_conn()
    now = _now()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        row = conn.execute(
            "SELECT version FROM run_safe_points WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            if expected_version != 0:
                return None
            try:
                conn.execute(
                    """INSERT INTO run_safe_points
                    (run_id, version, phase, payload_json, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?)""",
                    (run_id, phase, encoded, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return None
            return {"run_id": run_id, "version": 1, "phase": phase}
        if row["version"] != expected_version:
            return None
        conn.execute(
            """UPDATE run_safe_points
            SET version = ?, phase = ?, payload_json = ?, updated_at = ?
            WHERE run_id = ? AND version = ?""",
            (expected_version + 1, phase, encoded, now, run_id, expected_version),
        )
        conn.commit()
        return {"run_id": run_id, "version": expected_version + 1, "phase": phase}
    finally:
        conn.close()


def load_safe_point(run_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM run_safe_points WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data
    finally:
        conn.close()


def list_safe_points() -> list[dict[str, Any]]:
    ensure_tables()
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM run_safe_points ORDER BY updated_at").fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            result.append(data)
        return result
    finally:
        conn.close()


def delete_safe_point(run_id: str) -> None:
    ensure_tables()
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM run_safe_points WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()


def _reset_memo_for_tests() -> None:
    with _ensure_lock:
        _ensured_paths.clear()
