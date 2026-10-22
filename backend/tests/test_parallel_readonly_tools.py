"""driver 只读工具并行化 —— 连续只读段并发、副作用屏障、顺序红线。

背景：LLM 一轮常连发多个只读观测（sys_overview + process_inspect + log_read…），
原先严格串行，远程 RTT 线性叠加。现在连续 ≥2 个 READONLY_TOOLS 调用划为一个
并行段：段内串行逐个 PRE → asyncio.gather 并发执行 Saddle → 按原始顺序串行
POST + 审计 + 消息追加。段间以副作用工具为屏障。

本文件钉六件事：
  1. 连续只读调用真的并发（in-flight 峰值 ≥ 2，墙钟 < 串行和）；
  2. 副作用工具是天然屏障：[read, write, read] 三段严格按原顺序串行；
  3. role=tool 消息按 assistant.tool_calls 原始顺序配对（vLLM 400 红线）——
     后发先至的成员不得插队；
  4. POST_TOOL_CALL（审计链）按调用顺序触发，且每个检查点看到的
     ctx.active_tool_call 是当前成员（并行段内 [-1] 已不是当前条目）；
  5. 段内被拦截成员不执行，消息顺序不乱；PRE 注入仍落在本成员结果之前；
  6. 并行段内失败回滚按【身份】移除自己的 undo，不误删别的成员的条目。

运行: python -m pytest backend/tests/test_parallel_readonly_tools.py -v
"""
import asyncio
import time

from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    NoOpUndo,
    ReinDecision,
    ReinPoint,
    RunContext,
    ToolCall,
    ToolResult,
)
from agent.harness.driver import Driver
from agent.harness.saddle import Saddle
from agent.tools import ToolMeta, ToolRegistry


def _tc(tc_id: str, name: str) -> ToolCall:
    return ToolCall(id=tc_id, name=name, args={"path": f"/tmp/{tc_id}"})


class _NoopTelemetry:
    async def emit(self, ev):
        return None


class _StubBridle:
    def build_tool_result_message(self, tc_id, text, priority="normal"):
        return {"role": "tool", "tool_call_id": tc_id, "content": text, "priority": priority}


class _StubHarness:
    def __init__(self, saddle, reins):
        self.bridle = _StubBridle()
        self.saddle = saddle
        self.reins = reins
        self.cost_tracker = None


class _RecordingReins:
    """记录每个检查点看到的「当前调用」，并按名单拦截。"""

    def __init__(self, intercept_names=()):
        self.seen = []            # (point.value, tool_name)
        self.intercept_names = set(intercept_names)

    async def check(self, point, ctx):
        entry = ctx.active_tool_call
        tool = str(entry.get("tool") or "") if entry else ""
        self.seen.append((point.value, tool))
        if point is ReinPoint.PRE_TOOL_CALL and tool in self.intercept_names:
            return ReinDecision(intercepted=True, intercept_reason="测试拦截")
        return ReinDecision()


class _ConcurrencySaddle:
    """并发观测：in-flight 峰值 + start/end 事件序；按工具名控制耗时。"""

    def __init__(self, slow_name="file_read", slow=0.06, fast=0.005):
        self.in_flight = 0
        self.peak = 0
        self.order = []           # ("start"|"end", tc_id)
        self.slow_name = slow_name
        self.slow = slow
        self.fast = fast

    async def execute(self, tc, ctx, auto_rollback=True):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.order.append(("start", tc.id))
        await asyncio.sleep(self.slow if tc.name == self.slow_name else self.fast)
        self.order.append(("end", tc.id))
        self.in_flight -= 1
        return ToolResult(success=True, output=f"out-{tc.id}", duration_ms=1), None


def _new_ctx() -> RunContext:
    return RunContext.new(task="并行化验证", config=AgentConfig())


async def _drain(ctx, harness, llm_out):
    events = []
    async for ev in Driver()._execute_tool_calls(llm_out, ctx, harness, _NoopTelemetry(), 0):
        events.append(ev)
    return events


def _tool_msgs(ctx):
    return [m for m in ctx.messages if m.get("role") == "tool"]


# ── 1. 连续只读段真的并发 ─────────────────────────────────────────────────

