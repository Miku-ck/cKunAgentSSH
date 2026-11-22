"""goal/plan run 前装配 — 提示词注入段、permission_mode 推导、metadata 填充。

取代旧 routes/agent.py 的四件套（_WORKFLOW_MODE_PROMPTS / _paired_permission_mode
/ _load_active_goal_text / _setup_goal）。单一出口 load_goal_plan_state：

- injection：goal（陈述 + criteria）→ 活动 plan 步骤（含状态与证据）→
  [工作流] 收敛纪律。无结构化内容时为空串，调用方据此跳过注入；
- force_plan：存在 awaiting_approval plan 时为 True —— 服务端**不信任前端**
  下发的 permission_mode，强制本次 run 进入只读计划档（PlanModeGuard 硬拦截
  + PlanDisciplineRein 纪律注入），否则「等待批准」只是提示词约定；
- metadata：per-run 指针缓存（_gp_*）与 completion guard 兼容键
  （_goal_text / _goal_self_checked，见 test_completion_guard）。

任何读取失败一律降级（injection="" / force_plan=False），绝不让它拖垮 run。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 等待批准 / 已批准 / 执行中 → 本次 run 只读（批准门的服务端推导面）
_PLAN_STATUS_FORCE_PLAN = ("awaiting_approval", "approved", "active")


def load_goal_plan_state(conversation_id: str) -> dict[str, Any]:
    """读会话结构化 goal/plan 并装配 run 前状态。绝不抛（降级为空态）。"""
    empty: dict[str, Any] = {
        "injection": "",
        "force_plan": False,
        "goal_id": "",
        "plan_id": "",
        "plan_version": 0,
        "active_step_id": "",
        "goal_statement": "",
        "criteria_json": "[]",
        "is_conversation_only": False,
    }
    if not conversation_id:
        return empty
    try:
        from db import goal_plan_store

        snap = goal_plan_store.get_snapshot(conversation_id)
    except Exception as e:  # 存储故障：注入与只读强制同时降级，run 照常
        logger.warning("[goal_plan] run 装配读取失败（降级为空）cid=%s: %s",
                       conversation_id, e)
        return empty

    goal = snap.get("goal")
    plan = snap.get("plan")
    steps: list[dict[str, Any]] = snap.get("steps") or []
    progress = snap.get("progress") or {}
    state = dict(empty)

    if goal is None and plan is None:
        return state

    lines: list[str] = []
    if goal is not None:
        state["goal_id"] = goal["goal_id"]
        state["goal_statement"] = goal["statement"]
        criteria = goal.get("criteria") or []
        state["criteria_json"] = json.dumps(criteria, ensure_ascii=False)
        lines.append("[目标]")
        lines.append("本次任务的成功标准（执行中以此为准）：")
        lines.append(goal["statement"])
        if criteria:
            lines.append("逐条标准（x 为用户已确认达成）：")
            for item in criteria:
                mark = "x" if bool(item.get("met")) else " "
                text = str(item.get("text") or "").strip()
                if text:
                    lines.append(f"- [{mark}] {text}")

    active_step_id = ""
    if plan is not None:
        state["plan_id"] = plan["plan_id"]
        state["plan_version"] = plan["version"]
        state["force_plan"] = plan["status"] in _PLAN_STATUS_FORCE_PLAN
        status_label = {
            "awaiting_approval": "等待用户批准（批准前只能只读调研）",
            "approved": "已批准，可以执行",
            "active": "执行中",
        }.get(plan["status"], plan["status"])
        lines.append("[计划]")
        lines.append(f"当前计划 v{plan['version']}（{status_label}），共"
                     f" {progress.get('total', len(steps))} 步、已完成"
                     f" {progress.get('completed', 0)} 步。按步骤推进，"
                     "每步用 step_transition 更新状态，完成必须附 evidence。")
        for step in steps:
            mark = {"pending": " ", "in_progress": "▶", "completed": "✓",
                    "blocked": "!", "skipped": "-"}.get(step["status"], " ")
            line = f"- [{mark}] #{step['ordinal']} {step['title']} (id={step['step_id']})"
            if step["status"] == "completed" and step["evidence"]:
                line += f" — 证据：{step['evidence'][:80]}"
            if step["status"] == "blocked" and step["blocked_reason"]:
                line += f" — 受阻：{step['blocked_reason'][:80]}"
            lines.append(line)
            if step["status"] == "in_progress":
                active_step_id = step["step_id"]
        if plan["status"] == "awaiting_approval":
            lines.append(
                "注意：计划尚未获得用户批准。本段时间内任何写操作都会被系统直接拒绝，"
                "请只做只读调研或向用户说明计划要点，不要尝试绕过。"
            )
    else:
        # 无活动 plan（可能刚被用户重置）：归档证据作为重新规划的输入材料
        try:
            archived = goal_plan_store.get_last_superseded_steps(conversation_id)
        except Exception as e:  # 证据读取失败不影响主注入
            logger.debug("[goal_plan] 归档步骤读取失败（跳过）: %s", e)
            archived = []
        if archived:
            lines.append("[计划重置]")
            lines.append("上一版计划已被用户重置。已完成步骤与证据如下，"
                         "重新提出计划（plan_propose）时请参考，不要重复已验证的结论：")
            for step in archived:
                lines.append(
                    f"- ✓ #{step['ordinal']} {step['title']} — {step['evidence'][:80]}")

    if lines:
        lines.append("[工作流]")
        lines.append(
            "尚未产出明确成功标准前，不要擅自宣布任务完成。"
            "目标是否达成由用户裁决，你的自检结论只是建议。"
        )
        state["injection"] = "\n".join(lines)
    state["active_step_id"] = active_step_id
    return state


def fill_run_metadata(metadata: dict[str, Any], state: dict[str, Any]) -> None:
    """把 run 装配结果写入 ctx.metadata（轻量指针缓存，真值恒在 SQLite）。

    _goal_text / _goal_self_checked 是 completion guard 的兼容键（有 goal 的
    run 不重复裁决），语义见 test_completion_guard —— 键名与写入时机不可改。
    """
    metadata["_gp_goal_id"] = state.get("goal_id") or ""
    metadata["_gp_plan_id"] = state.get("plan_id") or ""
    metadata["_gp_plan_version"] = int(state.get("plan_version") or 0)
    metadata["_gp_active_step_id"] = state.get("active_step_id") or ""
    metadata["_gp_criteria_json"] = state.get("criteria_json") or "[]"
    metadata["_goal_text"] = state.get("goal_statement") or ""
    metadata["_goal_self_checked"] = False
