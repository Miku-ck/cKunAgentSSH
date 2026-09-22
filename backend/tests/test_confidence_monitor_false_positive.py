# -*- coding: utf-8 -*-
"""回归测试：ConfidenceMonitor 误报修复

修复前三个缺陷（均为运维场景常见组合，非"Agent 真困惑"信号）：
1. 探针类命令 exit 非零（grep 无匹配/test 为假等）被当作工具失败 -0.2 ——
   shell_exec 失败时 output 仍含 [exit_code=N] 哨兵，证明命令确已执行；
2. 同一 assistant 消息在后续多次 post_iteration 里被重复扣分；
3. 连续 3 次 <0.4 即求助，阈值过短。

运行: python -m pytest backend/tests/test_confidence_monitor_false_positive.py -v
"""
import re

import pytest

from agent.harness.contracts import RunContext
from agent.harness.reins.flow import ConfidenceMonitor

_EXIT_SENTINEL_RE = re.compile(r"\[exit_code=-?\d+\]")


def _make_ctx() -> RunContext:
    return RunContext.new(task="排查任务", mode="ssh")


def _add_assistant(ctx: RunContext, content: str) -> None:
    ctx.messages.append({"role": "assistant", "content": content})


def _log_call(
    ctx: RunContext,
    *,
    tool: str = "file_read",
    success: bool = False,
    output: str = "",
    args: dict | None = None,
) -> None:
    ctx.tool_calls_log.append({
        "tool": tool,
        "args": args if args is not None else {"path": "/etc/hosts"},
        "iteration": len(ctx.tool_calls_log),
        "success": success,
        "output": output,
    })


def _is_help(decision) -> bool:
    return any(e.kind == "human_help_needed" for e in decision.events)


# ── 缺陷 1：探针非零退出不算失败 ──

@pytest.mark.asyncio
async def test_probe_nonzero_exit_does_not_request_help():
    """循环：每轮 grep 无匹配（exit=1，输出含哨兵）+ 无不确定词 → 永不求助"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    for _ in range(6):
        _add_assistant(ctx, "继续排查")
        _log_call(ctx, tool="shell_exec", success=False,
                  output="进程不存在\n[exit_code=1]")
        decision = await mon.check(ctx)
        assert not _is_help(decision)
        assert not decision.inject_messages


# ── 缺陷 2：同一消息不重复计分 ──

@pytest.mark.asyncio
async def test_same_assistant_message_scored_once():
    """同一最后 assistant 消息跨多次 post_iteration 只计一次分"""
    ctx = _make_ctx()
    _add_assistant(ctx, "这个问题我不确定结论")
    mon = ConfidenceMonitor()
    await mon.check(ctx)
    # 第二次：消息与工具日志均未变化 → history 不应增长
    await mon.check(ctx)
    assert len(ctx.metadata["confidence_history"]) == 1


@pytest.mark.asyncio
async def test_hedging_word_alone_neither_injects_nor_requests_help():
    """仅含"不确定"标记（无工具失败）→ 每轮新消息 0.65，不纠偏不求助"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    for i in range(6):
        _add_assistant(ctx, f"我不确定原因{i}")
        decision = await mon.check(ctx)
        assert not _is_help(decision)
        assert not decision.inject_messages


# ── 缺陷 3：真困惑信号仍要能求助（连续 5 次低分） ──

@pytest.mark.asyncio
async def test_genuine_failures_still_request_help_after_five_low_rounds():
    """每轮一个真实失败 → 第 2 轮起低分，连续 5 轮低分（第 6 轮）触发一次求助"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    decisions = []
    for i in range(6):
        _add_assistant(ctx, f"换一个方式重试{i}")
        _log_call(ctx, tool="file_read", success=False, output="",
                  args={"path": f"/missing/{i}"})
        decisions.append(await mon.check(ctx))
    # 0.55 / 0.3 / 0.1×4（0.1 为下限）：单失败 0.55 不算低分，
    # 自第 2 轮起连续 5 轮 <0.4 → 第 6 轮开枪
    assert ctx.metadata["confidence_history"] == pytest.approx(
        [0.55, 0.3, 0.1, 0.1, 0.1, 0.1]
    )
    assert not any(_is_help(d) for d in decisions[:5])
    assert _is_help(decisions[5])
    # 同一 run 只求助一次（H14 语义保留）：第 6 次低分不再发
    _add_assistant(ctx, "继续")
    _log_call(ctx, tool="file_read", success=False, output="",
              args={"path": "/missing/6"})
    assert not _is_help(await mon.check(ctx))


# ── 既有语义回归：真实失败与置信度计算 ──

@pytest.mark.asyncio
async def test_probe_failures_never_request_help():
    """6 次探针失败（各带哨兵、各轮新消息）永不触发求助"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    for i in range(6):
        _add_assistant(ctx, f"探测第{i}项")
        _log_call(ctx, tool="shell_exec", success=False,
                  output="[exit_code=1]")
        assert not _is_help(await mon.check(ctx))
    assert not ctx.metadata.get("confidence_help_requested")


def test_exit_sentinel_regex_matches_shell_failure():
    """与 shell_exec._execute_remote 契约对齐：失败结果 output 末行含 [exit_code=N]"""
    assert _EXIT_SENTINEL_RE.search("no match\n[exit_code=1]")
    assert _EXIT_SENTINEL_RE.search("ok[exit_code=0]")
    assert not _EXIT_SENTINEL_RE.search("Bridge 连接失败")
