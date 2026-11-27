"""goal/plan 结构化模型工具 — goal_propose / plan_propose / step_transition

配套 backend/db/goal_plan_store.py（SQLite 三表，替代旧 markdown 工件）。
工具只做三件事：提出目标草案、提出计划（进入等待批准）、推进步骤状态
（经 goal_plan_fsm fail-closed 校验后落库）。

边界（不变量，守卫 test_goal_plan_approval_gate）：
- 模型侧**不存在**批准/裁决/重规划/删除的任何工具入口 —— 批准门只能由
  用户经 REST（routes/goal_plan.py）触发；本模块源码刻意不含这些关键字，
  与旧 workflow.py「delete_artifact 刻意不给模型工具」同一设计；
- goal 的 achieved 状态只能由用户裁决产生（store 层第二道闸）；
- plan 不可无 goal（无锚不立项）；同会话已有活动 plan 时 propose 被拒，
  重规划必须由用户先在 REST 侧放弃/重置旧 plan。
"""
import asyncio
import logging

from agent.tools import BaseTool, ToolResult, ToolMeta, ToolContext

logger = logging.getLogger(__name__)

# 内容上限：statement / criteria 会整段注入 system prompt，过长挤掉方法论引导
_MAX_STATEMENT_CHARS = 2000
_MAX_CRITERIA = 10
_MAX_CRITERIA_CHARS = 200
_MAX_STEPS = 50
_MAX_STEP_TITLE_CHARS = 120
_MAX_STEP_DETAIL_CHARS = 500


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text[:limit]


class GoalProposeTool(BaseTool):
    """提出目标草案：写明「达成什么、怎么算成功」，经用户裁决后成为执行锚。"""

    name = "goal_propose"
    description = (
        "把『这次任务达成什么、怎么算成功』写成结构化目标草案（goal）。在动手前调用："
        "给出一句目标陈述与可判定的成功标准列表。草案需经用户裁决生效后成为执行锚，"
        "后续每轮执行都会复现注入，完成判定以此为准，不擅自降低标准。"
        "若会话已有目标，本工具会失败 —— 目标修订由用户在前端完成。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "目标短标题（一句话，≤60 字）"},
            "statement": {"type": "string", "description": "目标陈述：达成什么、边界、怎么算成功"},
            "context": {"type": "string", "description": "可选：需求背景 / 约束 / 范围说明（会作为目标的 context 字段）"},
            "criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可判定的成功标准列表（每条一句话，建议 1-5 条）",
            },
        },
        "required": ["statement"],
        "additionalProperties": False,
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=False,
                    idempotent=False)

    async def execute(self, context: ToolContext, statement: str,
                      title: str = "", background: str = "",
                      criteria: list | None = None) -> ToolResult:
        if not context.conversation_id:
            return ToolResult(False, "", "无会话上下文，无法设立目标。请在会话内运行。",
                              priority="high")
        statement = _clip(statement, _MAX_STATEMENT_CHARS)
        if not statement:
            return ToolResult(False, "", "目标陈述不能为空。请先理解需求再提炼成功标准。",
                              priority="high")
        crit_items = [
            {"id": f"c{i}", "text": _clip(str(text), _MAX_CRITERIA_CHARS),
             "met": False, "evidence": ""}
            for i, text in enumerate((criteria or [])[:_MAX_CRITERIA], start=1)
            if str(text or "").strip()
        ]
        from db import goal_plan_store
        goal = await asyncio.to_thread(
            goal_plan_store.create_goal,
            context.conversation_id, _clip(title, 60), statement,
            _clip(background, _MAX_STATEMENT_CHARS), crit_items, "model_propose",
        )
        if goal is None:
            return ToolResult(
                False, "",
                "本会话已存在目标（或写入失败）。目标修订由用户在前端完成，不要重复提出。",
                priority="high",
            )
        n = len(goal.get("criteria") or [])
        return ToolResult(True, (
            "目标草案已提交，等待用户裁决生效。\n"
            f"目标：{statement[:80]}\n成功标准 {n} 条。\n"
            "裁决前请只做只读调研，不要执行变更操作。"
        ))


