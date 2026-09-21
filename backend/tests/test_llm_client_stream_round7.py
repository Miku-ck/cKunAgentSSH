"""
StreamLLMClient 流式收尾与用量统计回归（第 7 轮审查）

覆盖本轮修掉的问题：
- call() 直接 async for 消费 chat_stream，Agent 运行被用户中止时协程在循环处被取消，
  生成器只能等 GC 收尾 —— 期间 llm_adapter 的 _client_in_flight 不归零，LRU 淘汰据此
  拒绝 aclose 客户端，httpx 连接池泄漏到进程结束（与 routes/terminal.py 同一修法）
- provider 不返回 usage 时的兜底估算只算 content，thinking 模型的 reasoning 全被漏计，
  成本与 token 统计系统性偏低
"""
import asyncio

import pytest

import llm_adapter
from agent.cost_tracker import estimate_tokens
from agent.harness import llm_client as lc_mod

# 生成器 finally 落在这里：aclose()/GC 收尾都会置位，断言"取消后是否已关闭"
_stream_closed = {"v": False}


def _chunk(content="", reasoning=""):
    return type("C", (), {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })()


async def _never_ending_stream(**_kwargs):
    _stream_closed["v"] = False
    try:
        while True:
            yield _chunk(content="x")
            await asyncio.sleep(0.01)
    finally:
        _stream_closed["v"] = True


async def _start_then_cancel():
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    task = asyncio.create_task(client.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=[], model="m", temperature=0.7, max_tokens=100, enable_thinking=False,
    ))
    await asyncio.sleep(0.05)  # 确认流真的跑起来了（已消费过 chunk）再取消
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancel_during_stream_closes_llm_stream(monkeypatch):
    """中止运行时必须同步 aclose LLM 流，否则 in-flight 计数挡住客户端回收"""
    # call() 内是 `from llm_adapter import chat_stream`，每次调用时才解析模块属性
    monkeypatch.setattr(llm_adapter, "chat_stream", _never_ending_stream)
    asyncio.run(_start_then_cancel())
    assert _stream_closed["v"] is True, "取消路径未关闭 LLM 流式生成器"


def test_reasoning_counts_toward_estimated_completion_tokens(monkeypatch):
    """无 usage 时，reasoning 也要计入补全 token 估算"""
    long_reasoning = "z" * 400

    async def stream(**_kwargs):
        yield _chunk(reasoning=long_reasoning)
        yield _chunk(content="ok")

    monkeypatch.setattr(llm_adapter, "chat_stream", stream)
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    out = asyncio.run(client.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=[], model="m", temperature=0.7, max_tokens=100, enable_thinking=True,
    ))

    # content 只有 "ok"（每条消息 4 的格式开销 + ~0 token）；漏算 reasoning 时远小于此
    expected_floor = estimate_tokens(long_reasoning)
    assert out.completion_tokens >= expected_floor, (
        f"reasoning 未计入估算：{out.completion_tokens} < {expected_floor}"
    )
