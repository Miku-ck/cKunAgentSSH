# -*- coding: utf-8 -*-
"""C-review M12 回归：滚动再摘要真正写入 metadata，历史不退化为嵌套 200 字符切片。

修复前：_compress_run_into 只在"压缩结果含 system 消息"时写
ctx.metadata["context_summary"] — 但摘要消息 role=user 且输入已剥离全部 system，
分支恒死。摘要滞留 ctx.messages，下一轮压缩被截 200 字符再折叠 → 嵌套退化，
_build 的 [上下文摘要] 注入永远为空。
修复后：按 _SUMMARY_PREFIX 显式标记摘出摘要消息写入 metadata（有 cap），
先前摘要回喂时按 cap 完整参与滚动再摘要。

运行: python -m pytest backend/tests/test_rolling_summary_metadata.py -v
"""
from agent.harness.bridle import (
    Bridle,
    _PRIOR_SUMMARY_SENTINEL,
    _SUMMARY_PREFIX,
)
from agent.harness.contracts import AgentConfig, RunContext


def _bridle() -> Bridle:
    return Bridle(max_context_tokens=16384)


def _make_ctx(n_old: int = 20) -> RunContext:
    ctx = RunContext.new(task="测试任务", mode="ssh", config=AgentConfig())
    for i in range(n_old):
        ctx.messages.append({"role": "user", "content": f"早期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"早期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题"})
    ctx.messages.append({"role": "assistant", "content": "近期回答"})
    return ctx


def test_summary_written_to_metadata_not_messages():
    """压缩后摘要必须写入 metadata，且摘要消息不得滞留 ctx.messages"""
    b = _bridle()
    ctx = _make_ctx()
    b._compress_run_into(ctx, force=True)
    assert ctx.metadata.get("context_summary"), (
        "压缩后必须写入 ctx.metadata['context_summary']"
    )
    assert not ctx.metadata["context_summary"].startswith(_SUMMARY_PREFIX), (
        "metadata 中存储的摘要不应带标记前缀（assemble 以 [上下文摘要] 注入）"
    )
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_PREFIX)
        for m in ctx.messages
    ), "摘要消息不得滞留 ctx.messages（否则下轮压缩再截断嵌套）"


def test_rolling_resummary_keeps_full_prior():
    """滚动再摘要：新一代摘要包含完整的上一代摘要（cap 内），而非 200 字符切片"""
    b = _bridle()
    ctx = _make_ctx()
    b._compress_run_into(ctx, force=True)
    first_summary = ctx.metadata["context_summary"]
    assert first_summary and len(first_summary) > 200, "前置：第一代表征性摘要应 >200 字符"

    # 模拟 run 继续：新增早期消息 + 新一轮压缩
    for i in range(20, 40):
        ctx.messages.append({"role": "user", "content": f"二期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"二期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题2"})
    ctx.messages.append({"role": "assistant", "content": "近期回答2"})
    b._compress_run_into(ctx, force=True)
    second_summary = ctx.metadata["context_summary"]
    assert second_summary

    # M12 核心：旧摘要完整参与浓缩，不得只剩 200 字符嵌套
    assert first_summary in second_summary, (
        "第二代表征性摘要应包含完整第一代表征性摘要（滚动再摘要），"
        "而非其 200 字符切片"
    )
    # 合成前置摘要消息不得滞留 ctx.messages
    assert not any(
        isinstance(m.get("content"), str)
        and m["content"].startswith(_PRIOR_SUMMARY_SENTINEL)
        for m in ctx.messages
    ), "合成前置摘要消息（哨兵）不得进入 ctx.messages"


def test_assemble_injects_summary_from_metadata():
    """_build 必须从 metadata 重新注入 [上下文摘要]"""
    b = _bridle()
    ctx = _make_ctx()
    ctx.metadata["context_summary"] = "前期任务：排查 nginx 502"
    messages = b._build(ctx)
    contents = [m.get("content", "") for m in messages]
    assert any(
        isinstance(c, str) and c.startswith("[上下文摘要]") and "排查 nginx 502" in c
        for c in contents
    ), "assemble 必须从 metadata 重新注入摘要"


def test_no_summary_when_nothing_old():
    """无足够旧消息时不生成摘要，也不清掉既有 metadata"""
    b = _bridle()
    ctx = RunContext.new(task="测试", mode="ssh", config=AgentConfig())
    ctx.messages.append({"role": "user", "content": "唯一问题"})
    ctx.messages.append({"role": "assistant", "content": "唯一回答"})
    ctx.metadata["context_summary"] = "保留的既有摘要"
    b._compress_run_into(ctx, force=True)
    assert ctx.metadata["context_summary"] == "保留的既有摘要"
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_PREFIX)
        for m in ctx.messages
    )