def test_连续只读段并发执行():
    saddle = _ConcurrencySaddle()
    reins = _RecordingReins()
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-a", "file_read"), _tc("tc-b", "log_read")])

    t0 = time.monotonic()
    events = asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))
    elapsed = time.monotonic() - t0

    assert saddle.peak == 2, f"两个只读调用应并发执行，实测 in-flight 峰值 {saddle.peak}"
    # 串行 ≈ slow+fast；并发 ≈ max。给调度留余量，避免偶发抖动误报。
    assert elapsed < saddle.slow + saddle.fast + 0.04, \
        f"墙钟 {elapsed:.3f}s 接近串行和，并行未生效"
    # 两个 start 都先于任一 end（并行语义的自然事件序）
    starts = [i for i, (kind, _) in enumerate(saddle.order) if kind == "start"]
    ends = [i for i, (kind, _) in enumerate(saddle.order) if kind == "end"]
    assert max(starts) < min(ends), f"start 事件应全部先于 end：{saddle.order}"
    assert len(events) >= 4  # 2×(tool_call_start + tool_call_result)


# ── 2. 副作用工具是屏障 ──────────────────────────────────────────────────

def test_副作用工具是屏障():
    saddle = _ConcurrencySaddle()
    reins = _RecordingReins()
    ctx = _new_ctx()
    # read → write(副作用) → read：write 前后的 read 不得被交换进同一批
    llm_out = LLMOutput(tool_calls=[
        _tc("tc-a", "file_read"),
        _tc("tc-w", "file_write"),
        _tc("tc-b", "log_read"),
    ])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    # 三个调用各自独立成段，逐个完成：不存在任何 start 重叠
    timeline = saddle.order
    for i in range(len(timeline) - 1):
        kind, _ = timeline[i]
        if kind == "start":
            # 下一个事件必须是它自己的 end（串行）
            assert timeline[i + 1] == ("end", timeline[i][1]), \
                f"副作用屏障被打破：{timeline}"
    # 消息顺序 = 原始调用顺序
    assert [m["tool_call_id"] for m in _tool_msgs(ctx)] == ["tc-a", "tc-w", "tc-b"]


# ── 3. 后发先至不插队（role=tool 按原始顺序配对）─────────────────────────

