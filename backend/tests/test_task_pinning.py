"""P2 原始任务钉扎测试 — 长任务压缩后模型始终能看到原始目标，杜绝目标漂移。

覆盖：
1. 压缩后 ctx.messages[0] 为原任务全文（原样保留，未被摘要化）
2. 有 prior 摘要时不误摘/不误伤（哨兵合成消息不得进入 ctx.messages，任务仍在首位）
3. messages[0] 与 ctx.task 不匹配（ctx 重建等场景）→ 不钉，按普通消息压缩
4. 任务消息摘出后不参与摘要材料（生成式 prompt 与抽取式摘要均不含任务全文）
5. 钉扎与纠偏 system 消息共存：任务在前、system 在末尾

运行: python -m pytest backend/tests/test_task_pinning.py -v
"""
import asyncio

from agent.harness.bridle import Bridle, _SUMMARY_PREFIX
from agent.harness.contracts import AgentConfig, LLMOutput, RunContext


TASK = "修复登录页面 500 错误：定位 upstream 日志中的 traceback 并给出修复补丁"


class FakeLLM:
    def __init__(self, content="生成式摘要：已完成日志排查"):
        self.content = content
        self.prompts: list[str] = []

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, reasoning_max_tokens=None, top_p=0.9):
        self.prompts.append(messages[0]["content"])
        return LLMOutput(content=self.content)


def _bridle(llm=None) -> Bridle:
    b = Bridle(max_context_tokens=16384)
    if llm is not None:
        b.set_summarizer(llm)
    return b


def _make_ctx() -> RunContext:
    ctx = RunContext.new(task=TASK, config=AgentConfig())
    ctx.llm_model = "test-model"
    # messages[0] = 原始任务（harness.py 首条消息写入约定）
    ctx.messages.append({"role": "user", "content": TASK})
    for i in range(20):
        ctx.messages.append({"role": "user", "content": f"早期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"早期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题"})
    ctx.messages.append({"role": "assistant", "content": "近期回答"})
    return ctx


def test_task_message_pinned_after_compress():
    """压缩后 ctx.messages[0] 为原任务全文（同一 dict 对象原样保留）"""
    b = _bridle()
    ctx = _make_ctx()
    task_msg = ctx.messages[0]
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.messages[0] is task_msg, "任务消息应原样钉扎在首位"
    assert ctx.messages[0]["content"] == TASK
    assert ctx.metadata.get("context_summary"), "压缩仍应产出摘要"


def test_pinning_safe_with_prior_summary():
    """有 prior 摘要（哨兵合成消息参与压缩）时：任务仍钉在首位，哨兵不入 ctx.messages"""
    b = _bridle()
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    first_summary = ctx.metadata["context_summary"]

    for i in range(20, 40):
        ctx.messages.append({"role": "user", "content": f"二期问题 {i} " + "x" * 50})
        ctx.messages.append({"role": "assistant", "content": f"二期回答 {i} " + "y" * 50})
    ctx.messages.append({"role": "user", "content": "近期问题2"})
    ctx.messages.append({"role": "assistant", "content": "近期回答2"})
    asyncio.run(b._compress_run_into(ctx, force=True))

    assert ctx.messages[0]["content"] == TASK, "第二次压缩不得误摘哨兵/丢任务"
    assert first_summary in ctx.metadata["context_summary"], "滚动再摘要仍需包含旧摘要"
    assert not any(
        isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_PREFIX)
        for m in ctx.messages
    )


def test_no_pinning_when_first_message_mismatch():
    """conv[0] 与 ctx.task 不全等（防御 ctx 重建场景）→ 不钉，按普通消息压缩"""
    b = _bridle()
    ctx = _make_ctx()
    ctx.messages[0] = {"role": "user", "content": "另一条开场消息"}
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.messages[0]["content"] != "另一条开场消息" or len(ctx.messages) == 1, (
        "不匹配时不应发生钉扎")


def test_task_not_in_summary_material():
    """任务消息摘出后不参与摘要：生成式 prompt 与抽取式摘要均不含任务全文"""
    llm = FakeLLM()
    b = _bridle(llm)
    ctx = _make_ctx()
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert TASK not in llm.prompts[0], "生成式摘要材料不应包含已钉扎的任务全文"

    # 抽取式降级路径同样不含任务全文
    b2 = _bridle()
    ctx2 = _make_ctx()
    asyncio.run(b2._compress_run_into(ctx2, force=True))
    assert TASK not in ctx2.metadata["context_summary"], (
        "抽取式摘要材料也不应包含已钉扎的任务全文")


def test_pinning_with_system_messages():
    """钉扎与纠偏 system 消息共存：任务在最前，system 保留在末尾"""
    b = _bridle()
    ctx = _make_ctx()
    ctx.messages.append({"role": "system", "content": "[纠偏] 请勿重复调用同一工具"})
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.messages[0]["content"] == TASK
    assert ctx.messages[-1] == {"role": "system", "content": "[纠偏] 请勿重复调用同一工具"}


def test_task_only_no_compression():
    """任务消息是唯一消息时：直接返回，不做任何压缩"""
    b = _bridle()
    ctx = RunContext.new(task=TASK, config=AgentConfig())
    ctx.messages.append({"role": "user", "content": TASK})
    asyncio.run(b._compress_run_into(ctx, force=True))
    assert ctx.messages == [{"role": "user", "content": TASK}]
    assert not ctx.metadata.get("context_summary")
