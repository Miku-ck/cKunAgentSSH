"""运维剧本存储 — runbooks / runbook_runs 两表（镜像 schedules / schedule_runs）。

为什么剧本不进 memories（kind='runbook' 纯文本那份是 AI 沉淀的知识指南，继续走
runbook_tools 那条路）：memories 无结构化列（database.py:613-636），且
mstore.decay_memories 的 UPDATE 无 kind 过滤 —— 长期零命中的行会被降权到 0.2
地板后 retire，可执行剧本被记忆维护管线静默作废不可接受。新表不受 decay 管辖。
v1 明示接受的取舍：新表剧本不入 runbook_search 检索（检索路只看 memories），
双写一致性 defer 到 v2（docs/p0-ops-agent-enhancements.md §3.2）。

工程约定：
- 建表 CREATE TABLE IF NOT EXISTS 幂等（与 database.init_db 同惯例），首用即建，
  不往 database.py 加表定义（B/C 共享文件互斥）。
- SQL 全部为静态字面量语句，外部输入一律参数绑定（铁律：不得用拼接/format/
  f-string 组装 SQL）。局部更新用「COALESCE(?, 列) + 缺席传 None」表达
  『未提供的字段保持原值』，避免动态拼 SET 子句。
- 连接复用 database._get_conn()（同一 SQLite 库、同一 WAL / PRAGMA foreign_keys=ON）。
- 本模块函数全部同步（调用方 asyncio.to_thread），与 database.py / audit.py 同风格。
"""
from __future__ import annotations

import json
import logging
import threading

import database as db

logger = logging.getLogger(__name__)

# runbook_runs 终态枚举（running 之外全部是终态；与前端 STATUS 映射对齐）
RUN_TERMINAL_STATUSES = ("success", "failed", "stopped", "stale_reaped")

# 建表幂等 memo：按 db_path 记忆。测试会切库（_close_all_connections + init_db 新路径），
# memo 键必须跟着路径走，否则新库上跳过建表直接 OperationalError。
_ensured_paths: set[str] = set()
_ensure_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS runbooks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,                 -- ≤100 字（对齐 schedules 命名上限）
    description TEXT NOT NULL DEFAULT '',
    target_host TEXT NOT NULL DEFAULT '',      -- 连接 ID（同 schedules.target_host 语义）
    steps_json  TEXT NOT NULL,                 -- 步骤数组 JSON：[{command, assertCommand, note}]
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_runbooks_enabled ON runbooks(enabled, updated_at);

CREATE TABLE IF NOT EXISTS runbook_runs (
    id          TEXT PRIMARY KEY,
    runbook_id  TEXT NOT NULL REFERENCES runbooks(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL DEFAULT '',      -- 最后一个无头 run 的 run_id（审计关联键）
    status      TEXT NOT NULL DEFAULT 'running',  -- running|success|failed|stopped|stale_reaped
    current_step INTEGER NOT NULL DEFAULT 0,
    steps_state TEXT NOT NULL DEFAULT '[]',    -- [{seq,status,command,assertCommand,verdict,run_id,error}]
    diagnosis   TEXT NOT NULL DEFAULT '',      -- 失败后自动诊断 run 的结论
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_runbook_runs_rid ON runbook_runs(runbook_id, created_at);
"""


def ensure_tables() -> None:
    """幂等建表（含 memo：同一路径只执行一次 DDL）。"""
    key = str(getattr(db, "_db_path", ""))
    if key in _ensured_paths:
        return
    with _ensure_lock:
        if key in _ensured_paths:
            return
        conn = db._get_conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()
        _ensured_paths.add(key)


def _reset_memo_for_tests() -> None:
    """测试切库后强制下一条语句重新建表（生产路径不用）。"""
    with _ensure_lock:
        _ensured_paths.clear()


def _load_steps(raw) -> list[dict]:
    """steps_json → list；脏数据兜底为空数组（存量行不因解析失败炸列表接口）。"""
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw if isinstance(raw, str) else (raw or "[]"))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _row_to_runbook(row) -> dict:
    d = dict(row)
    d["steps"] = _load_steps(d.pop("steps_json", "[]"))
    return d


def _row_to_run(row) -> dict:
    d = dict(row)
    d["steps_state"] = _load_steps(d.pop("steps_state", "[]"))
    return d


# ── runbooks CRUD ─────────────────────────────────────────────────────────────

def list_runbooks() -> list[dict]:
    """剧本列表（含 last_run 摘要联查：取每剧本最近一次执行的状态/时间/历史行 id）。"""
    ensure_tables()
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT b.id, b.name, b.description, b.target_host, b.steps_json,
                   b.enabled, b.created_at, b.updated_at,
                   lr.status AS last_status, lr.started_at AS last_run_at,
                   lr.id AS last_run_id
            FROM runbooks b
            LEFT JOIN runbook_runs lr ON lr.rowid = (
                SELECT r2.rowid FROM runbook_runs r2
                WHERE r2.runbook_id = b.id
                ORDER BY r2.created_at DESC, r2.rowid DESC LIMIT 1
            )
            ORDER BY b.updated_at DESC, b.rowid DESC
            """
        ).fetchall()
        return [_row_to_runbook(r) for r in rows]
    finally:
        conn.close()


def get_runbook(runbook_id: str) -> dict | None:
    ensure_tables()
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT id, name, description, target_host, steps_json, enabled,"
            " created_at, updated_at FROM runbooks WHERE id = ?",
            (runbook_id,),
        ).fetchone()
        return _row_to_runbook(row) if row else None
    finally:
        conn.close()


