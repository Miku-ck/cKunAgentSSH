"""工具统一硬时限与取消回收。

ToolRegistry.execute 为所有工具罩一层由 ToolMeta.timeout 派生的 deadline：
到点必须取消工具协程、等它的 finally 跑完再返回失败结果，不能把取消半途丢给事件循环。
这里钉四件事：超时结果形态、清理真的执行、外层取消不被吞、正常工具不受影响。
"""
import asyncio
import time

import agent.tools as tools_module
from agent.harness.contracts import ErrorType
from agent.harness.stirrups import ErrorClassifier
from agent.tools import BaseTool, ToolContext, ToolMeta, ToolResult, ToolRegistry


def _run(coro):
    return asyncio.run(coro)


class _HangingTool(BaseTool):
    name = "hanging"
    meta = ToolMeta(risk_level="safe", idempotent=True, timeout=0.05)

    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    async def execute(self, context, **kwargs) -> ToolResult:
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("cleaned")
        return ToolResult(success=True, output="unreachable")


class _FastTool(BaseTool):
    name = "fast"
    meta = ToolMeta(risk_level="safe", idempotent=True, timeout=5.0)

    async def execute(self, context, **kwargs) -> ToolResult:
        return ToolResult(success=True, output="ok")


def test_timeout_returns_failure_result_after_cleanup():
    """超时返回结构化失败结果，且工具的 finally 在返回前已执行。"""
    events: list[str] = []
    reg = ToolRegistry()
    reg.register(_HangingTool(events))

    res = _run(reg.execute("hanging", {}, ToolContext()))

    assert res.success is False
    assert "hanging" in res.error and "超时" in res.error
    assert events == ["cleaned"], "超时后必须等工具清理完成再返回"
    assert res.priority == "high"
    assert res.error_kind == ErrorType.TRANSIENT.value


def test_timeout_error_is_classified_as_transient():
    """统一超时错误必须能被现有分类器识别为瞬时，幂等工具的既有重试路径才不失效。"""
    events: list[str] = []
    reg = ToolRegistry()
    reg.register(_HangingTool(events))
    res = _run(reg.execute("hanging", {}, ToolContext()))
    assert ErrorClassifier.classify_tool_error(res.error) is ErrorType.TRANSIENT


def test_outer_cancellation_reaches_tool_and_still_propagates():
    """外层 run 取消必须传给工具并最终向外抛，不能被超时兜底吞掉。"""
    started = asyncio.Event()
    events: list[str] = []

    class _CancelMe(BaseTool):
        name = "cancel_me"
        meta = ToolMeta(risk_level="safe", timeout=30.0)

        async def execute(self, context, **kwargs) -> ToolResult:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                events.append("cleaned")
            return ToolResult(success=True, output="unreachable")

    reg = ToolRegistry()
    reg.register(_CancelMe())

    async def scenario():
        task = asyncio.create_task(reg.execute("cancel_me", {}, ToolContext()))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    assert _run(scenario()) == "cancelled"
    assert events == ["cleaned"]


def test_cleanup_wait_is_bounded_even_if_tool_ignores_cancel(monkeypatch):
    """工具清理慢于回收窗口时也必须返回，超时不能变成另一种无限等待。"""
    monkeypatch.setattr(tools_module, "_TOOL_CLEANUP_GRACE_S", 0.02)
    events: list[str] = []

    class _Stubborn(BaseTool):
        name = "stubborn"
        meta = ToolMeta(risk_level="safe", timeout=0.05)

        async def execute(self, context, **kwargs) -> ToolResult:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.15)
                events.append("late_cleanup")
                raise
            return ToolResult(success=True, output="unreachable")

    reg = ToolRegistry()
    reg.register(_Stubborn())

    async def scenario():
        res = await reg.execute("stubborn", {}, ToolContext())
        await asyncio.sleep(0.2)  # 让被放弃的任务自然收尾，避免 loop 关闭时告警
        return res

    t0 = time.monotonic()
    res = _run(scenario())
    assert res.success is False and "超时" in res.error
    assert time.monotonic() - t0 < 1.0, "回收窗口必须是有界的"
    assert events == ["late_cleanup"]


def test_fast_tool_is_unaffected_by_deadline():
    """正常工具在 deadline 内返回真结果，不被包装层改写。"""
    reg = ToolRegistry()
    reg.register(_FastTool())
    res = _run(reg.execute("fast", {}, ToolContext()))
    assert res.success is True and res.output == "ok"
