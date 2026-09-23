"""
Spec/Plan/Goal 工作流接口

读：返回某会话当前工作流阶段 + 三份工件（spec/plan/goal）原文，
供工具条的「目标」/「计划」两枚 chip 展示（spec 目前没有展示位）。
读取失败一律空串/空对象降级。
写：工件内容仍只由 agent 通过 spec_write/plan_write/goal_write 工具维护，
本模块不提供写内容的端点。唯一的例外是删除 ——
DELETE /api/workflow/artifact 只挂在用户显式动作（chip 的 ×）上：
「目标」/「计划」是可关的开关，关不掉就仍是亮的空开关。前提是这条删除路径
不给模型任何工具入口，否则计划批准门会有逃逸口。
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from agent import workflow

logger = logging.getLogger(__name__)
router = APIRouter()

# kind 白名单：硬校验后才交给 workflow 拼路径，不接受任意文件名。
# 与 agent.workflow._KIND_FILES 同域，路径穿越的最终判据在 workflow 侧
# （解析后仍在 base 内），这里只负责把 400 给到调用方。
_DELETE_KINDS = ("spec", "plan", "goal")


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


@router.delete("/workflow/artifact", status_code=200)
async def delete_workflow_artifact(
    conversation_id: str = Query(default="", description="会话后端 ID"),
    kind: str = Query(description="工件类型：%s" % "/".join(_DELETE_KINDS)),
):
    """删除会话的某个工件。幂等：文件本就不存在也返回成功。

    返回删除后的 phase，供前端提示连带后果 —— get_phase() 取已存在的最高阶，
    删掉 goal.md 会回落到 plan：用户点一个 × 之后阶段确实变了，不说出来会很费解。
    """
    if kind not in _DELETE_KINDS:
        raise HTTPException(status_code=400, detail="kind 仅接受 %s" % "/".join(_DELETE_KINDS))
    if not conversation_id:
        raise HTTPException(status_code=400, detail="缺少 conversation_id")

    deleted = await asyncio.to_thread(workflow.delete_artifact, conversation_id, kind)
    phase = await asyncio.to_thread(workflow.get_phase, conversation_id)
    logger.info("[workflow] 用户删除工件 kind=%s cid=%s deleted=%s → phase=%s",
                kind, conversation_id, deleted, phase)
    return {"deleted": deleted, "phase": phase}