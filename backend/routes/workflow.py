"""
Spec/Plan/Goal 工作流只读接口

极轻：只返回某会话当前工作流阶段 + 三份工件（spec/plan/goal）原文，
供前端三段指示条只读展示。不提供任何写操作 —— 工件只由 agent 通过
spec_write/plan_write/goal_write 工具维护。读取失败一律空串/空对象降级。
"""
import asyncio
import logging

from fastapi import APIRouter, Query

from agent import workflow

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/workflow")
async def get_workflow(
    conversation_id: str = Query(default="", description="会话后端 ID"),
):
    """返回会话当前工作流阶段与三份工件原文。无会话/无工件返回空对象。"""
    if not conversation_id:
        return {"phase": "", "spec": "", "plan": "", "goal": ""}

    def _load():
        return (
            workflow.get_phase(conversation_id),
            workflow.read_artifacts(conversation_id),
        )

    try:
        phase, arts = await asyncio.to_thread(_load)
    except Exception as e:
        logger.warning("[workflow] 读取工作流失败（降级为空）: %s", e)
        return {"phase": "", "spec": "", "plan": "", "goal": ""}

    return {
        "phase": phase,
        "spec": arts.get("spec") or "",
        "plan": arts.get("plan") or "",
        "goal": arts.get("goal") or "",
    }


@router.get("/workflow/phases")
async def get_workflow_phases():
    """列出所有有工件的会话 ID（供导航条按会话展示）。"""
    try:
        return {"conversation_ids": await asyncio.to_thread(workflow.get_phases)}
    except Exception as e:
        logger.warning("[workflow] 列举工作流会话失败: %s", e)
        return {"conversation_ids": []}