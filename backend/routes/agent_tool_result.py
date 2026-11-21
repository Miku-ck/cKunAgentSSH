"""Agent 工具面路由：可见终端结果回传 + 工具列表/启停。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变：
- POST /agent/tool-result           —— 可见终端命令结果回传，恢复挂起循环
- GET  /agent/tools                 —— 可用工具列表（含启用状态与元数据）
- PATCH /agent/tools/{name}/enabled —— 工具启停（mode=all 为全局）
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database as db
from prompts import SSH_MODE

logger = logging.getLogger(__name__)
router = APIRouter()


class ToolResultRequest(BaseModel):
    """可见终端执行结果回传 — 前端在把命令写入可见终端并捕获输出后调用"""
    tool_id: str
    output: str = ""
    success: bool = False
    exit_code: Optional[int] = None
    # 用户点「拒绝」而非执行：必须区分于「执行失败」，否则模型看到
    # "[已写入可见终端] <cmd>"，以为破坏性命令已经在真实终端里跑过
    rejected: bool = False
    # S1-03：发起回传的 run。不传 = 不声明，服务端照旧按 tool_id 认领；
    # 传了就必须与 create() 时登记的 run 一致，否则一次跨 run 的冒名回传
    # 能把另一个挂起循环直接闭合成成功。
    run_id: Optional[str] = None
    # requires_confirm 的交付：调用方必须显式声明「用户确认过」。
    # 这不是密码学证明（本机持 token 的调用方想声明什么都能声明），但它把
    # 「用户从未点确认」从**服务端无从分辨**变成**一个需要主动主张、且会留在
    # 交付项上供审计读取的事实**（confirmed_by_user）。
    confirmed: bool = False


@router.post("/agent/tool-result")
async def agent_tool_result(req: ToolResultRequest):
    """回传可见终端命令执行结果，恢复挂起的 Agent 循环。

    - 200：成功恢复对应的待交付命令
    - 404：tool_id 未知（可能已超时清除/会话结束）
    - 403：run 归属对不上，或 requires_confirm 的交付未经确认（S1-03）
    """
    # 从 routes.agent 局部导入：测试对该模块的 PendingDeliveries 做 monkeypatch，
    # 局部导入才能拿到替换后的管理器（与 agent_runs / agent_clarification 同策略）。
    from routes.agent import PendingDeliveries

    delivery = PendingDeliveries.get(req.tool_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail=f"tool_id 未知: {req.tool_id}")
    if req.run_id is not None and req.run_id != delivery.run_id:
        # 措辞与 tool_id 未知分开：这一条是「认领对了但越权」，排查时含义完全不同
        logger.warning(
            "拒绝跨 run 的交付回传 (tool_id=%s 属 run=%s，回传方自称 run=%s)",
            req.tool_id, delivery.run_id, req.run_id,
        )
        raise HTTPException(status_code=403, detail="该待交付命令不属于此 run")
    if not req.rejected and delivery.requires_confirm and not req.confirmed:
        logger.warning(
            "拒绝未经用户确认的交付回传 (tool_id=%s, run=%s)", req.tool_id, delivery.run_id
        )
        raise HTTPException(status_code=403, detail="该命令需要用户确认后才能回传执行结果")

    if req.rejected:
        ok = PendingDeliveries.reject(req.tool_id)
    else:
        ok = PendingDeliveries.resume(
            req.tool_id,
            output=req.output,
            success=req.success,
            exit_code=req.exit_code,
            confirmed=req.confirmed,
        )
    if not ok:
        raise HTTPException(status_code=404, detail=f"tool_id 未知: {req.tool_id}")
    return {"status": "ok", "tool_id": req.tool_id}


@router.get("/agent/tools")
def list_tools():
    """查询可用工具列表（含启用状态）"""
    # 获取所有工具类的元信息（包括未注册的，用于显示被禁用的工具）
    from agent.tools.builtin import get_all_tool_classes
    all_tools_info = get_all_tool_classes()

    tools = []
    disabled_list = db.list_disabled_tools(SSH_MODE)
    for tool_cls in all_tools_info:
        # 实例化以读取 name/description/parameters（这些可能是实例属性或类属性）
        instance = tool_cls()
        tools.append({
            "name": instance.name,
            "description": instance.description,
            "parameters": instance.parameters,
            "risk_level": instance.meta.risk_level,
            "enabled": instance.name not in disabled_list,
            # 透传工具元数据，供前端展示
            "timeout": instance.meta.timeout,
            "cacheable": instance.meta.cacheable,
            "reversible": instance.meta.reversible,
        })
    return {"mode": SSH_MODE, "tools": tools}


class UpdateToolEnabledRequest(BaseModel):
    enabled: bool


@router.patch("/agent/tools/{tool_name}/enabled")
def update_tool_enabled(tool_name: str, req: UpdateToolEnabledRequest, mode: str = "all"):
    """更新工具启用状态（默认 mode=all 全局开关）"""
    result = db.upsert_tool_config(tool_name, mode, req.enabled)
    return result
