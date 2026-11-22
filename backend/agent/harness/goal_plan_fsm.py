"""goal/plan 状态机 — 合法转移矩阵、进度计算与收尾自检文案（纯函数，无 IO）。

goal / plan / step 的状态取值与转移规则在此**唯一定义**（前端
``src/utils/goal-plan-core.ts`` 手工同源，改动必须两侧同步）：

- step 只允许矩阵内转移，其余一律拒绝（fail-closed）；
- ``completed`` 必须携带 evidence、``blocked`` 必须携带 blocked_reason ——
  没有证据的"完成"与没有原因的"受阻"都会污染进度追踪；
- goal 达成（``achieved``）只能由用户裁决产生，模型侧不存在任何入口；
  本模块只提供判定与文案，不做存储、不拼 SQL、不做验收裁决。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── 状态取值集合（与 goal_plan_store 的 DDL 约束、前端 core 同值）─────────────

GOAL_STATUSES = ("draft", "active", "achieved", "revised", "abandoned")
GOAL_ADJUDICATIONS = ("pending", "user_confirmed", "auto_suggested")

PLAN_STATUSES = (
    "draft", "awaiting_approval", "approved", "active",
    "superseded", "completed", "abandoned",
)
# 「活动 plan」= 占用唯一名额的状态集合（同会话至多一个，条件唯一索引同域）
ACTIVE_PLAN_STATUSES = ("awaiting_approval", "approved", "active")

STEP_STATUSES = ("pending", "in_progress", "completed", "blocked", "skipped")
TERMINAL_STEP_STATUSES = ("completed", "skipped")

# 合法转移矩阵：键为当前状态，值为允许的目标状态；缺失键 / 不在值内一律拒绝
LEGAL_STEP_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("in_progress", "skipped"),
    "in_progress": ("completed", "blocked"),
    "blocked": ("in_progress",),
    "completed": (),
    "skipped": (),
}

# 修订 goal 时允许直接写入的状态（achieved 只走裁决入口，不在此列）
GOAL_REVISED_STATUSES = ("active", "revised", "abandoned")


# ── step 转移判定 ─────────────────────────────────────────────────────────────


def can_transition(
    current: str,
    target: str,
    evidence: str = "",
    blocked_reason: str = "",
) -> tuple[bool, str]:
    """判定一次 step 状态跃迁是否合法。返回 ``(是否合法, 拒绝原因)``。

    fail-closed：未知状态、矩阵外转移、缺证据 / 缺原因一律拒绝，reason
    给出可读的中文说明（直接回给模型工具与 REST 调用方）。
    """
    if current not in STEP_STATUSES:
        return False, f"未知当前状态 {current!r}"
    if target not in STEP_STATUSES:
        return False, f"未知目标状态 {target!r}"
    if target not in LEGAL_STEP_TRANSITIONS.get(current, ()):
        return False, f"不允许 {current} → {target} 的状态转移"
    if target == "completed" and not (evidence or "").strip():
        return False, "转移到 completed 必须携带 evidence（完成证据）"
    if target == "blocked" and not (blocked_reason or "").strip():
        return False, "转移到 blocked 必须携带 blocked_reason（受阻原因）"
    return True, ""


def compute_progress(steps: list[dict[str, Any]]) -> dict[str, int]:
    """按状态分桶统计步骤进度。未知状态不计入任何分桶（total 仍取条数，
    让前端能发现数据异常而不是静默丢步骤）。"""
    counts = {"pending": 0, "in_progress": 0, "completed": 0,
              "blocked": 0, "skipped": 0}
    for step in steps:
        status = str((step or {}).get("status") or "")
        if status in counts:
            counts[status] += 1
    return {"total": len(steps), **counts}


# ── criteria 裁决 ─────────────────────────────────────────────────────────────


def merge_criteria(
    criteria: list[dict[str, Any]],
    updates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 id 归并 criteria 裁决结果（met / evidence），返回新列表不改入参。

    updates 里未出现的条目原样保留；未知 id 忽略（调用方据此可发现漂移）。
    """
    merged: list[dict[str, Any]] = []
    for item in criteria:
        cid = str((item or {}).get("id") or "")
        patch = updates.get(cid)
        if not patch:
            merged.append(dict(item))
            continue
        out = dict(item)
        if "met" in patch:
            out["met"] = bool(patch["met"])
        if "evidence" in patch:
            out["evidence"] = str(patch["evidence"] or "")
        merged.append(out)
    return merged


