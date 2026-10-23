"""强制收敛轮（机制式）回归。

背景：软预警只是劝导，模型仍可能把最后一轮耗在工具调用上，终态落在
MAX_REACHED（用户拿到工具调用统计而非总结）。修复后：最后一轮
（remaining == 0）call_llm 不传 tools —— 模型物理上无法调工具，输出必然是
文本 → 走收尾分支，终态 COMPLETED；幻觉 tool_calls 物理丢弃；
收敛输出为空时 _build_run_summary 兜底；DynamicBudgetRein 追加预算后
remaining 重算 > 0，收敛自然顺延到新最后一轮。

运行: python -m pytest backend/tests/test_forced_convergence.py -v
"""
import asyncio
from typing import Optional

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

# 收敛轮守卫测试同时钉住哨兵 schema：非收敛轮必须原样透传
_SENTINEL_SCHEMA = [{"type": "function", "function": {"name": "shell_exec"}}]


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    async def assemble(self, ctx):
        return list(ctx.messages)

    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text}


class _StubSaddle:
    def __init__(self):
        self.executed = 0

    def schemas(self):
        return list(_SENTINEL_SCHEMA)

    async def execute(self, tc, ctx, auto_rollback=True):
        self.executed += 1
        return ToolResult(success=True, output="ok", duration_ms=1), None


class _RecordingStirrups:
    """前 N 轮返回工具调用，之后返回文本；记录每次 call_llm 的 tools 实参"""

    def __init__(self, tool_rounds: int = 99, hallucinate_last: bool = False,
                 last_content: str = "最终总结：已完成全部事项"):
        self.calls: list[list] = []
        self._round = 0
        self._tool_rounds = tool_rounds
        self._hallucinate_last = hallucinate_last
        self._last_content = last_content

    async def call_llm(self, ctx, messages, tools_schema):
        self.calls.append(list(tools_schema or []))
        self._round += 1
        if self._round <= self._tool_rounds:
            tcs = [ToolCall(id=f"tc{self._round}", name="shell_exec",
                            args={"command": "echo hi"})]
            if self._hallucinate_last and self._round == self._tool_rounds + 1:
                # 收敛轮幻觉：tools 已传空仍返回 tool_calls
                tcs.append(ToolCall(id="ghost", name="shell_exec",
                                    args={"command": "rm -rf /"}))
            return LLMOutput(tool_calls=tcs)
        return LLMOutput(content=self._last_content)

    async def undo_all(self, ctx):
        return None


class _StubReins:
    """POST_ITERATION 可注入预算追加动作（模拟 DynamicBudgetRein），其余点默认"""

    def __init__(self, extend_at: Optional[int] = None, extend_by: int = 0):
        self._extend_at = extend_at      # 第几次 POST_ITERATION（1 起）
        self._extend_by = extend_by
        self._n = 0

    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        if point == ReinPoint.POST_ITERATION:
            self._n += 1
            if self._extend_at is not None and self._n == self._extend_at:
                ctx.config.max_iterations += self._extend_by
        return ReinDecision()


class _StubHarness:
    def __init__(self, stirrups: _RecordingStirrups, reins: _StubReins):
        self.telemetry = _NoopTelemetry()
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.stirrups = stirrups
        self.cost_tracker = None
        self.reins = reins


async def _collect(harness: _StubHarness, ctx: RunContext):
    driver = Driver()
    events = []
    async for ev in driver.drive(ctx, harness):
        events.append(ev)
    return events


def _run(max_iterations: int, stirrups: _RecordingStirrups,
         reins: _StubReins = None, **config_kw):
    config = AgentConfig(max_iterations=max_iterations,
                         enable_intention_guide=False, **config_kw)
    ctx = RunContext.new(task="测试任务", config=config)
    harness = _StubHarness(stirrups, reins or _StubReins())
    events = asyncio.run(_collect(harness, ctx))
    return ctx, harness, events


def test_last_round_uses_empty_tools_and_completes():
    """最后一轮 tools=[]：物理禁工具，终态 COMPLETED（而非 MAX_REACHED）"""
    ctx, harness, events = _run(2, _RecordingStirrups(tool_rounds=1))
    assert ctx.state == State.COMPLETED, f"收敛轮应 COMPLETED，实际 {ctx.state}"
    stirrups = harness.stirrups
    assert stirrups.calls[0] == _SENTINEL_SCHEMA   # 第 1 轮正常带工具
    assert stirrups.calls[-1] == []                # 最后一轮物理禁工具
    assert harness.saddle.executed == 1            # 只执行了第 1 轮的工具
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].payload.get("iterations") == 2
    # 收敛态事件可见（复用 state kind）
    assert any(e.kind == "state" and e.payload.get("state") == "converging"
               for e in events)


def test_hallucinated_tool_calls_stripped():
    """收敛轮幻觉 tool_calls 被物理丢弃，按纯文本收尾，不执行任何工具"""
    ctx, harness, events = _run(
        2, _RecordingStirrups(tool_rounds=1, hallucinate_last=True))
    assert ctx.state == State.COMPLETED
    assert harness.saddle.executed == 1            # ghost 未被执行
    assert harness.stirrups.calls[-1] == []


def test_empty_convergence_output_builds_summary():
    """收敛输出为空 → _build_run_summary 确定性兜底，不交付空结果"""
    ctx, harness, events = _run(
        2, _RecordingStirrups(tool_rounds=1, last_content=""))
    assert ctx.state == State.COMPLETED
    summary_events = [e for e in events
                      if e.kind == "content" and e.payload.get("content")]
    assert summary_events, "空收敛输出必须有确定性摘要兜底事件"
    assert summary_events[-1].payload["content"].strip()


def test_budget_extension_postpones_convergence():
    """POST_ITERATION 追加预算 → remaining 重算，收敛顺延到新最后一轮"""
    reins = _StubReins(extend_at=1, extend_by=5)
    ctx, harness, events = _run(2, _RecordingStirrups(tool_rounds=99),
                                reins=reins)
    stirrups = harness.stirrups
    assert ctx.state == State.COMPLETED
    assert len(stirrups.calls) == 7                # 2 + 追加 5
    assert stirrups.calls[-1] == []                # 只有真最后一轮收敛
    assert sum(1 for c in stirrups.calls if c == []) == 1


def test_convergence_disabled_old_behavior():
    """关闭开关回退旧行为：最后一轮仍带工具，终态 MAX_REACHED"""
    ctx, harness, events = _run(
        2, _RecordingStirrups(tool_rounds=99),
        **{"forced_convergence_enabled": False})
    assert ctx.state == State.MAX_REACHED
    assert all(c == _SENTINEL_SCHEMA for c in harness.stirrups.calls)
    assert harness.saddle.executed == 2