def test_后发先至不插队():
    saddle = _ConcurrencySaddle()  # file_read 慢（0.06），log_read 快（0.005）
    reins = _RecordingReins()
    ctx = _new_ctx()
    # 原始顺序：慢的在前、快的在后 → 快的先完成，但消息必须仍按原始顺序
    llm_out = LLMOutput(tool_calls=[_tc("tc-slow", "file_read"), _tc("tc-fast", "log_read")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    assert [m["tool_call_id"] for m in _tool_msgs(ctx)] == ["tc-slow", "tc-fast"], \
        "role=tool 消息必须按 assistant.tool_calls 原始顺序追加（vLLM 400 红线）"
    assert _tool_msgs(ctx)[0]["content"] == "out-tc-slow"


# ── 4. 审计链顺序 + active_tool_call 指针正确 ────────────────────────────

def test_POST检查按序触发且当前调用指针正确():
    saddle = _ConcurrencySaddle()
    reins = _RecordingReins()
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-a", "file_read"), _tc("tc-b", "log_read")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    # PRE 顺序 = 原始顺序；POST 顺序 = 原始顺序（审计链红线）
    pre = [t for p, t in reins.seen if p == "pre_tool_call"]
    post = [t for p, t in reins.seen if p == "post_tool_call"]
    assert pre == ["file_read", "log_read"], f"PRE 顺序错: {pre}"
    assert post == ["file_read", "log_read"], f"POST（审计链）顺序错: {post}"


# ── 5. 段内拦截成员不执行、消息顺序不乱 ──────────────────────────────────

def test_段内被拦截成员不执行():
    saddle = _ConcurrencySaddle()
    # file_glob 被拦（排第二位），file_read 放行
    reins = _RecordingReins(intercept_names={"file_glob"})
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-a", "file_read"), _tc("tc-b", "file_glob")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    executed = [tid for kind, tid in saddle.order if kind == "start"]
    assert executed == ["tc-a"], f"被拦截成员不得执行: {executed}"
    msgs = _tool_msgs(ctx)
    assert [m["tool_call_id"] for m in msgs] == ["tc-a", "tc-b"]
    assert "测试拦截" in msgs[1]["content"]
    assert msgs[0]["content"] == "out-tc-a"
    # 被拦截成员的日志条目 success=False，放行成员 True
    by_tool = {e["tool"]: e for e in ctx.tool_calls_log}
    assert by_tool["file_read"]["success"] is True
    assert by_tool["file_glob"]["success"] is False


def test_段首成员被拦截时其余成员仍执行且消息有序():
    saddle = _ConcurrencySaddle()
    # file_read 排第一位被拦，file_glob 放行 —— 拦截消息必须仍排在结果之前
    reins = _RecordingReins(intercept_names={"file_read"})
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-a", "file_read"), _tc("tc-b", "file_glob")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    msgs = _tool_msgs(ctx)
    assert [m["tool_call_id"] for m in msgs] == ["tc-a", "tc-b"], \
        "先通过后拦截时，拦截消息不得插到前面成员的结果之前"
    assert "测试拦截" in msgs[0]["content"]
    assert msgs[1]["content"] == "out-tc-b"


# ── 6. 失败回滚按身份移除 undo ───────────────────────────────────────────

class _FakeReadTool:
    """最小只读工具：无 bound_command → Sandbox/UndoBuilder 都走默认路径。"""

    def __init__(self, name: str, raise_exc: bool = False):
        self.name = name
        self.description = "fake readonly"
        self.parameters = {"type": "object", "properties": {}}
        self.meta = ToolMeta(risk_level="safe", reversible=True, idempotent=True, timeout=5.0)
        self._raise = raise_exc

    def bound_command(self, args):
        return None

    async def execute(self, context, **args):
        if self._raise:
            raise RuntimeError("模拟只读调用失败")
        return ToolResult(success=True, output="ok", duration_ms=1)


def test_失败回滚按身份移除undo不误删他人():
    # 单元面：remove_undo 按 is 身份定位，栈中间的条目也能精确移除
    ctx = _new_ctx()
    undo_a, undo_b, undo_c = NoOpUndo(reason="a"), NoOpUndo(reason="b"), NoOpUndo(reason="c")
    ctx.push_undo(undo_a)
    ctx.push_undo(undo_b)
    ctx.push_undo(undo_c)
    ctx.remove_undo(undo_b)          # 移除栈中间（并行时序下的真实形态）
    assert ctx.undo_stack == [undo_a, undo_c], "必须按身份移除，栈序不受影响"
    ctx.remove_undo(undo_a)          # 移除栈底
    assert ctx.undo_stack == [undo_c]
    ctx.remove_undo(NoOpUndo(reason="不存在"))   # 幂等：没有就不动
    assert ctx.undo_stack == [undo_c]


def test_真实Saddle并行段内失败不放大且undo不串():
    registry = ToolRegistry()
    registry.register(_FakeReadTool("file_read"))
    registry.register(_FakeReadTool("log_read", raise_exc=True))
    saddle = Saddle(registry=registry)
    reins = _RecordingReins()
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-ok", "file_read"), _tc("tc-bad", "log_read")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    msgs = _tool_msgs(ctx)
    assert len(msgs) == 2, "一个成员失败不得放大为整段失败"
    assert [m["tool_call_id"] for m in msgs] == ["tc-ok", "tc-bad"]
    by_tool = {e["tool"]: e for e in ctx.tool_calls_log}
    assert by_tool["file_read"]["success"] is True
    assert by_tool["log_read"]["success"] is False
    # 成功成员的 NoOpUndo 仍在栈上（执行后不移除），失败成员的已被按身份清掉
    # —— 若实现弹栈顶，失败回滚可能误删成功成员的条目（条数就会对不上）
    assert len(ctx.undo_stack) == 1, \
        f"undo 栈应只剩成功成员的条目，实际 {len(ctx.undo_stack)} 个"


# ── 7. 单条只读走串行路径（行为不变）────────────────────────────────────

def test_单条只读不并行():
    saddle = _ConcurrencySaddle()
    reins = _RecordingReins()
    ctx = _new_ctx()
    llm_out = LLMOutput(tool_calls=[_tc("tc-only", "file_read")])

    asyncio.run(_drain(ctx, _StubHarness(saddle, reins), llm_out))

    assert saddle.peak == 1
    assert [m["tool_call_id"] for m in _tool_msgs(ctx)] == ["tc-only"]
    # 单条也走 active_tool_call（检查点看到正确条目）
    assert ("pre_tool_call", "file_read") in reins.seen
    assert ("post_tool_call", "file_read") in reins.seen
