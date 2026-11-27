"""goal/plan 结构化 REST — 用户侧唯一入口（读 + 裁决 / 批准 / 重规划 / 放弃）。

与模型侧的分工（口径单一）：
- 写内容的三个入口在 agent/tools/builtin/goal_plan_tools.py（模型工具）；
- 本模块是**用户侧**：任何改变门状态的端点都只服务前端显式动作，
  approved_by / adjudicated_by 恒为 'user'（服务端硬置，不信任请求体）；
- 步骤状态跃迁**不走 REST** —— 只由模型 step_transition 经 FSM 校验；
- 读取失败降级为空快照，绝不抛（渲染不该被存储故障拖垮）。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import goal_continuation, goal_plan_store

logger = logging.getLogger(__name__)
router = APIRouter()


class GoalCreateRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1)
    title: str = Field(default="", max_length=60)
    statement: str = Field(..., min_length=1, max_length=2000)
    context: str = Field(default="", max_length=2000)
    criteria: list[str] = Field(default_factory=list, max_length=10)


class GoalReviseRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    title: Optional[str] = Field(default=None, max_length=60)
    statement: Optional[str] = Field(default=None, max_length=2000)
    context: Optional[str] = Field(default=None, max_length=2000)
    criteria: Optional[list[str]] = Field(default=None, max_length=10)
    status: Optional[str] = None


class GoalAdjudicateRequest(BaseModel):
    adjudication: str
    achieved: bool = False


def _criteria_payload(raw: Optional[list[str]]) -> list[dict]:
    return [
        {"id": f"c{i}", "text": (text or "").strip()[:200],
         "met": False, "evidence": ""}
        for i, text in enumerate(raw or [], start=1)
        if (text or "").strip()
    ]


@router.get("/goal-plan/state")
async def get_goal_plan_state(conversation_id: str = ""):
    """会话级快照（goal + 活动 plan + steps + 进度）。无会话返回空快照。"""
    # get_snapshot 内部已降级（失败返空、不抛），这里不再包一层
    return await asyncio.to_thread(goal_plan_store.get_snapshot, conversation_id or "")


@router.post("/goal-plan/goal")
async def create_goal(req: GoalCreateRequest):
    goal = await asyncio.to_thread(
        goal_plan_store.create_goal,
        req.conversation_id, req.title, req.statement,
        req.context, _criteria_payload(req.criteria), "user",
    )
    if goal is None:
        raise HTTPException(status_code=409, detail="该会话已存在目标，请走修订入口")
    return goal


@router.post("/goal-plan/goal/{goal_id}/revise")
async def revise_goal(goal_id: str, req: GoalReviseRequest):
    patch: dict = {}
    for field in ("title", "statement", "context", "status"):
        value = getattr(req, field)
        if value is not None:
            patch[field] = value
    if req.criteria is not None:
        patch["criteria"] = _criteria_payload(req.criteria)
    goal = await asyncio.to_thread(
        goal_plan_store.revise_goal, goal_id, req.expected_version, patch)
    if goal is None:
        raise HTTPException(
            status_code=409,
            detail="目标不存在、状态取值非法或版本已过期（他人已修订），请刷新后重试")
    return goal


@router.post("/goal-plan/goal/{goal_id}/adjudicate")
async def adjudicate_goal(goal_id: str, req: GoalAdjudicateRequest):
    # adjudicated_by 恒为 'user'：本端点只服务前端显式裁决，不信任请求体
    goal = await asyncio.to_thread(
        goal_plan_store.adjudicate_goal,
        goal_id, req.adjudication, req.achieved, "user",
    )
    if goal is None:
        raise HTTPException(status_code=400, detail="裁决取值非法或目标不存在")
    return goal


@router.post("/goal-plan/goal/{goal_id}/continuation/resume")
async def resume_goal_continuation(goal_id: str):
    """用户处理完阻塞后显式恢复自动推进；不改变目标裁决。"""
    resumed = await asyncio.to_thread(goal_continuation.resume, goal_id)
    if not resumed:
        raise HTTPException(status_code=404, detail="该目标尚未进入自动推进")
    return {"resumed": True, "goal_id": goal_id}


@router.post("/goal-plan/plan/{plan_id}/approve")
async def approve_plan(plan_id: str):
    """用户显式批准：awaiting_approval → approved。模型侧没有对应工具入口。"""
    plan = await asyncio.to_thread(goal_plan_store.approve_plan, plan_id)
    if plan is None:
        raise HTTPException(
            status_code=409, detail="计划不存在或不处于等待批准状态（只能批准一次）")
    logger.info("[goal_plan] 用户批准计划 plan=%s", plan_id)
    await asyncio.to_thread(goal_continuation.resume, plan["goal_id"])
    return plan


@router.post("/goal-plan/plan/{plan_id}/replan")
async def replan_plan(plan_id: str):
    """用户触发重规划：旧 plan → superseded（evidence 保留），随后由模型
    重新 plan_propose 产出新版本（version 递增），再次经本模块批准。"""
    plan = await asyncio.to_thread(goal_plan_store.get_plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    ok = await asyncio.to_thread(goal_plan_store.supersede_plan, plan_id)
    if not ok:
        raise HTTPException(status_code=409, detail="计划不处于活动状态，无法重置")
    steps = await asyncio.to_thread(goal_plan_store.get_steps, plan_id)
    logger.info("[goal_plan] 用户重置计划 plan=%s（%d 步归档）", plan_id, len(steps))
    return {
        "superseded_plan_id": plan_id,
        "archived_steps": [
            {"ordinal": s["ordinal"], "title": s["title"], "status": s["status"],
             "evidence": s["evidence"]}
            for s in steps
        ],
    }


@router.post("/goal-plan/plan/{plan_id}/abandon")
async def abandon_plan(plan_id: str):
    ok = await asyncio.to_thread(goal_plan_store.abandon_plan, plan_id)
    if not ok:
        raise HTTPException(status_code=409, detail="计划不处于活动状态")
    logger.info("[goal_plan] 用户放弃计划 plan=%s", plan_id)
    return {"abandoned": True, "plan_id": plan_id}
