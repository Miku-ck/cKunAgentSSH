"""假完成强校验（CompletionGuard）回归。

背景：有 workflow goal 的 run 已有 goal 自检锚；无 goal 的 run「宣告完成」
的唯一凭据是模型自己不调工具了——答非所问也会被放行。修复后：收尾时用
一次轻量 LLM 裁决把钉扎任务原文与最终答案对表：
- FAIL → 注入纠偏 user 消息续跑（每 run 一次，第二次放行防死循环）
- PASS / 超时 / 异常 / 解析失败 → fail-open 放行
- trivial/simple 不触发；裁决期间 chunk 回调临时摘除并恢复

运行: python -m pytest backend/tests/test_completion_guard.py -v
"""
import asyncio

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
import agent.harness.driver as driver_mod


class _FakeLevel:
    def __init__(self, value: str):
        self.value = value


class _FakeIntention:
    """最小意图替身：带 driver 意图接线段真正访问的全部属性"""

    def __init__(self, level: str = "moderate"):
        self.level = _FakeLevel(level)
        self.task_type = None            # ≠ TaskType.CONVERSATION → 工具保留
        self.is_change = False
        self.reasoning_budget = None     # → 预算调整路径提前返回，不改动配置
        self.allow_planning_phase = False
        self.guide_prompt = ""           # 引导语为空 → driver 不注入 [意图引导]

    def to_dict(self):
        return {"level": self.level.value}


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


