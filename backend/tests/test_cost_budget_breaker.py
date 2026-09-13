# -*- coding: utf-8 -*-
"""C-review M10 回归：成本/token 硬熔断。

修复前：max_iterations 可 500 + DynamicBudgetRein 可再放宽 2x，最坏 ~1000 次
LLM 调用；CostTracker 只累计、无人切断。
修复后：CostBudgetRein（POST_ITERATION, priority=1）每轮检查累计成本/token，
超限 → intercepted + terminate，driver transition(FAILED) 终止 run。
上限：ctx.config.max_cost_usd / max_total_tokens（None=内置默认，0=禁用）。

运行: python -m pytest backend/tests/test_cost_budget_breaker.py -v
"""
import asyncio
from typing import Optional

import pytest

from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    ReinDecision,
    ReinPoint,
    RunContext,
    State,
    ToolCall,
    ToolResult,
)
from agent.harness.driver import Driver
from agent.harness.reins import CostBudgetRein
import agent.harness.reins as reins_mod


class _FakeTracker:
    def __init__(self, tokens: int = 0, cost: float = 0.0):
        self._tokens = tokens
        self._cost = cost

    def get_total_tokens(self) -> int:
        return self._tokens

    def get_total_cost(self) -> float:
        return self._cost

    def snapshot(self, iterations: int = 0):
        from agent.cost_tracker import CostSnapshot
        return CostSnapshot(
            prompt_tokens=self._tokens, completion_tokens=0,
            total_tokens=self._tokens, cost_usd=self._cost, iterations=iterations,
        )


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch):
    """熔断落审计走真实 DB — 测试中屏蔽（只验证决策行为）"""
    async def _noop_audit(**kw):
        return None
    monkeypatch.setattr(reins_mod, "append_record_async", _noop_audit)


def _ctx(config: AgentConfig, tracker: Optional[_FakeTracker]) -> RunContext:
    ctx = RunContext.new(task="测试任务", mode="ssh", config=config)
    if tracker is not None:
        ctx.metadata["cost_tracker"] = tracker
    return ctx


# ── 单元：CostBudgetRein 判定 ──

def test_within_budget_no_intercept():
    ctx = _ctx(AgentConfig(), _FakeTracker(tokens=1000, cost=0.01))
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert not d.intercepted and not d.terminate


def test_token_exceed_terminates():
    ctx = _ctx(AgentConfig(), _FakeTracker(tokens=4_000_000, cost=0.01))
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert d.intercepted and d.terminate
    assert d.events and d.events[0].kind == "warning"
    assert d.events[0].source == "reins.cost_budget"


def test_cost_exceed_terminates():
    ctx = _ctx(AgentConfig(), _FakeTracker(tokens=100, cost=5.0))
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert d.intercepted and d.terminate


def test_custom_limits_enforced():
    """自定义（更低）上限生效"""
    ctx = _ctx(AgentConfig(max_cost_usd=0.5, max_total_tokens=10_000),
               _FakeTracker(tokens=20_000, cost=0.1))
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert d.intercepted and d.terminate


def test_zero_disables_budget():
    """显式 0 = 禁用熔断"""
    ctx = _ctx(AgentConfig(max_cost_usd=0, max_total_tokens=0),
               _FakeTracker(tokens=10**9, cost=10**6))
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert not d.intercepted


def test_no_tracker_noop():
    ctx = _ctx(AgentConfig(), None)
    d = asyncio.run(CostBudgetRein().check(ctx))
    assert not d.intercepted


def test_breaker_registered_first_in_post_iteration():
    """priority=1：熔断先于动态预算/置信度检查（短路语义下决定 run 生死）"""
    assert CostBudgetRein().point == ReinPoint.POST_ITERATION
    assert CostBudgetRein().priority < 3  # DynamicBudgetRein priority=3


# ── driver 级：POST_ITERATION terminate 真正终止 run ──


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    def assemble(self, ctx):
        return list(ctx.messages)

    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    def schemas(self):
        return []

    async def execute(self, tc, ctx, auto_rollback=True):
        return ToolResult(success=True, output="ok", duration_ms=1), None


class _StubStirrups:
    """每轮都返回一个工具调用（让流程走到 POST_ITERATION）"""

    async def call_llm(self, ctx, messages, tools_schema):
        return LLMOutput(tool_calls=[ToolCall(id="tc1", name="shell_exec",
                                              args={"command": "echo hi"})])

    async def undo_all(self, ctx):
        return None


class _StubReins:
    """POST_ITERATION 交给真实 CostBudgetRein 判定，其余点返回默认"""

    def __init__(self, post_rein: CostBudgetRein):
        self._post = post_rein
        self.agent_failed = 0

    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        if point == ReinPoint.POST_ITERATION:
            return await self._post.check(ctx)
        if point == ReinPoint.AGENT_FAILED:
            self.agent_failed += 1
        return ReinDecision()


class _StubHarness:
    def __init__(self, tracker: _FakeTracker, post_rein: CostBudgetRein):
        self.telemetry = _NoopTelemetry()
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.stirrups = _StubStirrups()
        self.cost_tracker = tracker
        self.reins = _StubReins(post_rein)


async def _collect(harness: _StubHarness, ctx: RunContext):
    driver = Driver()
    events = []
    async for ev in driver.drive(ctx, harness):
        events.append(ev)
    return events


def test_driver_cost_breaker_terminates_run():
    """累计 token 超默认上限 → 第一轮末熔断，run 以 FAILED 终止"""
    ctx = _ctx(AgentConfig(max_iterations=10, enable_intention_guide=False),
               _FakeTracker(tokens=9_999_999, cost=99.0))
    harness = _StubHarness(ctx.metadata["cost_tracker"], CostBudgetRein())
    events = asyncio.run(_collect(harness, ctx))
    assert ctx.state == State.FAILED, f"熔断后应为 FAILED，实际 {ctx.state}"
    assert harness.reins.agent_failed == 1
    # 只跑了 1 轮就终止（而非 10 轮）
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].payload.get("iterations") == 1
    assert done[-1].payload.get("state") == "failed"
    # 熔断事件可见
    assert any(e.kind == "warning" and e.source == "reins.cost_budget" for e in events)


def test_driver_within_budget_runs_to_completion():
    """预算内 run 不受影响（跑满 max_iterations 正常终止）"""
    ctx = _ctx(AgentConfig(max_iterations=3, enable_intention_guide=False),
               _FakeTracker(tokens=1000, cost=0.001))
    harness = _StubHarness(ctx.metadata["cost_tracker"], CostBudgetRein())
    events = asyncio.run(_collect(harness, ctx))
    assert ctx.state == State.MAX_REACHED, f"预算内应跑满步数，实际 {ctx.state}"
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].payload.get("iterations") == 3
