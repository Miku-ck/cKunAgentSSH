"""P1 生成式滚动摘要测试 — LLM 浓缩早期上下文，失败静默降级抽取式。

覆盖：
1. 摘要 LLM 成功 → metadata 存生成摘要（而非抽取式拼接）
2. 摘要 LLM 抛异常 / 返回空 → 静默降级抽取式（降级铁律：绝不向调用方抛异常）
3. 滚动再摘要 → 第二次调用的 prompt 必须包含完整的上一代摘要（M12 语义延续）
4. wait_for 超时 → 降级抽取式
5. 未注入 summarizer / llm_summarize=False → 不调 LLM，直接抽取式

运行: python -m pytest backend/tests/test_llm_summarize.py -v
"""
import asyncio

from agent.harness.bridle import Bridle, CompressionConfig, _SUMMARY_PREFIX
from agent.harness.contracts import AgentConfig, LLMOutput, RunContext


class FakeLLM:
    """FakeLLM 桩 — 记录每次调用入参，按脚本返回/抛异常"""

    def __init__(self, script=None):
        # script: 每次调用的行为（"raise" 抛异常 / "" 返回空 / str 返回该文本 / callable）
        self.script = script if script is not None else ["生成式摘要：排查 nginx 502，定位到 upstream 超时"]
        self.calls: list[dict] = []

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, reasoning_max_tokens=None, top_p=0.9):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "enable_thinking": enable_thinking,
        })
        i = len(self.calls) - 1
        behavior = self.script[i] if i < len(self.script) else self.script[-1]
        if behavior == "raise":
            raise RuntimeError("mock LLM failure")
        if callable(behavior):
            return behavior()
        return LLMOutput(content=behavior)


class SlowLLM(FakeLLM):
    """模拟摘要 LLM 卡死，触发 wait_for 超时"""

    async def call(self, *args, **kwargs):
        await asyncio.sleep(30)
        raise AssertionError("wait_for 超时后不应执行到这里")


def _bridle(llm=None, **cfg) -> Bridle:
    b = Bridle(max_context_tokens=16384, config=CompressionConfig(**cfg))
    if llm is not None:
        b.set_summarizer(llm)
    return b


def _make_ctx(task: str = "排查线上 502") -> RunContext:
    ctx = RunContext.new(task=task, config=AgentConfig())
    ctx.llm_model = "test-model"
    for i in range(20):
        ctx.messages.append({"role": "user", "content": f"早期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"早期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题"})
    ctx.messages.append({"role": "assistant", "content": "近期回答"})
    return ctx


def test_generated_summary_stored_in_metadata():
    """摘要 LLM 成功 → metadata 存生成摘要正文（不带 _SUMMARY_PREFIX）"""
    llm = FakeLLM(["生成式摘要：定位到 upstream 超时，已修复并验证"])
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.metadata["context_summary"] == "生成式摘要：定位到 upstream 超时，已修复并验证", (
        f"metadata 应存生成摘要正文，实得: {ctx.metadata['context_summary'][:80]!r}")
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["model"] == "test-model"
    assert call["temperature"] == 0.2
    assert call["enable_thinking"] is False
    assert call["tools"] == []
    # 摘要消息不得滞留 ctx.messages（M12）
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_PREFIX)
        for m in ctx.messages
    )


def test_prompt_contains_material_lines():
    """摘要 prompt 必须包含与抽取式同口径的材料行（[role] content）"""
    llm = FakeLLM()
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "[user]" in prompt and "[assistant]" in prompt
    assert "[对话历史]" in prompt


def test_llm_raises_falls_back_to_extractive():
    """摘要 LLM 抛异常 → 静默降级抽取式（metadata 为抽取式拼接，不向上抛）"""
    llm = FakeLLM(["raise"])
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))  # 不应抛异常
    summary = ctx.metadata.get("context_summary", "")
    # 抽取式特征：材料行拼接（metadata 存剥掉 _SUMMARY_PREFIX 后的正文，M12）
    assert summary.startswith("[user]") and "[assistant]" in summary, (
        f"应降级抽取式材料行，实得: {summary[:80]!r}")


def test_llm_empty_output_falls_back_to_extractive():
    """摘要 LLM 返回空串 → 降级抽取式"""
    llm = FakeLLM([""])
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    summary = ctx.metadata.get("context_summary", "")
    assert summary.startswith("[user]") and "[assistant]" in summary


def test_timeout_falls_back_to_extractive():
    """wait_for 超时 → 降级抽取式"""
    llm = SlowLLM()
    b = _bridle(llm, llm_summarize_timeout=0.05)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    summary = ctx.metadata.get("context_summary", "")
    assert summary.startswith("[user]") and "[assistant]" in summary


def test_no_summarizer_skips_llm():
    """未注入 summarizer → 不调 LLM，直接抽取式"""
    b = _bridle()  # 不 set_summarizer
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    summary = ctx.metadata.get("context_summary", "")
    assert summary.startswith("[user]") and "[assistant]" in summary


def test_disabled_flag_skips_llm():
    """llm_summarize=False → 不调 LLM，直接抽取式"""
    llm = FakeLLM()
    b = _bridle(llm, llm_summarize=False)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert llm.calls == []
    summary = ctx.metadata.get("context_summary", "")
    assert summary.startswith("[user]") and "[assistant]" in summary


def test_rolling_summary_second_call_gets_full_prior():
    """滚动再摘要：第二次压缩的 prompt 必须含完整的上一代生成摘要（cap 内）"""
    first = "第一代生成摘要：" + "甲" * 300  # 远超 200 字符，验证完整回喂而非 200 字符切片
    llm = FakeLLM([first, "第二代生成摘要：合并了第一代内容"])
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.metadata["context_summary"] == first

    for i in range(20, 40):
        ctx.messages.append({"role": "user", "content": f"二期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"二期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题2"})
    ctx.messages.append({"role": "assistant", "content": "近期回答2"})
    asyncio.run(b._compress_run_into(ctx, force=True))

    assert ctx.metadata["context_summary"] == "第二代生成摘要：合并了第一代内容"
    second_prompt = llm.calls[1]["messages"][0]["content"]
    assert first in second_prompt, (
        "第二次摘要调用的材料必须包含完整的上一代摘要（滚动回喂），而非其切片")
