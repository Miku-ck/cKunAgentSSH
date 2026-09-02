# -*- coding: utf-8 -*-
"""回归测试：C-review M6（LoopDetector 第 3 次触发真正终止 run）
与 C-review M8（once 授权不再于每轮迭代末被清空）。
"""
import asyncio

from agent.harness.contracts import (
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
)
from agent.harness.driver import Driver
from agent.harness.reins import LoopDetector, PermissionGate


def _make_ctx() -> RunContext:
    return RunContext.new(task="测试任务", mode="ssh")


def _log_call(ctx: RunContext, name: str = "shell_exec", command: str = "echo hi") -> None:
    ctx.tool_calls_log.append({
        "tool": name,
        "args": {"command": command},
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubReins:
    def __init__(self, owner, pre_decision: ReinDecision):
        self._owner = owner
        self._pre = pre_decision

    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        if point == ReinPoint.PRE_TOOL_CALL:
            return self._pre
        if point == ReinPoint.AGENT_FAILED:
            self._owner.agent_failed_calls += 1
        return ReinDecision()


class _StubHarness:
    """reins 返回预置决策；记录 AGENT_FAILED 触发次数"""

    def __init__(self, pre_decision: ReinDecision):
        self.agent_failed_calls = 0
        self.bridle = _StubBridle()
        self.saddle = None
        self.cost_tracker = None
        self.reins = _StubReins(self, pre_decision)


async def _run_execute_tool_calls(harness, ctx):
    driver = Driver()
    llm_out = LLMOutput(tool_calls=[ToolCall(id="tc1", name="shell_exec", args={"command": "echo hi"})])
    async for ev in driver._execute_tool_calls(llm_out, ctx, harness, _NoopTelemetry(), 0):
        pass


def _run(harness: _StubHarness, ctx: RunContext):
    async def main():
        await ctx.transition(State.ACTING, event="need_tool")
        _log_call(ctx)
        await _run_execute_tool_calls(harness, ctx)
    asyncio.run(main())


# ── M6：LoopDetector 升级链路 ──

def test_loop_detector_third_detection_terminates():
    """连续相同调用：第 3/4 次检测仅拦截，第 5 次（第 3 次检测）terminate=True"""
    ctx = _make_ctx()
    det = LoopDetector()
    decisions = []
    for _ in range(5):
        _log_call(ctx)
        decisions.append(asyncio.run(det.check(ctx)))
    assert not decisions[0].intercepted
    assert not decisions[1].intercepted
    # 第 1 次检测：仅注入纠偏消息，不拦截
    assert not decisions[2].intercepted and decisions[2].inject_messages
    # 第 2 次检测：拦截 + 注入
    assert decisions[3].intercepted and not decisions[3].terminate
    # 第 3 次检测：拦截 + 终止 run
    assert decisions[4].intercepted and decisions[4].terminate


# ── M6：driver 真正终止 run ──

def test_driver_honors_terminate_decision():
    ctx = _make_ctx()
    harness = _StubHarness(ReinDecision(
        intercepted=True,
        terminate=True,
        intercept_reason="循环检测第 3 次触发，终止运行",
    ))
    _run(harness, ctx)
    assert ctx.state == State.FAILED
    assert ctx.metadata.get("force_terminated") is True
    # 终态后不应再转移 OBSERVING/THINKING
    assert not any(h["to"] in ("observing", "thinking") for h in ctx.state_history
                   if h["from"] == "failed")
    assert harness.agent_failed_calls == 1


def test_driver_non_terminate_intercept_continues():
    """未置 terminate 的拦截保持旧行为：拦截 + 继续 OBSERVING→THINKING"""
    ctx = _make_ctx()
    harness = _StubHarness(ReinDecision(
        intercepted=True,
        intercept_reason="普通拦截",
    ))
    _run(harness, ctx)
    assert ctx.state == State.THINKING
    assert "force_terminated" not in ctx.metadata


# ── M8：once 授权不再于迭代末被清空 ──

def test_once_grant_consumes_and_survives_iteration_end():
    """once 授权：消费一次后失效；迭代结束不再全清（竞态修复核心）"""
    gate = PermissionGate()
    gate._confirmed_permanent = set()  # 排除宿主 allowlist.txt 干扰
    key = gate.grant("shell_exec", "once", command="rm -f /tmp/x")
    args = {"command": "rm -f /tmp/x"}
    assert gate.check_permission("shell_exec", args, risk="dangerous") is True
    assert gate.check_permission("shell_exec", args, risk="dangerous") is False
    # 重新授权后授权仍在（driver 已不在迭代末 clear_once，授权存活到被消费）
    gate.grant("shell_exec", "once", command="rm -f /tmp/x")
    assert gate._confirmed_once.get(key, 0) == 1
