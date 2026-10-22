"""C-review M12 回归：滚动再摘要真正写入 metadata，历史不退化为嵌套 200 字符切片。

修复前：_compress_run_into 只在"压缩结果含 system 消息"时写
ctx.metadata["context_summary"] — 但摘要消息 role=user 且输入已剥离全部 system，
分支恒死。摘要滞留 ctx.messages，下一轮压缩被截 200 字符再折叠 → 嵌套退化，
_build 的 [上下文摘要] 注入永远为空。
修复后：按 _SUMMARY_PREFIX 显式标记摘出摘要消息写入 metadata（有 cap），
先前摘要回喂时按 cap 完整参与滚动再摘要。

运行: python -m pytest backend/tests/test_rolling_summary_metadata.py -v
"""
import asyncio

from agent.harness.bridle import (
    Bridle,
    _PRIOR_SUMMARY_SENTINEL,
    _SUMMARY_PREFIX,
)
from agent.harness.contracts import AgentConfig, RunContext


def _run(coro):
    """P1 起 _compress_run_into / assemble 为 async（生成式摘要需 await），测试统一驱动"""
    return asyncio.run(coro)


def _bridle() -> Bridle:
    return Bridle(max_context_tokens=16384)


def _make_ctx(n_old: int = 20) -> RunContext:
    ctx = RunContext.new(task="测试任务", config=AgentConfig())
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
    _run(b._compress_run_into(ctx, force=True))
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
    _run(b._compress_run_into(ctx, force=True))
    first_summary = ctx.metadata["context_summary"]
    assert first_summary and len(first_summary) > 200, "前置：第一代表征性摘要应 >200 字符"

    # 模拟 run 继续：新增早期消息 + 新一轮压缩
    for i in range(20, 40):
        ctx.messages.append({"role": "user", "content": f"二期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"二期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题2"})
    ctx.messages.append({"role": "assistant", "content": "近期回答2"})
    _run(b._compress_run_into(ctx, force=True))
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
    ctx = RunContext.new(task="测试", config=AgentConfig())
    ctx.messages.append({"role": "user", "content": "唯一问题"})
    ctx.messages.append({"role": "assistant", "content": "唯一回答"})
    ctx.metadata["context_summary"] = "保留的既有摘要"
    _run(b._compress_run_into(ctx, force=True))
    assert ctx.metadata["context_summary"] == "保留的既有摘要"
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_PREFIX)
        for m in ctx.messages
    )


# ── priority 的生命周期：压缩改写 ctx，剥离只该发生在发给 LLM 的边界 ──────
# build_tool_result_message 给每条 tool 结果带 priority（low=日志噪声、high=必须留全的
# 输出），compress_messages 按它差异化截断/摘要。但 compress_messages 的返回值会被
# _compress_run_into 赋回 ctx.messages —— 它顺手做的 _strip_tool_priority 于是把
# priority 从**内存态**里抹掉了。剥离本是 assemble()（唯一的 LLM 出口，stirrups 压缩
# 重试也走 set_messages_rebuilder=assemble 重取）的职责，在这里做等于把压缩器自己要用的
# 输入改坏：本 run 第一次压缩之后，high 结果被当 normal 砍到 1000 字符、low 噪声
# 被当 normal 留 1000 字符而不是省略 —— 优先级机制静默失效，且没有任何用例覆盖过。

def _tool_msgs() -> list[dict]:
    return [
        {"role": "tool", "tool_call_id": "c_low", "content": "L" * 3000, "priority": "low"},
        {"role": "tool", "tool_call_id": "c_high", "content": "H" * 6000, "priority": "high"},
    ]


def test_压缩后内存里的tool消息仍带着priority():
    b = _bridle()
    ctx = _make_ctx(n_old=20)
    for m in _tool_msgs():
        ctx.messages.append(dict(m))
    _run(b._compress_run_into(ctx, force=True))

    kept = [m for m in ctx.messages if m.get("role") == "tool"]
    assert kept, "近期 tool 结果应被保留在 ctx.messages"
    by_id = {m.get("tool_call_id"): m for m in kept}
    assert by_id["c_low"].get("priority") == "low", (
        f"压缩把 priority 从内存态抹掉了：{by_id['c_low'].get('priority')!r}")
    assert by_id["c_high"].get("priority") == "high"


def test_第二次压缩仍按priority差异化处理():
    """上一条的下游后果，用真实链路而不是手搭的输入来断言。"""
    b = _bridle()
    ctx = _make_ctx(n_old=20)
    for m in _tool_msgs():
        ctx.messages.append(dict(m))
    _run(b._compress_run_into(ctx, force=True))

    kept = [m for m in ctx.messages if m.get("role") == "tool"]
    out = b.compress_messages(kept, force=True)
    low = next(m for m in out if m.get("tool_call_id") == "c_low")
    assert low["content"] == "[low-priority result omitted]", (
        f"压缩过一次之后 low 优先级噪声就不再被省略了（实得 {len(low['content'])} 字符）："
        "priority 已被上一轮压缩从内存态里抹掉")


def test_assemble_发给llm的载荷仍然剥离priority():
    """反向锁：priority 只许留在内存态，出网前必须剥（严格 vLLM 端点对非标准字段 400）。"""
    b = _bridle()
    ctx = _make_ctx(n_old=2)
    for m in _tool_msgs():
        ctx.messages.append(dict(m))
    payload = _run(b.assemble(ctx))
    assert all("priority" not in m for m in payload), [
        m for m in payload if "priority" in m]


def test_头尾截断不得超过max_chars():
    """_truncate_head_tail 的注释写着"确保 head + tail 不超过 max_chars"，实得超了：
    head+tail 恰好等于 max_chars，再拼上省略提示 ⇒ 结果比上限长，调用方的预算被击穿。"""
    b = _bridle()
    out = b._truncate_head_tail("z" * 10_000, 1000)
    assert len(out) <= 1000, f"声称上限 1000，实得 {len(out)} 字符"
