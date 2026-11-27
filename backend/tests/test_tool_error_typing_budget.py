"""工具错误类型化 + 重试预算/参数错熔断回归。

类型由执行边界写在 ToolResult.error_kind，分类器优先采信；文案关键字只兜底。
重试前检查任务级 deadline；连续参数错误达到阈值走既有 forced_convergence 通道收尾。
"""
import asyncio
import time

from agent.harness.contracts import ErrorType, RunContext
from agent.harness.stirrups import ErrorClassifier, note_tool_outcome, retry_with_backoff
from agent.tools import ToolContext, ToolResult, create_registry


def _run(coro):
    return asyncio.run(coro)


def test_error_kind_wins_over_error_text_keywords():
    """同一句含 connection 的文案，标成参数错就必须按参数错处理。"""
    clf = ErrorClassifier()
    assert clf.classify_tool_error(
        "connection reset", error_kind=ErrorType.INVALID_ARGS.value,
    ) is ErrorType.INVALID_ARGS
    assert clf.classify_tool_error(
        "参数校验失败", error_kind=ErrorType.TRANSIENT.value,
    ) is ErrorType.TRANSIENT
    # 未知类型回退关键字，不因新增字段丢失老行为
    assert clf.classify_tool_error(
        "connection reset", error_kind="not-a-kind",
    ) is ErrorType.TRANSIENT


def test_registry_marks_deterministic_failures():
    reg = create_registry()
    unknown = _run(reg.execute("no_such_tool", {}, ToolContext()))
    assert unknown.error_kind == ErrorType.INVALID_ARGS.value
    bad_args = _run(reg.execute("shell_exec", {}, ToolContext()))
    assert bad_args.error_kind == ErrorType.INVALID_ARGS.value


def test_retry_skipped_when_task_deadline_already_passed():
    calls: list[int] = []

    async def func():
        calls.append(1)
        return ToolResult(
            success=False, output="", error="x", error_kind=ErrorType.TRANSIENT.value)

    result, err = _run(retry_with_backoff(
        func, max_retries=3, base_delay=0.01,
        deadline_mono=time.monotonic() - 1,
    ))
    assert calls == [], "预算已耗尽时一次都不该执行"
    assert result is None and err


def test_retry_does_not_sleep_past_task_deadline():
    calls: list[int] = []

    async def func():
        calls.append(1)
        return ToolResult(
            success=False, output="", error="connection reset",
            error_kind=ErrorType.TRANSIENT.value,
        )

    deadline = time.monotonic() + 0.02
    result, err = _run(retry_with_backoff(
        func, max_retries=3, base_delay=1.0, deadline_mono=deadline,
    ))
    assert len(calls) == 1, "退避会越过 deadline 时不得发起第二次尝试"
    assert result is not None and err


def test_explicit_retryable_false_overrides_kind():
    calls: list[int] = []

    async def func():
        calls.append(1)
        return ToolResult(
            success=False, output="", error="connection reset",
            error_kind=ErrorType.TRANSIENT.value, retryable=False,
        )

    _run(retry_with_backoff(func, max_retries=3, base_delay=0.01))
    assert len(calls) == 1, "工具显式声明不可重试时不得重试"


def test_consecutive_arg_errors_trip_circuit_and_success_resets():
    ctx = RunContext.new("参数错熔断")
    note_tool_outcome(ctx, ErrorType.INVALID_ARGS)
    note_tool_outcome(ctx, ErrorType.INVALID_ARGS)
    assert ctx.metadata.get("arg_error_circuit_tripped") is not True
    note_tool_outcome(ctx, ErrorType.INVALID_ARGS)
    assert ctx.metadata["arg_error_circuit_tripped"] is True
    assert ctx.metadata["consecutive_invalid_args"] == 3
    note_tool_outcome(ctx, None)
    assert ctx.metadata["consecutive_invalid_args"] == 0
