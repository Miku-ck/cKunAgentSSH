"""Agent 治理路由：意识 tick（对账/维护）+ 策略（反思 + 演化）。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变。
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import database as db
from agent.consciousness_tick import get_global_tick

router = APIRouter()


# ── 意识 tick ──


@router.get("/agent/tick/status")
def consciousness_tick_status():
    """查询意识 tick 状态"""
    tick = get_global_tick()
    return {
        "running": tick._task is not None and not tick._task.done(),
        "interval_seconds": tick.config.interval_seconds,
        "last_tick": tick.last_tick_result,
    }


@router.post("/agent/tick/run")
async def consciousness_tick_run():
    """手动执行一次 tick（对账 + 维护），返回执行结果"""
    return await get_global_tick().trigger_now()


# ── 策略（反思 + 演化） ──


@router.get("/agent/strategies")
def list_agent_strategies(
    mode: Optional[str] = None,
    strategy_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    """查询 Agent 策略列表（reflection / evolved_config）"""
    rows = db.get_agent_strategies(
        mode=mode, strategy_type=strategy_type, limit=limit
    )
    return {"strategies": rows, "count": len(rows)}


@router.patch("/agent/strategies/{strategy_id}/enabled")
def toggle_strategy_enabled(strategy_id: str, enabled: int = 1):
    """启用/禁用单条策略"""
    ok = db.update_agent_strategy_enabled(strategy_id, enabled=int(enabled))
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"updated": True, "id": strategy_id, "enabled": int(enabled)}


@router.delete("/agent/strategies/{strategy_id}")
def delete_agent_strategy_route(strategy_id: str):
    """删除单条策略"""
    ok = db.delete_agent_strategy(strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"deleted": True, "id": strategy_id}
