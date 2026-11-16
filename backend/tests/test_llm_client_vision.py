"""视觉多模态（图片粘贴）LLM 请求边界回归

覆盖：
- _attach_images_to_messages：并入首条非空 user 消息、其余消息原样、
  无可挂载消息时追加独立条（绝不静默丢图）
- StreamLLMClient.call(images=...)：发给 chat_stream 的 messages 已是
  content 数组格式；无 images 时请求体与引入前逐字段等价
- 上游拒绝（错误文案带图片/视觉标记）时给出人话提示
"""
import asyncio

import pytest

import llm_adapter
from agent.harness import llm_client as lc_mod


def _chunk(content="", reasoning=""):
    return type("C", (), {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })()


IMG = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]


def test_attach_images_merges_into_first_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "好"},
    ]
    out = lc_mod._attach_images_to_messages(messages, IMG)
    assert out[0] == messages[0]                    # system 原样
    assert out[2] == messages[2]                    # 后续消息原样
    assert isinstance(out[1]["content"], list)
    assert out[1]["content"][0] == {"type": "text", "text": "任务"}
    assert out[1]["content"][1] == IMG[0]


def test_attach_images_skips_empty_user_texts():
    """空 user 消息不可挂载（content 数组里塞空文本毫无意义），继续找下一条"""
    messages = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "真正的任务"},
    ]
    out = lc_mod._attach_images_to_messages(messages, IMG)
    assert out[0] == messages[0]                    # 空消息原样保留
    assert isinstance(out[1]["content"], list)


def test_attach_images_appends_when_no_user_text():
    messages = [{"role": "system", "content": "sys"}]
    out = lc_mod._attach_images_to_messages(messages, IMG)
    assert out[-1] == {"role": "user", "content": IMG}


def test_no_images_keeps_payload_identical(monkeypatch):
    """无 images 时连 transform 都不做：messages 引用原样透传"""
    captured = {}

    async def stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield _chunk(content="ok")

    monkeypatch.setattr(llm_adapter, "chat_stream", stream)
    msgs = [{"role": "user", "content": "hi"}]
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    asyncio.run(client.call(
        messages=msgs, tools=[], model="m", temperature=0.7,
        max_tokens=100, enable_thinking=False,
    ))
    assert captured["messages"] is msgs


def test_images_reach_payload_as_content_array(monkeypatch):
    captured = {}

    async def stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield _chunk(content="ok")

    monkeypatch.setattr(llm_adapter, "chat_stream", stream)
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    out = asyncio.run(client.call(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "看这张截图"},
        ],
        tools=[], model="m", temperature=0.7, max_tokens=100,
        enable_thinking=False, images=IMG,
    ))
    assert out.content == "ok"
    sent = captured["messages"]
    assert isinstance(sent[1]["content"], list)
    assert sent[1]["content"][0]["text"] == "看这张截图"
    assert sent[1]["content"][1] == IMG[0]
    # token 估算兜底走 list 分支不炸（provider 无 usage 时）
    assert out.prompt_tokens > 0


def test_vision_reject_error_gets_human_hint(monkeypatch):
    """文本模型拒收图片（错误带 image/视觉标记）：提示换多模态模型，而不是裸 400"""

    async def stream(**kwargs):
        raise RuntimeError("400: 'image_url' input is not supported by this model")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_adapter, "chat_stream", stream)
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    with pytest.raises(RuntimeError, match="视觉多模态"):
        asyncio.run(client.call(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], model="m", temperature=0.7, max_tokens=100,
            enable_thinking=False, images=IMG,
        ))


def test_plain_error_with_images_keeps_original_message(monkeypatch):
    """与图片无关的错误（如连接拒绝）不误伤：原文透传"""

    async def stream(**kwargs):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_adapter, "chat_stream", stream)
    client = lc_mod.StreamLLMClient(base_url="http://x", api_key="k")
    with pytest.raises(RuntimeError, match="connection refused"):
        asyncio.run(client.call(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], model="m", temperature=0.7, max_tokens=100,
            enable_thinking=False, images=IMG,
        ))
