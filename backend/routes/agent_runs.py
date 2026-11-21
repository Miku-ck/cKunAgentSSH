"""Agent 挂起任务路由：取消 / 待确认点查询 / 恢复。

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与请求/响应契约不变。

并发槽与执行栈符号按需从 routes.agent 局部导入：这些函数与模块级状态
（_in_flight_runs / _global_run_count）定义在 routes.agent，测试会对该模块
做 monkeypatch，局部导入保证拿到的是同一份可变状态。
"""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db
from agent.audit import append_record_async
from routes.terminal import _get_model_config
from sse_utils import sse_event

logger = logging.getLogger(__name__)

router = APIRouter()


class ResumeRunRequest(BaseModel):
    """POST /agent/runs/{run_id}/resume 请求体（澄清恢复）"""
    kind: Literal["clarification"] = "clarification"
    tool_call_id: str = ""
    answers: dict = Field(default_factory=dict)


@router.post("/agent/runs/{run_id}/cancel")
async def cancel_suspended_run(run_id: str):
    """用户取消等待中的任务 — 幂等。

    取消不是 failed：前端显示「已取消」，启动对账也不把它当崩溃残留。
    待确认点 → cancelled 后清掉快照（取消的任务没有恢复意义）。
    """
    from agent.harness.reins.clarification import cancel_clarification
    from db.run_checkpoints import load_checkpoint, set_pending, clear_checkpoint
    from agent.harness.contracts import TERMINAL_RUN_STATES

    row = await asyncio.to_thread(db.get_agent_run, run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run 记录不存在")
    status = str(row.get("status") or "")
    if status in TERMINAL_RUN_STATES:
        # 幂等：已终态的任务再取消一次无害，直接报告现状
        return {"run_id": run_id, "status": status, "cancelled": False}

    # 取消哨兵 + 内存态回收（挂起路径 run task 已返回，这里通常是 no-op；
    # 竞态窗口内 run 还在等答案时，哨兵能让等待方立刻退出）
    cancel_clarification(run_id)

    ck = await asyncio.to_thread(load_checkpoint, run_id)
    if ck and ck.get("pending"):
        await asyncio.to_thread(
            set_pending, run_id, {**ck["pending"], "state": "cancelled"})
    await asyncio.to_thread(db.update_agent_run, run_id=run_id, status="cancelled")
    await asyncio.to_thread(clear_checkpoint, run_id)
    await append_record_async(
        action="run_cancel",
        conclusion="cancelled",
        run_id=run_id,
        target_type="run",
        target_id=run_id,
    )
    return {"run_id": run_id, "status": "cancelled", "cancelled": True}


@router.get("/agent/runs/{run_id}/pending")
async def get_run_pending(run_id: str):
    """查询挂起任务的待确认点（前端重启后重建卡片用）。

    形状与 clarification_needed 事件 payload 对齐；pending 为 null 表示
    没有挂起的待确认项。
    """
    from db.run_checkpoints import load_checkpoint
    ck = await asyncio.to_thread(load_checkpoint, run_id)
    pending = (ck or {}).get("pending") or {}
    if not pending or pending.get("state") != "pending":
        return {"run_id": run_id, "pending": None}
    row = await asyncio.to_thread(db.get_agent_run, run_id)
    run_status = str((row or {}).get("status") or "")
    if run_status != "waiting_confirmation":
        # DB 状态与快照不一致（对账前的窗口）：不给卡片，避免误导
        return {"run_id": run_id, "pending": None}
    return {
        "run_id": run_id,
        "pending": {
            "kind": "clarification",
            "tool": pending.get("tool") or "",
            "tool_call_id": pending.get("tool_call_id") or "",
            "owner_tool_call_id": pending.get("owner_tool_call_id") or "",
            "args": pending.get("args") or {},
            "command": pending.get("command") or "",
            # 前端权限卡口径：safe|moderate|dangerous（policy_risk 供后端/审计）
            "risk": pending.get("risk") or "moderate",
            "policy_risk": pending.get("policy_risk") or "",
            "steps": pending.get("steps") or [],
            "step": pending.get("step") or 0,
            "total": pending.get("total") or 0,
            "reason": pending.get("reason") or "",
            "created_at": pending.get("created_at") or "",
            "partial": (ck or {}).get("partial") or {},
        },
    }


@router.post("/agent/runs/{run_id}/resume")
async def resume_suspended_run(run_id: str, body: ResumeRunRequest):
    """从待确认点恢复挂起的 run — 同一 run_id 开新 SSE 流。

    语义（永久等待 + 跨重启恢复）：
    - 澄清：答案只作废原调用并触发重规划，原调用绝不执行；
    - 恢复 = 快照回填 → 重放待确认轮 → 进入正常 THINK 循环；
    - CAS 防双恢复：pending state pending→resuming 抢占，失败即 409。
    """
    from db.run_checkpoints import load_checkpoint, set_pending, clear_checkpoint
    from agent.harness.reins.clarification import (
        reinject_pending, record_clarification_answers,
    )
    # 执行栈按需导入（同 /agent/run 的约定）：这些符号在模块层不存在，
    # 不导入的话首次 resume 直接 NameError。
    from agent.cost_tracker import CostTracker
    from agent.harness import Harness, HarnessConfig
    from agent.harness.contracts import AgentConfig as HarnessAgentConfig
    from agent.harness.llm_client import StreamLLMClient
    from agent.harness.telemetry import SSEAdapter
    from agent.tools import create_registry
    # 并发槽定义在 routes.agent（模块级状态与其同体），局部导入保证拿到
    # 同一份可被测试 monkeypatch 的状态。
    from routes.agent import (
        _acquire_global_run_slot,
        _acquire_run_slot,
        _release_global_run_slot_sync,
        _release_run_slot_sync,
    )

    ck = await asyncio.to_thread(load_checkpoint, run_id)
    if ck is None:
        raise HTTPException(status_code=404, detail="该任务没有可恢复的待确认点")
    pending = ck.get("pending") or {}
    if not pending or pending.get("state") not in (None, "pending"):
        raise HTTPException(
            status_code=409, detail="该待确认点已处理（已恢复或已取消）")
    if body.kind != "clarification":
        raise HTTPException(
            status_code=422, detail="当前版本仅支持澄清类待确认点的恢复")
    if not body.answers:
        raise HTTPException(
            status_code=400, detail="澄清答案不能为空（跳过已下线，取消请用 cancel）")
    ticket = str(pending.get("tool_call_id") or "")
    if str(body.tool_call_id or "") and str(body.tool_call_id) != ticket:
        raise HTTPException(status_code=409, detail="澄清票据不匹配（卡片已过期）")

    row = await asyncio.to_thread(db.get_agent_run, run_id)
    if not row:
        await asyncio.to_thread(clear_checkpoint, run_id)
        raise HTTPException(status_code=404, detail="run 记录不存在")
    if str(row.get("status") or "") != "waiting_confirmation":
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {row.get('status')}，只有等待确认中的任务可以恢复")
    model_id = str(row.get("model_used") or "")
    model_cfg = await asyncio.to_thread(_get_model_config, model_id)
    if not model_cfg:
        raise HTTPException(status_code=409, detail="原模型配置已删除，无法恢复")

    spec = ck.get("harness") or {}
    conv_id = str(spec.get("conversation_id") or ck.get("conv_id") or "")
    ssh_session_id = str(spec.get("ssh_session_id") or "")

    # CAS 抢占：pending → resuming；DB waiting_confirmation → running。
    # 任何一步失败都回滚，绝不留下「状态说在跑、快照说在恢复」的半截态。
    await asyncio.to_thread(
        set_pending, run_id, {**pending, "state": "resuming"})
    await asyncio.to_thread(
        db.update_agent_run, run_id=run_id, status="running",
        iteration=int(ck.get("iteration") or 0))

    # 回填澄清内存态 + 记录答案（重放的 PRE_TOOL_CALL 消费答案作废原调用）
    core_pending = {
        k: v for k, v in pending.items()
        if k not in ("state", "round_calls", "start_index", "iteration")
    }
    if not reinject_pending(run_id, core_pending):
        await asyncio.to_thread(
            set_pending, run_id, {**pending, "state": "pending"})
        await asyncio.to_thread(
            db.update_agent_run, run_id=run_id, status="waiting_confirmation")
        raise HTTPException(status_code=500, detail="待确认题回填失败，已回滚")
    rec = record_clarification_answers(run_id, body.answers, tool_call_id=ticket)
    if rec != "recorded":
        await asyncio.to_thread(
            set_pending, run_id, {**pending, "state": "pending"})
        await asyncio.to_thread(
            db.update_agent_run, run_id=run_id, status="waiting_confirmation")
        raise HTTPException(
            status_code=409, detail="澄清答案无法归属当前待确认题（票据或选项不合法）")

    # 并发槽：恢复与普通 run 同待遇（拿不到就 429，保持 waiting 态可重试）
    if not await _acquire_global_run_slot():
        raise HTTPException(
            status_code=429, detail="同时运行的 Agent 任务过多，请稍后重试")
    _conflict = await _acquire_run_slot(conv_id, run_id)
    if _conflict:
        _release_global_run_slot_sync()
        raise HTTPException(
            status_code=409,
            detail=f"该会话已有运行中的 Agent 任务（run_id={_conflict}）")

    resumed = {"flag": False}
    re_suspended = {"flag": False}

    async def _resume_stream():
        try:
            harness = Harness(HarnessConfig(
                base_url=model_cfg["base_url"],
                api_key=model_cfg["api_key"],
                model=model_cfg.get("model_id") or model_cfg["id"],
                max_context_tokens=model_cfg.get("max_tokens", 16384) or 16384,
            ))
            harness.assemble(
                llm_client=StreamLLMClient(
                    base_url=model_cfg["base_url"],
                    api_key=model_cfg["api_key"],
                ),
                tool_registry=create_registry(),
                cost_tracker=CostTracker(
                    model_id=model_cfg.get("model_id") or model_cfg["id"]),
            )
            async for ev in harness.run(
                task=str(spec.get("task") or ""),
                conversation_id=conv_id,
                ssh_session_id=ssh_session_id or None,
                environment=str(spec.get("environment") or "test"),
                environment_source="resume",
                target_host=str(spec.get("target_host") or ""),
                system_prompt=str(spec.get("system_prompt") or ""),
                history=[],
                memories=[],
                config=HarnessAgentConfig(
                    # 恢复必须还有下一轮可用：挂起发生在最后一轮时，
                    # 原预算耗尽会让恢复后的 run 立刻 MAX_REACHED
                    max_iterations=max(
                        int(spec.get("max_iterations") or 100),
                        int(ck.get("iteration") or 0) + 2,
                    ),
                    enable_dynamic_budget=True,
                ),
                run_id=run_id,
                permission_mode=str(spec.get("permission_mode") or "normal"),
                await_approval=True,
                resume_checkpoint=ck,
            ):
                sse_dict = SSEAdapter.to_sse_dict(ev)
                sse_dict["run_id"] = run_id
                if ev.kind == "done":
                    resumed["flag"] = True
                if (ev.kind == "state"
                        and (ev.payload or {}).get("state") == "waiting_confirmation"):
                    # 重放中再次触发澄清 → 新一轮挂起。恢复流是独立的压缩
                    # 消费循环（没有 /agent/run 的 done 分支），DB 状态必须
                    # 在这里落：running → waiting_confirmation。
                    re_suspended["flag"] = True
                    try:
                        await asyncio.to_thread(
                            db.update_agent_run, run_id=run_id,
                            status="waiting_confirmation",
                            iteration=int(ev.iteration or 0),
                        )
                    except Exception as e:
                        logger.warning(
                            "恢复流再次挂起落库失败 run_id=%s: %s", run_id, e)
                yield sse_event(sse_dict)
        except Exception as e:
            logger.exception("恢复运行异常 (run_id=%s)", run_id)
            yield sse_event({
                "type": "error",
                "message": f"恢复失败 {type(e).__name__}: {str(e) or '无错误信息'}",
            })
        finally:
            if resumed["flag"]:
                # 正常终态（completed/failed/...）：清快照（幂等 DELETE）。
                # 若 run 在正常结束前又写过快照（不太可能），新行一并清掉 ——
                # done 意味着没有未决的待确认项。
                await asyncio.to_thread(clear_checkpoint, run_id)
            elif re_suspended["flag"]:
                # 重放中再次挂起：driver 已写入**新**快照（save 是整体覆盖），
                # 新 pending 就是当前待确认项 —— 绝不能用本次 resume 消费的
                # 旧 pending 覆盖它（会把新题换回旧题）。DB 状态已落。
                pass
            else:
                # 恢复失败（引擎异常/流中断）：回滚到可重试的 pending 态。
                # 只翻转 state 位，不整体覆盖 —— 保留磁盘上可能已更新的行。
                ck2 = await asyncio.to_thread(load_checkpoint, run_id)
                p2 = (ck2 or {}).get("pending") or {}
                if p2.get("state") == "resuming":
                    await asyncio.to_thread(
                        set_pending, run_id, {**p2, "state": "pending"})
                await asyncio.to_thread(
                    db.update_agent_run, run_id=run_id,
                    status="waiting_confirmation")
            _release_run_slot_sync(conv_id, run_id)
            _release_global_run_slot_sync()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _resume_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
