"""任务级墙钟 deadline（WallClockRein）回归。

背景：单命令 60s 超时只兜住一条命令挂死，任务级时间维度此前无硬上限；
步数/token 熔断对"多轮短步数、每步都合法"的慢性失控可能过宽。
修复后：WallClockRein（POST_ITERATION, priority=2）按 ctx.metadata
["run_started_mono"]（driver 主循环入口打点）计算 elapsed：
- elapsed ≥ 上限 80% → 一次性注入收尾劝导（inject_messages，metadata 防重发）
- elapsed ≥ 上限 → intercepted + terminate + 审计（wall_clock_breaker/blocked）
- max_wall_clock_s=0 → 显式禁用；打点损坏 fail-open（跳过检查不误杀）

运行: python -m pytest backend/tests/test_wall_clock_rein.py -v
"""
import asyncio
import time
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
from agent.harness.reins import WallClockRein
import agent.harness.reins.flow as _flow_mod


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
    monkeypatch.setattr(_flow_mod, "append_record_async", _noop_audit)


def _ctx(config: AgentConfig, started_delta_s: float = 0.0) -> RunContext:
    ctx = RunContext.new(task="测试任务", config=config)
    ctx.metadata["run_started_mono"] = time.monotonic() - started_delta_s
    return ctx


# ── 单元：WallClockRein 判定 ──

def test_within_limit_no_intercept():
    ctx = _ctx(AgentConfig(max_wall_clock_s=1800))
    d = asyncio.run(WallClockRein().check(ctx))
    assert not d.intercepted and not d.terminate
    assert not d.inject_messages


def test_soft_threshold_injects_once():
    """elapsed ≥ 80% 上限 → 注入一次劝导；metadata 标记后不再重发"""
    rein = WallClockRein()
    ctx = _ctx(AgentConfig(max_wall_clock_s=100), started_delta_s=85.0)
    d = asyncio.run(rein.check(ctx))
    assert not d.intercepted and not d.terminate
    assert d.inject_messages and "收尾" in d.inject_messages[0]["content"]
    # 第二次检查：已标记，不再注入
    d2 = asyncio.run(rein.check(ctx))
    assert not d2.inject_messages


def test_hard_threshold_terminates():
    ctx = _ctx(AgentConfig(max_wall_clock_s=100), started_delta_s=120.0)
    d = asyncio.run(WallClockRein().check(ctx))
    assert d.intercepted and d.terminate
    assert d.events and d.events[0].kind == "warning"
    assert d.events[0].source == "reins.wall_clock"


def test_hard_threshold_skips_soft_injection():
    """elapsed 直接越硬阈值时不走软预警（终止决策优先，短路语义）"""
    ctx = _ctx(AgentConfig(max_wall_clock_s=100), started_delta_s=120.0)
    d = asyncio.run(WallClockRein().check(ctx))
    assert d.terminate and not d.inject_messages


def test_zero_disables():
    ctx = _ctx(AgentConfig(max_wall_clock_s=0), started_delta_s=10_000.0)
    d = asyncio.run(WallClockRein().check(ctx))
    assert not d.intercepted


def test_missing_stamp_noop():
    """无打点 → 跳过检查（防御缺打点的路径）"""
    ctx = RunContext.new(task="测试任务", config=AgentConfig())
    ctx.metadata.pop("run_started_mono", None)
    d = asyncio.run(WallClockRein().check(ctx))
    assert not d.intercepted


def test_bad_stamp_fail_open():
    """打点损坏（类型错）→ fail-open 跳过，不误杀"""
    ctx = _ctx(AgentConfig(max_wall_clock_s=100), started_delta_s=1.0)
    ctx.metadata["run_started_mono"] = "not-a-number"
    d = asyncio.run(WallClockRein().check(ctx))
    assert not d.intercepted


def test_soft_ratio_below_no_injection():
    """elapsed 未达软阈值 → 不注入"""
    ctx = _ctx(AgentConfig(max_wall_clock_s=100), started_delta_s=50.0)
    d = asyncio.run(WallClockRein().check(ctx))
    assert not d.inject_messages and not d.intercepted


def test_point_and_priority():
    """priority=2：介于 CostBudgetRein(1) 与 DynamicBudgetRein(3) 之间"""
    assert WallClockRein().point == ReinPoint.POST_ITERATION
    assert 1 < WallClockRein().priority < 3


# ── driver 级：打点 + POST_ITERATION terminate 真正终止 run ──


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    async def assemble(self, ctx):
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
    """POST_ITERATION 交给真实 WallClockRein 判定，其余点返回默认"""

    def __init__(self, post_rein: WallClockRein):
        self._post = post_rein
        self.agent_failed = 0

    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        if point == ReinPoint.POST_ITERATION:
            return await self._post.check(ctx)
        if point == ReinPoint.AGENT_FAILED:
            self.agent_failed += 1
        return ReinDecision()


class _StubHarness:
    def __init__(self, tracker: _FakeTracker, post_rein: WallClockRein):
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


def test_driver_wall_clock_breaker_terminates_run():
    """预置打点已超限（续跑场景 setdefault 不覆盖）→ 第一轮末熔断，FAILED 终止"""
    config = AgentConfig(max_iterations=10, enable_intention_guide=False)
    ctx = RunContext.new(task="测试任务", config=config)
    ctx.metadata["run_started_mono"] = time.monotonic() - 10_000.0
    harness = _StubHarness(_FakeTracker(tokens=100, cost=0.0), WallClockRein())
    events = asyncio.run(_collect(harness, ctx))
    assert ctx.state == State.FAILED, f"熔断后应为 FAILED，实际 {ctx.state}"
    assert harness.reins.agent_failed == 1
    # 只跑了 1 轮就终止（而非 10 轮）
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].payload.get("iterations") == 1
    assert done[-1].payload.get("state") == "failed"
    # 熔断事件可见
    assert any(e.kind == "warning" and e.source == "reins.wall_clock" for e in events)


def test_driver_stamps_start_time_without_overwriting():
    """driver 入口打点用 setdefault：续跑预置值不被覆盖"""
    config = AgentConfig(max_iterations=1, enable_intention_guide=False)
    ctx = RunContext.new(task="测试任务", config=config)
    preset = time.monotonic() - 123.0
    ctx.metadata["run_started_mono"] = preset
    harness = _StubHarness(_FakeTracker(tokens=100, cost=0.0), WallClockRein())
    asyncio.run(_collect(harness, ctx))
    assert ctx.metadata["run_started_mono"] == preset


def test_driver_within_limit_runs_to_completion():
    """墙钟内 run 不受影响（跑满步数正常终止）

    强制收敛轮（Phase 2）后，最后一轮物理禁工具：stub 每轮都返回 tool_calls，
    最后一轮的幻觉调用会被丢弃并走收尾分支 → 终态 COMPLETED（不再是 MAX_REACHED）。
    """
    config = AgentConfig(max_iterations=3, enable_intention_guide=False)
    ctx = RunContext.new(task="测试任务", config=config)
    ctx.metadata["run_started_mono"] = time.monotonic()
    harness = _StubHarness(_FakeTracker(tokens=100, cost=0.0), WallClockRein())
    events = asyncio.run(_collect(harness, ctx))
    assert ctx.state == State.COMPLETED, f"墙钟内应跑满步数，实际 {ctx.state}"
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].payload.get("iterations") == 3
