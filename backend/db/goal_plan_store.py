"""goal/plan 结构化存储 — goal_plan_goals / plans / steps 三表（一等公民）。

替代 Spec/Plan/Goal markdown 文件工件（backend/agent/workflow.py，M3 已删除）的
唯一持久化域；旧工件的读取逻辑内联在本模块迁移器里。工程约定与
``db/run_checkpoints.py`` 同风格：

- 建表 ``CREATE TABLE IF NOT EXISTS`` 幂等、首用即建；memo 按库路径记忆
  （测试切库必须重走 DDL），**不进 database.py 的 _SCHEMA_VERSION 迁移链**
  （main.py 铁律：绝不改 database.py 的建表/迁移）；
- SQL 全部静态字面量，外部输入一律参数绑定；不拼接 SQL；
- 连接复用 ``db._conn._get_conn()``（同一 SQLite 库、同一 WAL / PRAGMA）；
- 全部同步函数（调用方自行 ``asyncio.to_thread``）；
- 刻意不建外键（同 run_checkpoints 的 prune 安全口径），孤儿行由显式清理负责；
- 本模块只管存储：状态合法性判定在 ``agent.harness.goal_plan_fsm``，
  提示词拼装在 ``routes._goal_plan_setup``，语义校验在路由层。

状态口径（与 goal_plan_fsm / 前端 goal-plan-core 手工同源）：
- goal.status ∈ draft|active|achieved|revised|abandoned；achieved 仅在
  adjudication=user_confirmed 下可达（模型侧不存在任何入口）；
- plan 活动 status ∈ awaiting_approval|approved|active，同会话至多一个
  （条件唯一索引 + 应用层校验双保险）；approved_by 恒为 'user'；
- step 转移由 FSM fail-closed 校验后落库。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import db._conn as _conn_mod
from db._conn import _assert_sql_ident, _get_conn
from db._util import _new_id, _now

logger = logging.getLogger(__name__)

__all__ = [
    "ensure_tables",
    "get_goal_by_conversation",
    "create_goal",
    "revise_goal",
    "adjudicate_goal",
    "create_plan_with_steps",
    "get_active_plan",
    "get_plan",
    "get_steps",
    "approve_plan",
    "supersede_plan",
    "abandon_plan",
    "update_step_status",
    "get_snapshot",
    "get_last_superseded_steps",
    "migrate_legacy_artifacts",
    "empty_snapshot",
    "_reset_memo_for_tests",
]

# ── DDL（静态字面量；条件唯一索引保证同会话至多一个活动 plan）─────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS goal_plan_goals (
    goal_id         TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    statement       TEXT NOT NULL DEFAULT '',
    context         TEXT NOT NULL DEFAULT '',
    criteria_json   TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'draft',
    version         INTEGER NOT NULL DEFAULT 1,
    adjudication    TEXT NOT NULL DEFAULT 'pending',
    adjudicated_by  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_plan_goals_conv
    ON goal_plan_goals(conversation_id);

CREATE TABLE IF NOT EXISTS goal_plan_plans (
    plan_id           TEXT PRIMARY KEY,
    goal_id           TEXT NOT NULL DEFAULT '',
    conversation_id   TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'draft',
    approved_by       TEXT NOT NULL DEFAULT '',
    approved_at       TEXT NOT NULL DEFAULT '',
    supersedes_plan_id TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goal_plan_plans_conv
    ON goal_plan_plans(conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_plan_plans_active
    ON goal_plan_plans(conversation_id)
    WHERE status IN ('draft','awaiting_approval','approved','active');

CREATE TABLE IF NOT EXISTS goal_plan_steps (
    step_id        TEXT PRIMARY KEY,
    plan_id        TEXT NOT NULL,
    ordinal        INTEGER NOT NULL DEFAULT 0,
    title          TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    blocked_reason TEXT NOT NULL DEFAULT '',
    evidence       TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    iteration      INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL DEFAULT '',
    finished_at    TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goal_plan_steps_plan
    ON goal_plan_steps(plan_id);
"""

# 建表幂等 memo：按 db_path 记忆（切库必须重跑 DDL，见 run_checkpoints 同款注释）
_ensured_paths: set[str] = set()
_ensure_lock = threading.Lock()


def ensure_tables() -> None:
    """幂等建表（同一路径只执行一次 DDL）。"""
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


