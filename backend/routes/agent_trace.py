"""Agent 轨迹路由：运行列表 + 单条运行完整轨迹（Trace Viewer 数据源）。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变。
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import database as db

router = APIRouter()


@router.get("/agent/runs")
async def list_agent_runs(
    status: Optional[str] = Query(default=None, max_length=32),
    run_id: Optional[str] = Query(default=None, max_length=128),
    conversation_id: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """按时间倒序列出 Agent 运行记录（运维审计页「运行列表」分段的数据源）。

    为什么要有这一条：此前只有 /agent/runs/{run_id}/trace 单查，前端要找回一条
    已经跑完的 run 只能靠消息气泡里那枚 ▸轨迹 —— 会话被删，号就无处可查。
    items 与 total 共用 database._run_filters，两个各抄一遍迟早对不上。
    query/count 是同步 sqlite，移入线程池避免阻塞事件循环（与 /audit/records 同法）。
    """
    items, total = await asyncio.to_thread(
        lambda: (
            db.get_agent_runs(
                conversation_id=conversation_id, status=status, run_id=run_id,
                limit=limit, offset=offset,
            ),
            db.count_agent_runs(conversation_id=conversation_id, status=status, run_id=run_id),
        )
    )
    return {"items": items, "total": total}


@router.get("/agent/runs/{run_id}/trace")
def get_agent_run_trace(run_id: str):
    """
    获取 Agent 运行完整轨迹（供 Trace Viewer 可视化）
    包含: run 元信息 + 工具调用时间线 + 状态历史 + 成本统计
    """
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 运行不存在")

    tool_calls = db.get_tool_calls(run_id)

    # 解析 state_history
    state_history = []
    sh_raw = run.get("state_history")
    if sh_raw:
        try:
            state_history = json.loads(sh_raw) if isinstance(sh_raw, str) else sh_raw
        except (json.JSONDecodeError, TypeError):
            state_history = []

    # 计算成本明细
    from agent.cost_tracker import get_model_pricing
    model_used = run.get("model_used") or ""
    in_price, out_price = get_model_pricing(model_used)

    return {
        "run": {
            "id": run.get("id"),
            "conversation_id": run.get("conversation_id"),
            "task": run.get("task"),
            "mode": run.get("mode"),
            "model_used": model_used,
            "status": run.get("status"),
            "iteration": run.get("iteration"),
            "max_iterations": run.get("max_iterations"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
        },
        "cost": {
            "prompt_tokens": run.get("prompt_tokens", 0),
            "completion_tokens": run.get("completion_tokens", 0),
            "total_tokens": run.get("total_tokens", 0),
            "cost_usd": run.get("cost_usd", 0.0),
            "duration_ms": run.get("duration_ms", 0),
            "tool_call_count": run.get("tool_call_count", 0),
            "pricing": {
                "input_per_1m": in_price,
                "output_per_1m": out_price,
                "model": model_used,
            },
        },
        "state_history": state_history,
        "tool_calls": [
            {
                "id": tc.get("id"),
                "iteration": tc.get("iteration"),
                "tool_name": tc.get("tool_name"),
                "arguments": tc.get("arguments"),
                "result": tc.get("result"),
                # 三态透传：NULL = 调用发起了但结果从未回写（崩溃/中止）。原写法
                # bool(tc.get("success", 1)) 把 NULL 折成 True，半截调用被回放成成功。
                "success": None if tc.get("success") is None else bool(tc["success"]),
                "duration_ms": tc.get("duration_ms"),
                "created_at": tc.get("created_at"),
            }
            for tc in tool_calls
        ],
    }