class _JudgeClient:
    """裁决客户端替身：按预设逐次返回；记录调用；可注入延迟/异常"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._on_chunk = None            # driver 会临时摘/还 chunk 回调
        self._delay = 0.0
        self._explode = False

    async def call(self, messages=None, tools=None, model=None,
                   temperature=None, max_tokens=None, enable_thinking=None,
                   **kw):
        self.calls.append({
            "messages": messages, "tools": tools, "max_tokens": max_tokens,
        })
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._explode:
            raise RuntimeError("judge exploded")
        reply = self.replies.pop(0) if self.replies else "PASS"
        return LLMOutput(content=reply)


class _GuardStirrups:
    """第 1 轮返回工具调用，之后返回最终文本；携带裁决客户端"""

    def __init__(self, llm_client: _JudgeClient):
        self.llm_client = llm_client
        self.calls = 0

    async def call_llm(self, ctx, messages, tools_schema):
        self.calls += 1
        if self.calls == 1:
            return LLMOutput(tool_calls=[ToolCall(
                id="tc1", name="shell_exec", args={"command": "echo hi"})])
        return LLMOutput(content="最终答案：服务已恢复，根因是配置回滚")

    async def undo_all(self, ctx):
        return None


class _StubReins:
    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        return ReinDecision()


class _StubHarness:
    def __init__(self, stirrups: _GuardStirrups):
        self.telemetry = _NoopTelemetry()
        self.bridle = _StubBridle()
        self.saddle = _StubSaddle()
        self.stirrups = stirrups
        self.cost_tracker = None
        self.reins = _StubReins()


@pytest.fixture(autouse=True)
def _moderate_intention(monkeypatch):
    """把意图级联替换为 moderate 替身（真实 classify 对短任务会判 trivial）。

    Phase 1 起 driver 的意图扩展点是 resolve_intention（级联：
    规则 → LLM 校准 → 澄清），不再直连 classify —— 补丁必须跟着走，
    否则替身不生效、真 classify 把任务判成 DEBUG，guard 门槛
    （仅 moderate/complex）就再也触发不了。
    """
    async def _fake_resolve(ctx, telemetry=None, max_budget=None):
        return _FakeIntention("moderate")
    monkeypatch.setattr(driver_mod, "resolve_intention", _fake_resolve)


async def _drive(harness: _StubHarness, ctx: RunContext):
    driver = Driver()
    events = []
    async for ev in driver.drive(ctx, harness):
        events.append(ev)
    return events


def _run(replies, max_iterations: int = 3, **config_kw):
    judge = _JudgeClient(replies)
    ctx = RunContext.new(
        task="排查生产服务器 502 并恢复服务",
        config=AgentConfig(max_iterations=max_iterations,
                           enable_intention_guide=True, **config_kw))
    harness = _StubHarness(_GuardStirrups(judge))
    events = asyncio.run(_drive(harness, ctx))
    return ctx, harness, judge, events


def _injected_messages(ctx: RunContext):
    return [m for m in ctx.messages
            if m.get("role") == "user" and "[系统拦截]" in m.get("content", "")]


def test_fail_bounces_then_second_completes():
    """FAIL → 注入纠偏续跑；_completion_guarded 置位后第二次直接放行"""
    ctx, harness, judge, events = _run(["FAIL\n缺少重启后的验证", "PASS"])
    assert ctx.state == State.COMPLETED
    assert harness.stirrups.calls == 3          # 工具轮 + 被弹回轮 + 补充轮
    assert len(judge.calls) == 1                     # 每 run 至多一次裁决
    assert judge.calls[0]["max_tokens"] == 64   # 轻量裁决：小 max_tokens
    assert judge.calls[0]["tools"] == []
    injected = _injected_messages(ctx)
    assert injected and "缺少重启后的验证" in injected[0]["content"]
    assert any(e.kind == "warning" and e.source == "driver.completion_guard"
               for e in events)


def test_pass_completes_without_injection():
    ctx, harness, judge, events = _run(["PASS"])
    assert ctx.state == State.COMPLETED
    assert harness.stirrups.calls == 2
    assert len(judge.calls) == 1
    assert not _injected_messages(ctx)
    assert not [e for e in events if e.source == "driver.completion_guard"]


def test_trivial_level_skips():
    """trivial/simple 不触发裁决（不值得多花一次调用）"""
    monkeypatch_level = "trivial"

    async def _fake_resolve(ctx, telemetry=None, max_budget=None):
        return _FakeIntention(monkeypatch_level)
    driver_mod.resolve_intention = _fake_resolve  # noqa: 覆盖 autouse fixture 的 moderate
    try:
        ctx, harness, judge, events = _run(["FAIL\n whatever"])
    finally:
        async def _restore(ctx, telemetry=None, max_budget=None):
            return _FakeIntention("moderate")
        driver_mod.resolve_intention = _restore
    assert ctx.state == State.COMPLETED
    assert len(judge.calls) == 0
    assert not _injected_messages(ctx)


def test_timeout_fail_open():
    """裁决超时 → fail-open 放行，绝不阻断收尾"""
    judge = _JudgeClient(["FAIL\n永远等不到"])
    judge._delay = 0.5
    ctx = RunContext.new(
        task="排查生产服务器 502 并恢复服务",
        config=AgentConfig(max_iterations=3, enable_intention_guide=True,
                           completion_guard_timeout_s=0.05))
    harness = _StubHarness(_GuardStirrups(judge))
    events = asyncio.run(_drive(harness, ctx))
    assert ctx.state == State.COMPLETED
    assert len(judge.calls) == 1
    assert not _injected_messages(ctx)


def test_malformed_output_fail_open():
    """输出不合约定（无 PASS/FAIL）→ fail-open 放行"""
    ctx, harness, judge, events = _run(["也许算完成了吧"])
    assert ctx.state == State.COMPLETED
    assert len(judge.calls) == 1
    assert not _injected_messages(ctx)


def test_judge_exception_fail_open():
    """裁决客户端抛异常 → fail-open 放行"""
    judge = _JudgeClient(["FAIL\n exploded"])
    judge._explode = True
    ctx = RunContext.new(
        task="排查生产服务器 502 并恢复服务",
        config=AgentConfig(max_iterations=3, enable_intention_guide=True))
    harness = _StubHarness(_GuardStirrups(judge))
    events = asyncio.run(_drive(harness, ctx))
    assert ctx.state == State.COMPLETED
    assert len(judge.calls) == 1
    assert not _injected_messages(ctx)


def test_chunk_callback_restored():
    """裁决期间摘掉的 chunk 回调必须在 finally 中还原"""
    sentinel = object()
    judge = _JudgeClient(["PASS"])
    judge._on_chunk = sentinel
    ctx = RunContext.new(
        task="排查生产服务器 502 并恢复服务",
        config=AgentConfig(max_iterations=3, enable_intention_guide=True))
    harness = _StubHarness(_GuardStirrups(judge))
    asyncio.run(_drive(harness, ctx))
    assert judge._on_chunk is sentinel


def test_goal_anchor_exclusive():
    """有 workflow goal 的 run 走 goal 自检锚，guard 不重复裁决"""
    import agent.workflow as wf_mod
    monkeypatch_target = wf_mod
    orig = wf_mod.goal_self_check_prompt
    wf_mod.goal_self_check_prompt = lambda goal: "请对照目标自检"
    try:
        judge = _JudgeClient(["FAIL\n不该被调到"])
        ctx = RunContext.new(
            task="排查生产服务器 502 并恢复服务",
            config=AgentConfig(max_iterations=3, enable_intention_guide=True))
        ctx.metadata["_goal_text"] = "服务恢复且 502 归零"
        harness = _StubHarness(_GuardStirrups(judge))
        asyncio.run(_drive(harness, ctx))
        assert len(judge.calls) == 0
        assert ctx.state == State.COMPLETED
    finally:
        wf_mod.goal_self_check_prompt = orig


def test_disabled_config_skips():
    ctx, harness, judge, events = _run(
        ["FAIL\n whatever"], completion_guard_enabled=False)
    assert ctx.state == State.COMPLETED
    assert len(judge.calls) == 0
