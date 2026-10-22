"""回归测试：ConfidenceMonitor 误报修复

修复前三个缺陷（均为运维场景常见组合，非"Agent 真困惑"信号）：
1. 探针类命令 exit 非零（grep 无匹配/test 为假等）被当作工具失败 -0.2 ——
   shell_exec 失败时 output 仍含 [exit_code=N] 哨兵，证明命令确已执行；
2. 同一 assistant 消息在后续多次 post_iteration 里被重复扣分；
3. 连续 3 次 <0.4 即求助，阈值过短。

2026-10-22 追加：prod 策略拒绝（未预授权）豁免 ——
4. 策略拒绝条目不计入置信度扣分（政策堵路 ≠ 模型困惑）；
5. 连续 3 次策略拒绝触发专用求助卡（说真话：预登记 allowlist），
   不再等低置信度兜底发「连续多次低置信度」误诊卡。

运行: python -m pytest backend/tests/test_confidence_monitor_false_positive.py -v
"""
import re

import pytest

from agent.harness.contracts import POLICY_DENY_MARKER, RunContext, is_policy_denial
from agent.harness.reins.flow import ConfidenceMonitor

_EXIT_SENTINEL_RE = re.compile(r"\[exit_code=-?\d+\]")


def _make_ctx() -> RunContext:
    return RunContext.new(task="排查任务")


def _add_assistant(ctx: RunContext, content: str) -> None:
    ctx.messages.append({"role": "assistant", "content": content})


def _log_call(
    ctx: RunContext,
    *,
    tool: str = "file_read",
    success: bool = False,
    output: str = "",
    args: dict | None = None,
    intercept_reason: str = "",
) -> None:
    entry = {
        "tool": tool,
        "args": args if args is not None else {"path": "/etc/hosts"},
        "iteration": len(ctx.tool_calls_log),
        "success": success,
        "output": output,
    }
    if intercept_reason:
        entry["intercept_reason"] = intercept_reason
    ctx.tool_calls_log.append(entry)


def _log_policy_denial(ctx: RunContext, cmd: str = "rm -rf /data/tmp") -> None:
    """模拟 driver 拦截分支落库的 prod 未预授权条目（reason 由 guards.py 拼装）"""
    _log_call(
        ctx, tool="shell_exec", success=False, output="",
        args={"command": cmd},
        intercept_reason=f"生产环境危险命令{POLICY_DENY_MARKER}: find -delete (…)",
    )


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


# ── 2026-10-22：prod 策略拒绝豁免与专用求助卡 ──

@pytest.mark.asyncio
async def test_policy_denials_exempt_and_fire_allowlist_card():
    """连续 prod 未预授权拒绝：置信度不扣分，第 3 次发专用卡（预登记 allowlist）。

    修复前：每次拒绝按真实失败 -0.25，5 轮后低置信度兜底发「连续多次低置信度」
    误诊卡（截图事故）。修复后：豁免扣分 + 第 3 次连续拒绝直接说真话。
    """
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    decisions = []
    for i in range(4):
        _add_assistant(ctx, f"再试一个变体{i}")
        _log_policy_denial(ctx, cmd=f"变体命令{i}")
        decisions.append(await mon.check(ctx))
    # 豁免：策略拒绝不计失败，置信度保持基线 0.8
    assert ctx.metadata["confidence_history"] == pytest.approx([0.8, 0.8, 0.8, 0.8])
    # 第 3 次连续拒绝 → 专用求助卡（比低置信度兜底的 5 次更早收口）
    assert not any(_is_help(d) for d in decisions[:2])
    assert _is_help(decisions[2])
    assert decisions[2].intercepted is True
    card = next(e for e in decisions[2].events if e.kind == "human_help_needed")
    assert "allowlist" in card.payload["reason"]
    assert "登记" in card.payload["reason"]
    # 每 run 一次：第 4 轮不再发
    assert not _is_help(decisions[3])
    # 低置信度误诊路径全程未触发
    assert not ctx.metadata.get("confidence_help_requested")


@pytest.mark.asyncio
async def test_policy_denial_streak_resets_after_success():
    """拒绝序列中夹一次成功调用 → 连续计数清零，不发专用卡"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    for i in range(5):
        _add_assistant(ctx, f"步骤{i}")
        if i == 2:
            _log_call(ctx, tool="shell_exec", success=True, output="ok[exit_code=0]")
        else:
            _log_policy_denial(ctx)
        assert not _is_help(await mon.check(ctx))
    assert not ctx.metadata.get("policy_help_requested")


@pytest.mark.asyncio
async def test_non_policy_interception_still_counts_as_failure():
    """非策略类拦截（用户拒绝过的命令重发）仍按真实失败扣分，不豁免"""
    ctx = _make_ctx()
    mon = ConfidenceMonitor()
    _add_assistant(ctx, "重试被拒命令")
    _log_call(ctx, tool="shell_exec", success=False, output="",
              args={"command": "rm -rf /tmp/x"},
              intercept_reason="命令曾被用户拒绝: rm -rf /tmp/x")
    decision = await mon.check(ctx)
    assert ctx.metadata["confidence_history"][-1] == pytest.approx(0.55)
    assert not _is_help(decision)


def test_is_policy_denial_contract():
    """豁免判定契约：success=False + reason 含标记才成立，其余一律不算"""
    assert is_policy_denial({
        "success": False,
        "intercept_reason": f"生产环境危险命令{POLICY_DENY_MARKER}: x",
    })
    assert is_policy_denial({
        "success": False,
        "intercept_reason": f"生产环境危险工具{POLICY_DENY_MARKER}: ops_file",
    })
    # 非策略拒绝 / 缺 reason / 未执行 / 成功调用 → 都不算
    assert not is_policy_denial({"success": False, "intercept_reason": "命令曾被用户拒绝: x"})
    assert not is_policy_denial({"success": False})
    assert not is_policy_denial({"success": None, "intercept_reason": POLICY_DENY_MARKER})
    assert not is_policy_denial({"success": True, "intercept_reason": POLICY_DENY_MARKER})
