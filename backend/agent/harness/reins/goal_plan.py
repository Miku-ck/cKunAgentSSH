"""goal/plan 进度 Rein — snapshot / delta 事件与每轮步骤上下文注入。

挂载：harness.assemble 按会话存在结构化 goal 时条件追加（**不进**
defaults.py 默认装配清单 —— test_plan_mode 的默认优先级守卫不受影响）。
priority=4，避开 PlanModeGuard(6)/ClarificationInterceptor(7)/
PermissionGate(8)/FilesystemGuard(10)/PlanDisciplineRein(12)。

行为边界：
- 永不拦截：三个挂点都返回非 intercepted 的 ReinDecision（与 plan_mode
  两个拦截器的「零差异」口径一致），事件放进 decision.events —— Driver
  对 events 无条件发出，不需要 emit_event 通道；
- kind="goal_plan_progress" 字面量只出现在本文件，被
  test_trace_projection_keys._emitted_kinds() 的 HARNESS.rglob 扫描覆盖；
- store 读取失败一律降级为空决策（进度可见性是增强，不是正确性前提）。
"""
from __future__ import annotations

import logging
from typing import Any

from ..contracts import ReinDecision, ReinPoint, RunContext, TraceEvent

logger = logging.getLogger(__name__)

_PROGRESS_KIND = "goal_plan_progress"  # 字面量：三处同源守卫依赖它
_PRIORITY = 4


def _load_snapshot(conversation_id: str) -> dict[str, Any] | None:
    """读会话快照；无 goal / 读取失败返回 None（降级，不发事件）。"""
    if not conversation_id:
        return None
    try:
        from db import goal_plan_store
        snap = goal_plan_store.get_snapshot(conversation_id)
    except Exception as e:  # 进度可见性是增强：存储故障不影响 run
        logger.debug("[goal_plan] Rein 快照读取失败（跳过）: %s", e)
        return None
    return snap if snap.get("goal") is not None else None


def _progress_event(ctx: RunContext, mode: str,
                    snap: dict[str, Any]) -> TraceEvent:
    """构造进度事件。刻意**不带完整步骤列表**（`steps` 键已被澄清向导占用，
    且大 payload 重复传输浪费）：snapshot 模式是「前端重新 GET /goal-plan/state
    的信号」，delta 模式带单条 step 由前端按 id 归并。"""
    payload: dict[str, Any] = {
        "mode": mode,
        "goal": snap.get("goal"),
        "plan": snap.get("plan"),
        "progress": snap.get("progress"),
    }
    return TraceEvent(
        kind=_PROGRESS_KIND,
        source="reins.goal_plan",
        run_id=ctx.run_id,
        iteration=ctx.iteration,
        payload=payload,
    )


class GoalPlanSnapshotRein:
    """AGENT_START：向前端对齐一次服务端真值（整体替换前端快照）。"""

    point = ReinPoint.AGENT_START
    priority = _PRIORITY

    async def check(self, ctx: RunContext) -> ReinDecision:
        snap = _load_snapshot(ctx.conversation_id)
        if snap is None:
            return ReinDecision()
        return ReinDecision(events=[_progress_event(ctx, "snapshot", snap)])


class GoalPlanDeltaRein:
    """POST_TOOL_CALL：step_transition 落库后把最新进度推给前端。"""

    point = ReinPoint.POST_TOOL_CALL
    priority = _PRIORITY

    async def check(self, ctx: RunContext) -> ReinDecision:
        last = ctx.active_tool_call or {}
        if str(last.get("tool") or "") != "step_transition":
            return ReinDecision()
        snap = _load_snapshot(ctx.conversation_id)
        if snap is None:
            return ReinDecision()
        event = _progress_event(ctx, "delta", snap)
        event.payload["step"] = _latest_step(snap)
        return ReinDecision(events=[event])


def _latest_step(snap: dict[str, Any]) -> dict[str, Any] | None:
    """取最近更新的步骤（delta 事件只带一条，前端按 step_id 归并）。"""
    steps: list[dict[str, Any]] = snap.get("steps") or []
    if not steps:
        return None
    return max(steps, key=lambda s: str(s.get("updated_at") or ""))


class GoalPlanStepContextRein:
    """PRE_ITERATION：每轮注入当前进行中步骤的轻量上下文（不累积）。

    与 PlanDisciplineRein 同机制：Driver 每轮 assemble 后 extend
    inject_messages，messages 是局部量、不写回 ctx，不会越滚越大。
    """

    point = ReinPoint.PRE_ITERATION
    priority = _PRIORITY

    async def check(self, ctx: RunContext) -> ReinDecision:
        snap = _load_snapshot(ctx.conversation_id)
        if snap is None:
            return ReinDecision()
        current = _current_step(snap)
        if current is None:
            return ReinDecision()
        content = (
            f"[当前步骤] #{current['ordinal']} {current['title']}"
            f" (id={current['step_id']})。完成时用 step_transition 更新状态并附证据；"
            "受阻时转 blocked 并说明原因，不要静默跳步。"
        )
        return ReinDecision(inject_messages=[{"role": "user", "content": content}])


def _current_step(snap: dict[str, Any]) -> dict[str, Any] | None:
    steps: list[dict[str, Any]] = snap.get("steps") or []
    for step in steps:
        if step.get("status") == "in_progress":
            return step
    return None