def all_criteria_met(criteria: list[dict[str, Any]]) -> bool:
    """全部条目 met 才算达成（空 criteria 不算达成 —— 无标准即无验收）。"""
    if not criteria:
        return False
    return all(bool(item.get("met")) for item in criteria)


# ── 收尾自检文案 ──────────────────────────────────────────────────────────────


def build_goal_self_check_prompt(statement: str,
                                 criteria: list[dict[str, Any]]) -> str:
    """构造「目标验收自检」提示（M1 起 driver 收尾判定前交给 agent）。

    措辞与前缀 ``[目标验收]``、``已**全部达成**`` / ``仍有未达成`` 是被
    test_goal_plan_adjudication 钉住的取值，改动前先看守卫。
    无 goal 内容时返回空串（调用方据此跳过自检，保持原有收尾行为）。
    """
    statement = (statement or "").strip()
    if not statement:
        return ""
    lines = [
        "[目标验收] 你已经停止调用工具，准备宣布任务完成。宣布前请先对照本次任务的成功标准自检：",
        statement,
    ]
    if criteria:
        lines.append("逐条标准：")
        for item in criteria:
            mark = "x" if bool(item.get("met")) else " "
            text = str(item.get("text") or item.get("id") or "").strip()
            if text:
                lines.append(f"- [{mark}] {text}")
    lines.append(
        "请明确回答：是否已**全部达成**上述成功标准？"
    )
    lines.append(
        "- 若**已全部达成**：简述哪几条标准已满足，然后正常给出最终总结。"
    )
    lines.append(
        "- 若**仍有未达成**：指明确哪些标准未满足，并在下一步继续（重新规划或补调工具），"
        "不要在此轮直接宣布完成；若受限于迭代次数确实无法全部达成，请如实说明差距。"
    )
    return "\n".join(lines)


def criteria_summary(metadata: dict[str, Any]) -> dict[str, int]:
    """从 metadata 统计 criteria 达成情况（done 载荷用）。

    兼容两种来源：``_gp_criteria``（list，driver 兜底路径直存对象）与
    ``_gp_criteria_json``（JSON 串，routes 装配路径）。均缺失时全零。
    """
    criteria = (metadata or {}).get("_gp_criteria")
    if not isinstance(criteria, list):
        try:
            criteria = json.loads((metadata or {}).get("_gp_criteria_json") or "[]")
        except Exception as e:  # 坏 JSON 不该打断收尾
            logger.debug("[goal_plan] criteria_json 解析失败，按空统计: %s", e)
            criteria = []
    if not isinstance(criteria, list):
        criteria = []
    met = sum(1 for item in criteria if isinstance(item, dict) and bool(item.get("met")))
    return {"met": met, "total": len(criteria)}


def criteria_summary_safe(metadata: dict[str, Any]) -> dict[str, int]:
    """criteria_summary 的免异常包装（driver done 载荷用）：统计是增强信息，
    任何意外失败按全零降级，绝不阻断收尾。"""
    try:
        return criteria_summary(metadata)
    except Exception as e:
        logger.debug("[goal_plan] criteria 统计失败，按全零处理: %s", e)
        return {"met": 0, "total": 0}


def resolve_self_check_from_metadata(metadata: dict[str, Any]) -> str:
    """从 per-run metadata 解析收尾自检提示（供 driver 收尾锚调用）。

    - ``_goal_text`` 为空 → 返回空串（无 goal run 走原收尾）；
    - criteria 兼容 ``_gp_criteria``（list）与 ``_gp_criteria_json``（串）；
    - 纯函数：driver 不依赖 store，解析失败时降级为空串走原收尾。
    """
    statement = str((metadata or {}).get("_goal_text") or "").strip()
    if not statement:
        return ""
    criteria = (metadata or {}).get("_gp_criteria")
    if not isinstance(criteria, list):
        try:
            criteria = json.loads((metadata or {}).get("_gp_criteria_json") or "[]")
        except Exception as e:  # 坏 JSON 不该打断收尾，按无逐条标准自检
            logger.debug("[goal_plan] criteria_json 解析失败，按空列表自检: %s", e)
            criteria = []
    return build_goal_self_check_prompt(
        statement, criteria if isinstance(criteria, list) else [])