def create_runbook(data: dict) -> dict:
    """data 需含 name/steps（已由路由校验）；steps 在此序列化为 steps_json。"""
    ensure_tables()
    rid = db._new_id()
    now = db._now()
    steps = data.get("steps")
    steps_json = json.dumps(steps if isinstance(steps, list) else [], ensure_ascii=False)
    conn = db._get_conn()
    try:
        conn.execute(
            """INSERT INTO runbooks (id, name, description, target_host, steps_json,
                                     enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                data.get("name") or "",
                data.get("description") or "",
                data.get("target_host") or "",
                steps_json,
                1 if data.get("enabled", True) else 0,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_runbook(rid)


def update_runbook(runbook_id: str, patch: dict) -> dict | None:
    """局部更新：patch 只含显式传入字段；steps 键存在时整体替换步骤数组。

    静态 UPDATE + COALESCE(?, 列)：缺席字段传 None → 保留原值；显式空串是合法值
    （清空说明），COALESCE 对空串照写不误（空串不是 NULL）。路由层已先行
    _drop_nulls，patch 里不会有显式 None。
    """
    ensure_tables()
    steps = patch.get("steps")
    steps_json = (
        json.dumps(steps, ensure_ascii=False)
        if isinstance(steps, list) else None
    )
    conn = db._get_conn()
    try:
        cur = conn.execute(
            """UPDATE runbooks SET
                   name        = COALESCE(?, name),
                   description = COALESCE(?, description),
                   target_host = COALESCE(?, target_host),
                   steps_json  = COALESCE(?, steps_json),
                   enabled     = COALESCE(?, enabled),
                   updated_at  = ?
               WHERE id = ?""",
            (
                patch.get("name"),
                patch.get("description"),
                patch.get("target_host"),
                steps_json,
                (1 if patch.get("enabled") else 0) if "enabled" in patch else None,
                db._now(),
                runbook_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_runbook(runbook_id)


def delete_runbook(runbook_id: str) -> bool:
    """删除剧本；runbook_runs 由 FK ON DELETE CASCADE 一并删除
    （database.py:80 每连接 PRAGMA foreign_keys=ON，级联真实生效）。"""
    ensure_tables()
    conn = db._get_conn()
    try:
        cur = conn.execute("DELETE FROM runbooks WHERE id = ?", (runbook_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── runbook_runs（执行历史） ──────────────────────────────────────────────────

def create_runbook_run(runbook_id: str, run_id: str = "") -> dict:
    """执行开始：落一行 running 历史。滞留 running 的收尾见 reap_stale_runs。"""
    ensure_tables()
    rrb_id = db._new_id()
    now = db._now()
    conn = db._get_conn()
    try:
        conn.execute(
            """INSERT INTO runbook_runs (id, runbook_id, run_id, status, current_step,
                                         steps_state, started_at, created_at)
               VALUES (?, ?, ?, 'running', 0, '[]', ?, ?)""",
            (rrb_id, runbook_id, run_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": rrb_id, "runbook_id": runbook_id, "run_id": run_id,
        "status": "running", "current_step": 0, "steps_state": [],
        "diagnosis": "", "error": "", "started_at": now,
        "finished_at": None, "created_at": now,
    }


def update_runbook_run(
    rrb_id: str,
    *,
    run_id: str | None = None,
    current_step: int | None = None,
    steps_state: list[dict] | None = None,
) -> None:
    """执行中的进度回写（不改状态 —— 状态只经 finish/reap 走终态单一出口）。

    缺席字段传 None → COALESCE 保留原值，与 update_runbook 同一静态语句手法。
    """
    steps_state_json = (
        json.dumps(steps_state, ensure_ascii=False) if steps_state is not None else None
    )
    ensure_tables()
    conn = db._get_conn()
    try:
        conn.execute(
            """UPDATE runbook_runs SET
                   run_id       = COALESCE(?, run_id),
                   current_step = COALESCE(?, current_step),
                   steps_state  = COALESCE(?, steps_state)
               WHERE id = ?""",
            (run_id, current_step, steps_state_json, rrb_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_runbook_run(
    rrb_id: str,
    status: str,
    *,
    error: str = "",
    diagnosis: str | None = None,
    steps_state: list[dict] | None = None,
    run_id: str | None = None,
) -> bool:
    """落终态（幂等）：只允许 running → 终态，重复调用 no-op。

    终态判定的单一出口 —— 正常收尾 / CancelledError / 启动收尸三条路都汇到这里，
    WHERE 带 status = 'running' 保证「先到者写，后到者弃」，两次收尾互不覆盖。
    可选字段（diagnosis/steps_state/run_id）缺席传 None → COALESCE 保留原值。
    """
    if status not in RUN_TERMINAL_STATUSES:
        raise ValueError(f"非法的 runbook_runs 终态: {status}")
    steps_state_json = (
        json.dumps(steps_state, ensure_ascii=False) if steps_state is not None else None
    )
    ensure_tables()
    conn = db._get_conn()
    try:
        cur = conn.execute(
            """UPDATE runbook_runs SET
                   status      = ?,
                   error       = ?,
                   finished_at = ?,
                   diagnosis   = COALESCE(?, diagnosis),
                   steps_state = COALESCE(?, steps_state),
                   run_id      = COALESCE(?, run_id)
               WHERE id = ? AND status = 'running'""",
            (
                status,
                error or "",
                db._now(),
                diagnosis,
                steps_state_json,
                run_id,
                rrb_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_runbook_runs(runbook_id: str, limit: int = 50) -> list[dict]:
    ensure_tables()
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """SELECT id, runbook_id, run_id, status, current_step, steps_state,
                      diagnosis, error, started_at, finished_at, created_at
               FROM runbook_runs WHERE runbook_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT ?""",
            (runbook_id, limit),
        ).fetchall()
        return [_row_to_run(r) for r in rows]
    finally:
        conn.close()


def clear_runbook_runs(runbook_id: str) -> int:
    """清空某剧本的全部执行历史（保留剧本本身），返回删除行数。

    与 delete_runbook（删整实体、FK CASCADE 带走历史）区分；本函数只清历史行。
    不做 running 检查（路由层 has_running_runbook_run 闸）。SQL 静态字面量 + 参数绑定。
    """
    ensure_tables()
    conn = db._get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM runbook_runs WHERE runbook_id = ?",
            (runbook_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def has_running_runbook_run(runbook_id: str) -> bool:
    """该剧本是否有仍处 running 的执行历史（清空前的进行中闸）。

    与 finish/reap 同一非终态口径：running 是唯一非终态（终态见 RUN_TERMINAL_STATUSES）。
    """
    ensure_tables()
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM runbook_runs WHERE runbook_id = ? AND status = 'running' LIMIT 1",
            (runbook_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def reap_stale_runs(exclude_runbook_ids: set[str] | None = None) -> int:
    """启动收尸（先例 schedule_engine._reap_stale_runs，schedule_engine.py:191-214）：
    把上一次进程存活期遗留的 running 行落为 stale_reaped。

    running 的唯一出口是那个已随进程死掉的 asyncio task —— 后端被杀/崩溃后由
    BackendManager 自动重启，没人回收这些行，前端 isRunning 据此把「执行」按钮
    永久置灰。exclude_runbook_ids：当前进程仍在执行的剧本不被收尸（触发路径在
    登记去重标记之后、建行之前调用本函数时，该剧本的 in-flight 行受保护）。

    排除集在 Python 侧过滤（先 SELECT 再逐行参数化 UPDATE），不拼 IN 子句 ——
    全库 SQL 保持静态字面量语句。
    """
    ensure_tables()
    exclude = {e for e in (exclude_runbook_ids or set()) if e}
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, runbook_id FROM runbook_runs WHERE status = 'running'"
        ).fetchall()
        reaped = 0
        for row in rows:
            if row["runbook_id"] in exclude:
                continue
            cur = conn.execute(
                "UPDATE runbook_runs SET status = 'stale_reaped',"
                " error = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                ("后端重启导致本次执行中断", db._now(), row["id"]),
            )
            reaped += cur.rowcount
        conn.commit()
        return reaped
    finally:
        conn.close()