def _reset_memo_for_tests() -> None:
    """测试切库后强制下一条语句重新建表（生产路径不用）。"""
    with _ensure_lock:
        _ensured_paths.clear()


# ── 行 ↔ dict 转换 ────────────────────────────────────────────────────────────


def _load_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw if isinstance(raw, str) else (raw or "[]"))
    except Exception as e:  # 一条坏行不该炸整个快照；记 debug 留痕
        logger.debug("[goal_plan] criteria_json 解析失败，按空列表处理: %s", e)
        return []
    return value if isinstance(value, list) else []


def _row_to_goal(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "goal_id": row["goal_id"],
        "conversation_id": row["conversation_id"],
        "title": row["title"],
        "statement": row["statement"],
        "context": row["context"],
        "criteria": _load_list(row["criteria_json"]),
        "status": row["status"],
        "version": row["version"],
        "adjudication": row["adjudication"],
        "adjudicated_by": row["adjudicated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_plan(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "plan_id": row["plan_id"],
        "goal_id": row["goal_id"],
        "conversation_id": row["conversation_id"],
        "version": row["version"],
        "status": row["status"],
        "approved_by": row["approved_by"],
        "approved_at": row["approved_at"],
        "supersedes_plan_id": row["supersedes_plan_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_step(row: Any) -> dict[str, Any]:
    return {
        "step_id": row["step_id"],
        "plan_id": row["plan_id"],
        "ordinal": row["ordinal"],
        "title": row["title"],
        "detail": row["detail"],
        "status": row["status"],
        "blocked_reason": row["blocked_reason"],
        "evidence": row["evidence"],
        "run_id": row["run_id"],
        "iteration": row["iteration"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }


def empty_snapshot() -> dict[str, Any]:
    """无会话 / 读取降级时的空快照（形状与 compute_progress 空列表一致）。

    返回新对象而非共享模块常量，避免调用方原地修改污染。"""
    return {
        "goal": None,
        "plan": None,
        "steps": [],
        "progress": {
            "total": 0, "pending": 0, "in_progress": 0,
            "completed": 0, "blocked": 0, "skipped": 0,
        },
    }


# ── goal ──────────────────────────────────────────────────────────────────────


def get_goal_by_conversation(conversation_id: str) -> dict[str, Any] | None:
    """按会话取 goal；无则 None（唯一索引保证至多一条）。"""
    if not conversation_id:
        return None
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_goals WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_goal(row)


def create_goal(conversation_id: str, title: str, statement: str,
                context: str = "", criteria: list | None = None,
                source: str = "user") -> dict[str, Any] | None:
    """新建 goal（status=draft, adjudication=pending, version=1）。

    同会话已存在 goal 时返回 None（唯一索引冲突；调用方应走 revise）。
    """
    if not conversation_id:
        return None
    ensure_tables()
    now = _now()
    goal_id = _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO goal_plan_goals"
            " (goal_id, conversation_id, title, statement, context,"
            "  criteria_json, status, version, adjudication, adjudicated_by,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'draft', 1, 'pending', '', ?, ?)",
            (goal_id, conversation_id, title or "", statement or "",
             context or "", json.dumps(criteria or [], ensure_ascii=False),
             now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        logger.warning("[goal_plan] 会话 %s 已存在 goal，拒绝重复创建: %s",
                       conversation_id, e)
        return None
    finally:
        conn.close()
    logger.info("[goal_plan] goal 已创建 cid=%s goal_id=%s source=%s",
                conversation_id, goal_id, source)
    return get_goal_by_conversation(conversation_id)


def revise_goal(goal_id: str, expected_version: int,
                patch: dict[str, Any]) -> dict[str, Any] | None:
    """乐观锁修订 goal：WHERE version=expected_version，成功则 version+1。

    仅接受白名单字段；status 只允许修订态集合（achieved 只走裁决入口）。
    版本不匹配或 goal 不存在返回 None（并发冲突，调用方重读再试）。
    """
    ensure_tables()
    allowed = ("title", "statement", "context", "criteria", "status")
    sets: list[str] = []
    params: list[Any] = []
    for field in allowed:
        if field not in patch:
            continue
        if field == "status" and patch["status"] not in ("active", "revised", "abandoned"):
            return None
        column = "criteria_json" if field == "criteria" else field
        # column 全部来自本函数字面量映射，校验是防御性的（同 db._conn 口径）
        _assert_sql_ident(column)
        value = json.dumps(patch[field], ensure_ascii=False) if field == "criteria" else patch[field]
        sets.append(f"{column} = ?")
        params.append(value)
    if not sets:
        return None
    sets.append("version = version + 1")
    sets.append("updated_at = ?")
    params.extend([_now(), goal_id, expected_version])
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"UPDATE goal_plan_goals SET {', '.join(sets)}"
            " WHERE goal_id = ? AND version = ?",
            params,
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
    finally:
        conn.close()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_goal(row)


def adjudicate_goal(goal_id: str, adjudication: str, achieved: bool,
                    adjudicated_by: str) -> dict[str, Any] | None:
    """用户对 goal 裁决。achieved 仅在 adjudication=user_confirmed 下可达
    （store 层硬校验；模型侧本就没有 HTTP 入口，这里是第二道闸）。"""
    if adjudication not in ("pending", "user_confirmed", "auto_suggested"):
        return None
    new_status = "achieved" if (achieved and adjudication == "user_confirmed") else None
    ensure_tables()
    now = _now()
    conn = _get_conn()
    try:
        if new_status:
            cur = conn.execute(
                "UPDATE goal_plan_goals SET status = ?, adjudication = ?,"
                " adjudicated_by = ?, version = version + 1, updated_at = ?"
                " WHERE goal_id = ?",
                (new_status, adjudication, adjudicated_by or "", now, goal_id),
            )
        else:
            cur = conn.execute(
                "UPDATE goal_plan_goals SET adjudication = ?,"
                " adjudicated_by = ?, version = version + 1, updated_at = ?"
                " WHERE goal_id = ?",
                (adjudication, adjudicated_by or "", now, goal_id),
            )
        conn.commit()
        if cur.rowcount != 1:
            return None
    finally:
        conn.close()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_goal(row)


# ── plan / step ───────────────────────────────────────────────────────────────


def create_plan_with_steps(conversation_id: str, goal_id: str,
                           steps: list[dict[str, Any]],
                           supersedes_plan_id: str = "",
                           status: str = "awaiting_approval") -> dict[str, Any] | None:
    """新建 plan（version = 会话内 max+1）+ steps（status=pending，按序号）。

    同会话已有活动 plan（draft/awaiting_approval/approved/active）时唯一索引
    冲突 → 返回 None；重规划必须先 supersede_plan 旧的。
    """
    if not conversation_id or not steps:
        return None
    if status not in ("draft", "awaiting_approval"):
        return None
    ensure_tables()
    now = _now()
    plan_id = _new_id()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM goal_plan_plans"
            " WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO goal_plan_plans"
            " (plan_id, goal_id, conversation_id, version, status,"
            "  approved_by, approved_at, supersedes_plan_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?)",
            (plan_id, goal_id or "", conversation_id, version, status,
             supersedes_plan_id or "", now, now),
        )
        for ordinal, step in enumerate(steps, start=1):
            step = step or {}
            conn.execute(
                "INSERT INTO goal_plan_steps"
                " (step_id, plan_id, ordinal, title, detail, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (_new_id(), plan_id, ordinal,
                 str(step.get("title") or ""), str(step.get("detail") or ""), now),
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        logger.warning("[goal_plan] 会话 %s 已有活动 plan，拒绝重复创建: %s",
                       conversation_id, e)
        return None
    finally:
        conn.close()
    return get_active_plan(conversation_id)


def get_active_plan(conversation_id: str) -> dict[str, Any] | None:
    """会话当前活动 plan（awaiting_approval/approved/active 中最新一条）。"""
    if not conversation_id:
        return None
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_plans WHERE conversation_id = ?"
            " AND status IN ('awaiting_approval','approved','active')"
            " ORDER BY version DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_plan(row)


def get_plan(plan_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_plan(row)


def get_steps(plan_id: str) -> list[dict[str, Any]]:
    ensure_tables()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM goal_plan_steps WHERE plan_id = ? ORDER BY ordinal",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_step(row) for row in rows]


def approve_plan(plan_id: str) -> dict[str, Any] | None:
    """用户显式批准：awaiting_approval → approved，approved_by 恒 'user'。

    其它状态（含已批准/已废弃）返回 None —— 批准只能发生一次。
    """
    ensure_tables()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE goal_plan_plans SET status = 'approved',"
            " approved_by = 'user', approved_at = ?, updated_at = ?"
            " WHERE plan_id = ? AND status = 'awaiting_approval'",
            (_now(), _now(), plan_id),
        )
        conn.commit()
        ok = cur.rowcount == 1
    finally:
        conn.close()
    if not ok:
        return None
    return get_plan(plan_id)


def supersede_plan(plan_id: str) -> bool:
    """重规划前置：活动 plan → superseded（保留 evidence 供新 plan 参考）。"""
    return _transition_plan(plan_id, "superseded")


def abandon_plan(plan_id: str) -> bool:
    """用户放弃：活动 plan → abandoned。"""
    return _transition_plan(plan_id, "abandoned")


def _transition_plan(plan_id: str, to_status: str) -> bool:
    ensure_tables()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE goal_plan_plans SET status = ?, updated_at = ?"
            " WHERE plan_id = ? AND status IN"
            " ('awaiting_approval','approved','active')",
            (to_status, _now(), plan_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def update_step_status(step_id: str, to_status: str, evidence: str = "",
                       blocked_reason: str = "", run_id: str = "",
                       iteration: int = 0) -> tuple[dict[str, Any] | None, str]:
    """step 跃迁：先经 FSM 校验（fail-closed），合法才落库。

    返回 ``(更新后的 step, 拒绝原因)``；拒绝时 step 为 None。
    首个 step 进入 in_progress 时把所属 plan 从 approved 推进为 active。
    """
    from agent.harness.goal_plan_fsm import can_transition  # 局部导入防环

    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_steps WHERE step_id = ?", (step_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, f"step 不存在: {step_id}"
    current = _row_to_step(row)
    ok, reason = can_transition(current["status"], to_status,
                                evidence=evidence, blocked_reason=blocked_reason)
    if not ok:
        return None, reason
    now = _now()
    started = current["started_at"] or (now if to_status == "in_progress" else "")
    finished = now if to_status in ("completed", "skipped") else ""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE goal_plan_steps SET status = ?, evidence = ?,"
            " blocked_reason = ?, run_id = ?, iteration = ?,"
            " started_at = ?, finished_at = ?, updated_at = ?"
            " WHERE step_id = ?",
            (to_status, evidence or "", blocked_reason or "",
             run_id or "", int(iteration or 0), started, finished, now, step_id),
        )
        if to_status == "in_progress":
            conn.execute(
                "UPDATE goal_plan_plans SET status = 'active', updated_at = ?"
                " WHERE plan_id = ? AND status = 'approved'",
                (now, current["plan_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM goal_plan_steps WHERE step_id = ?", (step_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_step(row), ""


# ── 快照 / 迁移 ───────────────────────────────────────────────────────────────


def get_snapshot(conversation_id: str) -> dict[str, Any]:
    """会话级快照：goal + 活动 plan + steps + 进度。读取失败降级为空快照
    （不抛 —— 注入与前端渲染都不该被存储故障打断）。"""
    if not conversation_id:
        return empty_snapshot()
    try:
        goal = get_goal_by_conversation(conversation_id)
        plan = get_active_plan(conversation_id)
        steps = get_steps(plan["plan_id"]) if plan else []
    except Exception as e:  # 存储故障：注入侧降级空串、前端拿到空快照
        logger.warning("[goal_plan] 快照读取失败（降级为空）cid=%s: %s",
                       conversation_id, e)
        return empty_snapshot()
    from agent.harness.goal_plan_fsm import compute_progress  # 局部导入防环
    return {
        "goal": goal,
        "plan": plan,
        "steps": steps,
        "progress": compute_progress(steps),
    }


def get_last_superseded_steps(conversation_id: str) -> list[dict[str, Any]]:
    """最近一次被重置 plan 的已完成步骤（含 evidence），供重新规划参考。

    无活动 plan 但存在归档时，run 装配把这份证据注入提示词 —— 重规划不是
    从零开始，已完成步骤的证据是输入材料。
    """
    if not conversation_id:
        return []
    ensure_tables()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT plan_id FROM goal_plan_plans WHERE conversation_id = ?"
            " AND status = 'superseded' ORDER BY version DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT * FROM goal_plan_steps WHERE plan_id = ? AND status IN"
            " ('completed','skipped') ORDER BY ordinal",
            (row["plan_id"],),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_step(r) for r in rows]


# 旧 markdown 工件的编号行拆分：1. / 1、 / 1) / - / * 开头的行视为一步
_STEP_LINE_RE = re.compile(r"^\s*(?:\d+[.、)]|[-*])\s+")

# ── 旧工件读取（内联自已删除的 agent/workflow.py，仅迁移器使用） ─────────────

_LEGACY_BASE_DIR = Path.home() / ".ckunagentic" / "workflow"
_LEGACY_FILES = {"spec": "spec.md", "plan": "plan.md", "goal": "goal.md"}


def _legacy_checked_path(base_dir: Path, conversation_id: str, name: str) -> Path | None:
    """旧工件路径的 containment 判据（原 _checked_path，注释保留原义）。

    resolve 后要求路径仍落在 base 内：带 ".." 的会话 id 不能越目录读到
    他人会话工件。非法入参返回 None，调用方按"工件不存在"处理（绝不抛）。
    """
    if not conversation_id:
        return None
    try:
        base_root = Path(base_dir).resolve()
        path = (base_root / conversation_id / name).resolve()
    except (OSError, ValueError):
        # 非法路径字符（如 Windows 保留名、嵌入 \0）：视作非法入参，不抛
        return None
    if not path.is_relative_to(base_root):
        return None
    return path


def _read_legacy_artifacts(conversation_id: str, base_dir: str | None) -> dict[str, str]:
    """读取旧三工件原文。缺失字段返回空串；入参非法也返回空串。"""
    base = Path(base_dir) if base_dir else _LEGACY_BASE_DIR

    def _one(name: str) -> str:
        path = _legacy_checked_path(base, conversation_id, name)
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    return {"spec": _one(_LEGACY_FILES["spec"]),
            "plan": _one(_LEGACY_FILES["plan"]),
            "goal": _one(_LEGACY_FILES["goal"])}


def migrate_legacy_artifacts(conversation_id: str,
                             base_dir: str | None = None) -> dict[str, Any] | None:
    """旧 ~/.ckunagentic/workflow/{cid}/{spec,plan,goal}.md 一次性幂等迁移。

    - goal.md → goal.statement + 单条 criteria；spec.md → goal.context；
      plan.md → plan(version 按会话递增, status=awaiting_approval)，编号行拆 steps；
    - 会话已有 goal 时跳过（幂等）；旧文件只读一次、永久保留不删；
    - 迁移产物一律 adjudication=pending / plan 未批准 —— 必须经用户显式
      裁决与批准才生效，不因迁移自动放行。
    返回迁移后的活动 plan（无可迁内容或已迁移过返回 None）。
    """
    if not conversation_id:
        return None
    if get_goal_by_conversation(conversation_id) is not None:
        return None  # 已有结构化 goal：视为迁移完成（幂等）

    arts = _read_legacy_artifacts(conversation_id, base_dir)
    goal_text = (arts.get("goal") or "").strip()
    plan_text = (arts.get("plan") or "").strip()
    spec_text = (arts.get("spec") or "").strip()
    if not (goal_text or plan_text or spec_text):
        return None

    criteria: list[dict[str, Any]] = []
    if goal_text:
        criteria = [{"id": "c1", "text": goal_text, "met": False, "evidence": ""}]
    goal = create_goal(conversation_id, title="", statement=goal_text,
                       context=spec_text, criteria=criteria, source="migration")
    if goal is None:
        return None

    steps: list[dict[str, Any]] = []
    if plan_text:
        for line in plan_text.splitlines():
            if _STEP_LINE_RE.match(line):
                steps.append({"title": _STEP_LINE_RE.sub("", line).strip()[:120],
                              "detail": ""})
    if not steps:
        return None  # goal 已迁移；旧 plan 无可拆步骤则不造空 plan
    plan = create_plan_with_steps(conversation_id, goal["goal_id"], steps,
                                  status="awaiting_approval")
    logger.info("[goal_plan] 旧工件迁移完成 cid=%s goal=%s plan=%s steps=%d",
                conversation_id, bool(goal), bool(plan), len(steps))
    return plan