class PlanProposeTool(BaseTool):
    """提出执行计划：有序步骤清单，写入等待批准状态，用户批准后才可执行。"""

    name = "plan_propose"
    description = (
        "把目标拆成有序执行步骤，写成结构化计划（plan）。步骤应有明确的交付物与顺序。"
        "写入后计划处于「等待批准」状态：用户批准前写操作会被只读强制拦截，"
        "你只能做只读调研。若会话已有活动计划（等待批准 / 已批准 / 执行中），"
        "本工具会失败 —— 计划重置由用户在前端完成，重置后可参考已完成的步骤证据重新提出。"
        "需要先有目标：会话尚无目标时请先调用 goal_propose。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "步骤名（一句话）"},
                        "detail": {"type": "string", "description": "可选：交付物 / 命令要点"},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
                "description": "有序步骤列表（建议 ≤20 步）",
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }
    meta = ToolMeta(risk_level="safe", reversible=True, cacheable=False,
                    idempotent=False)

    async def execute(self, context: ToolContext, steps: list | None = None) -> ToolResult:
        if not context.conversation_id:
            return ToolResult(False, "", "无会话上下文，无法提出计划。请在会话内运行。",
                              priority="high")
        items = []
        for step in (steps or [])[:_MAX_STEPS]:
            step = step if isinstance(step, dict) else {}
            title = _clip(str(step.get("title") or ""), _MAX_STEP_TITLE_CHARS)
            if not title:
                continue
            items.append({
                "title": title,
                "detail": _clip(str(step.get("detail") or ""), _MAX_STEP_DETAIL_CHARS),
            })
        if not items:
            return ToolResult(False, "", "计划不能为空：请给出至少一个有明确交付物的步骤。",
                              priority="high")

        from db import goal_plan_store
        goal = await asyncio.to_thread(
            goal_plan_store.get_goal_by_conversation, context.conversation_id)
        if goal is None:
            return ToolResult(False, "",
                              "会话尚无目标：请先用 goal_propose 写明成功标准，再提出计划。",
                              priority="high")
        plan = await asyncio.to_thread(
            goal_plan_store.create_plan_with_steps,
            context.conversation_id, goal["goal_id"], items,
        )
        if plan is None:
            return ToolResult(
                False, "",
                "已有活动计划（等待批准 / 已批准 / 执行中），不能重复提出。"
                "需要调整时请告知用户在前端重置计划。",
                priority="high",
            )
        return ToolResult(True, (
            f"计划已提交（{len(items)} 步），状态：等待用户批准。\n"
            "批准前请只做只读调研；批准后按步骤推进，每步用 step_transition 更新状态，"
            "完成时必须附带证据（evidence），并注明本步推进了哪条成功标准。"
        ))


class StepTransitionTool(BaseTool):
    """推进步骤状态：经状态机校验后落库，完成时必须附带证据。"""

    name = "step_transition"
    description = (
        "更新当前会话活动计划中某一步骤的状态。合法转移：pending→in_progress、"
        "pending→skipped、in_progress→completed（必须带 evidence 完成证据，如命令回显要点）、"
        "in_progress→blocked（必须带 blocked_reason 受阻原因）、blocked→in_progress。"
        "completed 的 evidence 须注明本步推进了哪条成功标准（引用目标里标准原文的要点），"
        "让每步完成都能对照目标验收，而不是做完才统一对账。"
        "completed / skipped 是终态，不可再改。step_id 来自系统提示中注入的当前计划步骤列表。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "step_id": {"type": "string", "description": "步骤 ID（来自注入的计划步骤列表）"},
            "to_status": {
                "type": "string",
                "enum": ["in_progress", "completed", "blocked", "skipped"],
                "description": "目标状态",
            },
            "evidence": {"type": "string", "description": "完成证据（completed 时必填）：命令回显要点 / 关键输出"},
            "blocked_reason": {"type": "string", "description": "受阻原因（blocked 时必填）"},
        },
        "required": ["step_id", "to_status"],
        "additionalProperties": False,
    }
    meta = ToolMeta(risk_level="safe", reversible=False, cacheable=False,
                    idempotent=False)

    async def execute(self, context: ToolContext, step_id: str,
                      to_status: str, evidence: str = "",
                      blocked_reason: str = "") -> ToolResult:
        if not context.conversation_id:
            return ToolResult(False, "", "无会话上下文。", priority="high")

        from db import goal_plan_store
        plan = await asyncio.to_thread(
            goal_plan_store.get_active_plan, context.conversation_id)
        if plan is None:
            return ToolResult(False, "", "当前会话没有活动计划，无法推进步骤。",
                              priority="high")
        steps = await asyncio.to_thread(goal_plan_store.get_steps, plan["plan_id"])
        if not any(s["step_id"] == step_id for s in steps):
            return ToolResult(False, "",
                              "step_id 不属于当前活动计划。请核对系统提示中的步骤列表。",
                              priority="high")
        step, reason = await asyncio.to_thread(
            goal_plan_store.update_step_status,
            step_id, to_status, evidence, blocked_reason,
            context.agent_run_id, 0,
        )
        if step is None:
            return ToolResult(False, "", f"状态更新被拒绝：{reason}", priority="high")
        return ToolResult(True, f"步骤「{step['title']}」已更新为 {to_status}。")
