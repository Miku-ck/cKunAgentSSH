"""Goal 自主推进的持久化领取与停止预算。"""
from __future__ import annotations

import threading
from typing import Any

import db._conn as _conn_mod
from db._conn import _get_conn
from db._util import _now
from db import goal_plan_store

_DDL = """
CREATE TABLE IF NOT EXISTS goal_continuations (
    goal_id          TEXT PRIMARY KEY,
    next_run_at      INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    no_progress      INTEGER NOT NULL DEFAULT 0,
    fingerprint      TEXT NOT NULL DEFAULT '',
    paused           INTEGER NOT NULL DEFAULT 0,
    stop_reason      TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL
);
"""
_ensured_paths: set[str] = set()
_lock = threading.Lock()


def ensure_tables() -> None:
    key = str(getattr(_conn_mod, "_db_path", ""))
    if key in _ensured_paths:
        return
    with _lock:
        if key in _ensured_paths:
            return
        goal_plan_store.ensure_tables()
        conn = _get_conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
        _ensured_paths.add(key)


def _reset_memo_for_tests() -> None:
    with _lock:
        _ensured_paths.clear()


def list_due(now: int, limit: int = 10) -> list[dict[str, Any]]:
    """只返回已批准计划、仍有可执行步骤且没有在途 run 的 active Goal。"""
    ensure_tables()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT g.goal_id, g.conversation_id, g.title, g.statement, g.version,"
            " p.plan_id, p.version AS plan_version"
            " FROM goal_plan_goals g"
            " JOIN goal_plan_plans p ON p.goal_id = g.goal_id"
            " AND p.status IN ('approved','active')"
            " JOIN conversations c ON c.id = g.conversation_id"
            " LEFT JOIN goal_continuations s ON s.goal_id = g.goal_id"
            " WHERE g.status = 'active' AND COALESCE(s.paused, 0) = 0"
            " AND COALESCE(s.next_run_at, 0) <= ?"
            " AND COALESCE(s.attempts, 0) < 100"
            " AND EXISTS (SELECT 1 FROM goal_plan_steps st WHERE st.plan_id = p.plan_id"
            " AND st.status IN ('pending','in_progress','blocked'))"
            " AND NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.conversation_id = g.conversation_id"
            " AND r.status IN ('running','init','waiting_confirmation'))"
            " AND (s.goal_id IS NULL OR s.stop_reason = 'user_resumed' OR NOT EXISTS ("
            " SELECT 1 FROM agent_runs r WHERE r.id = ("
            " SELECT id FROM agent_runs WHERE conversation_id = g.conversation_id"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1)"
            " AND r.status = 'human_help'))"
            " ORDER BY COALESCE(s.next_run_at, 0), g.updated_at LIMIT ?",
            (now, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def claim(goal_id: str, *, now: int, interval: int) -> dict[str, Any] | None:
    """原子推进租约；冲突或已暂停返回 None。"""
    ensure_tables()
    conn = _get_conn()
    try:
        previous = conn.execute(
            "SELECT stop_reason FROM goal_continuations WHERE goal_id = ?", (goal_id,),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO goal_continuations"
            " (goal_id, next_run_at, last_attempt_at, attempts, stop_reason, updated_at)"
            " VALUES (?, ?, ?, 1, '', ?)"
            " ON CONFLICT(goal_id) DO UPDATE SET"
            " next_run_at = excluded.next_run_at, last_attempt_at = excluded.last_attempt_at,"
            " attempts = goal_continuations.attempts + 1, stop_reason = '',"
            " updated_at = excluded.updated_at"
            " WHERE goal_continuations.paused = 0"
            " AND goal_continuations.next_run_at <= ?"
            " AND goal_continuations.attempts < 100"
            " AND (goal_continuations.stop_reason = 'user_resumed' OR NOT EXISTS ("
            " SELECT 1 FROM agent_runs r WHERE r.id = ("
            " SELECT id FROM agent_runs WHERE conversation_id = ("
            " SELECT conversation_id FROM goal_plan_goals WHERE goal_id = ?)"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1)"
            " AND r.status = 'human_help'))",
            (goal_id, now + interval, now, _now(), now, goal_id),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM goal_continuations WHERE goal_id = ?", (goal_id,),
        ).fetchone()
        claimed = dict(row) if row else None
        if claimed is not None:
            claimed["resumed_by_user"] = bool(
                previous and previous["stop_reason"] == "user_resumed"
            )
        return claimed
    finally:
        conn.close()


def finish(
    goal_id: str, *, expected_attempt: int, fingerprint: str, progressed: bool,
    next_run_at: int, max_no_progress: int = 5, max_attempts: int = 100,
    stop_reason: str = "", resumed_by_user: bool = False,
) -> bool:
    """按 attempt CAS 收口；永不修改 Goal 的 achieved/adjudication。"""
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_continuations WHERE goal_id = ?", (goal_id,),
        ).fetchone()
        if row is None or row["attempts"] != expected_attempt:
            return False
        no_progress = 0 if progressed else row["no_progress"] + 1
        paused = bool(stop_reason or no_progress >= max_no_progress or expected_attempt >= max_attempts)
        reason = stop_reason or ("no_progress" if no_progress >= max_no_progress else
                                 "attempt_budget" if expected_attempt >= max_attempts else "")
        if resumed_by_user and not reason:
            reason = "user_resumed"
        cur = conn.execute(
            "UPDATE goal_continuations SET next_run_at = ?, no_progress = ?,"
            " fingerprint = ?, paused = ?, stop_reason = ?, updated_at = ?"
            " WHERE goal_id = ? AND attempts = ?",
            (next_run_at, no_progress, fingerprint, int(paused), reason, _now(),
             goal_id, expected_attempt),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get(goal_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_continuations WHERE goal_id = ?", (goal_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def resume(goal_id: str) -> bool:
    """用户明确恢复后清除暂停，不修改 Goal 裁决。"""
    ensure_tables()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE goal_continuations SET paused = 0, stop_reason = 'user_resumed',"
            " no_progress = 0, attempts = 0, fingerprint = '', next_run_at = 0, updated_at = ? WHERE goal_id = ?",
            (_now(), goal_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
