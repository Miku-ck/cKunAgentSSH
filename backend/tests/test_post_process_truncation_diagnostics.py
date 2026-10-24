"""后处理截断诊断、标题预算与记忆解析重试回归。

覆盖：
- chat_stream 透传 finish_reason（stop/length 才能被上层诊断，而不是只看到 JSON 失败）
- _call_llm_with_timeout 在空内容 / length 结束时输出诊断
- 记忆解析失败后只做一次 2048 预算重试；合法空结果不触发重试

运行: python -m pytest backend/tests/test_post_process_truncation_diagnostics.py -v
"""
import asyncio
import json
from types import SimpleNamespace

import llm_adapter
import post_processor as pp
from llm_adapter import StreamChunk
from post_processor import PostProcessConfig, TurnContext


# ── 1. llm_adapter：finish_reason 透传 ────────────────────────────────────────


def test_chat_stream_impl_passes_finish_reason_and_usage(monkeypatch):
    """最后一个 choices chunk 的 finish_reason 不能被丢弃；usage chunk 仍单独产出。"""
    content_delta = SimpleNamespace(content="x", reasoning="", tool_calls=None)
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=content_delta, finish_reason=None)]),
        SimpleNamespace(choices=[SimpleNamespace(delta=content_delta, finish_reason="length")]),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
        ),
    ]

    async def fake_create(**_kwargs):
        async def gen():
            for chunk in chunks:
                yield chunk
        return gen()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))

    async def fake_get_client(base_url, api_key):
        return client

    monkeypatch.setattr(llm_adapter, "_get_client", fake_get_client)

    async def drive():
        got = []
        async for chunk in llm_adapter._chat_stream_impl(
            base_url="http://x", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
        ):
            got.append(chunk)
        return got

    got = asyncio.run(drive())
    assert got[-2].finish_reason == "length", "finish_reason 被丢弃，上层无法诊断输出截断"
    assert got[-1].completion_tokens == 7


# ── 2. post_processor：截断/空内容诊断 ────────────────────────────────────────


def test_call_llm_logs_length_and_empty_diagnostics(monkeypatch, caplog):
    """reasoning 耗尽预算且 content 为空时，必须能看到 finish_reason 与用量。"""
    async def fake_stream(**_kwargs):
        yield StreamChunk(
            content="", reasoning="thinking", finish_reason="length",
            completion_tokens=50,
        )

    monkeypatch.setattr(pp, "chat_stream", fake_stream)
    with caplog.at_level("WARNING", logger=pp.logger.name):
        result = asyncio.run(pp._call_llm_with_timeout(
            messages=[{"role": "user", "content": "hi"}],
            model_cfg={"base_url": "u", "api_key": "k"},
            model_id="m", temperature=0.3, max_tokens=128,
            chunk_timeout=1, total_timeout=2, task_name="title",
        ))

    assert result is None
    text = caplog.text
    assert "finish_reason=length" in text
    assert "completion_tokens=50" in text
    assert "reasoning_len=8" in text


def test_call_llm_warns_when_non_empty_output_hits_budget(monkeypatch, caplog):
    """有 content 但 finish_reason=length 也必须单独告警，否则只见下游解析失败。"""
    async def fake_stream(**_kwargs):
        yield StreamChunk(content="partial", finish_reason="length", completion_tokens=20)

    monkeypatch.setattr(pp, "chat_stream", fake_stream)
    with caplog.at_level("WARNING", logger=pp.logger.name):
        result = asyncio.run(pp._call_llm_with_timeout(
            messages=[{"role": "user", "content": "hi"}],
            model_cfg={"base_url": "u", "api_key": "k"},
            model_id="m", temperature=0.3, max_tokens=128,
            chunk_timeout=1, total_timeout=2, task_name="memory",
        ))

    assert result == "partial"
    assert "max_tokens=128" in caplog.text
    assert "可能截断" in caplog.text


# ── 3. 记忆解析：完整对象抢救 / 失败区分 / 合法空结果 ────────────────────────


def _memory(title="记忆A", content="内容A", stop_mid=False):
    obj = f'{{"title": "{title}", "content": "{content}", "kind": "semantic", "scope": "global"}}'
    if stop_mid:
        obj = obj[:-20]
    return obj


def test_parse_memories_payload_rescues_complete_object_from_truncated_output():
    truncated = '{"memories": [' + _memory() + ", " + _memory("被截断", "内容", stop_mid=True)
    parsed = pp._parse_memories_payload(truncated)
    assert parsed is not None and len(parsed) == 1
    assert parsed[0]["title"] == "记忆A"


def test_parse_memories_payload_distinguishes_failure_and_empty():
    # 无任何自闭合对象 = 解析失败（可触发上层重试）
    assert pp._parse_memories_payload('{"memories": [{"title": "截断在') is None
    # 合法 JSON 空数组 = 不是失败，不应重试
    assert pp._parse_memories_payload('{"memories": []}') == []


# ── 4. 记忆解析失败后的一次受控重试 ─────────────────────────────────────────


def test_extract_memories_retries_once_with_larger_budget(monkeypatch):
    calls: list[dict] = []
    enqueued: list = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        if kwargs.get("task_name") == "memory":
            # 模拟整段截断且没有可抢救对象
            return '{"memories": [{"title": "截断在字符串中'
        assert kwargs.get("task_name") == "memory_retry"
        return json.dumps({"memories": [
            {"title": "重试记忆", "content": "重试内容", "kind": "semantic"},
            {"title": "第二条", "content": "不应入库", "kind": "semantic"},
        ]})

    monkeypatch.setattr(pp.db, "get_memory_settings", lambda: {"enabled": True})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid, "base_url": "u", "api_key": "k"})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    monkeypatch.setattr("agent.memory.mstore.enqueue_job",
                        lambda job, payload, retries: enqueued.append((job, payload)))

    ctx = TurnContext(
        conversation_id="conv-1",
        user_message="q",
        assistant_content="a",
        assistant_reasoning="",
        model_id="model-1",
        turn_index=1,
    )
    asyncio.run(pp._extract_memories(ctx, PostProcessConfig(max_memories_per_turn=1)))

    assert [c.get("max_tokens") for c in calls] == [1024, 2048], "未按首查/一次重试的预算执行"
    assert [c.get("task_name") for c in calls] == ["memory", "memory_retry"]
    assert len(enqueued) == 1
    items = enqueued[0][1]["items"]
    assert len(items) == 1 and items[0]["title"] == "重试记忆"


def test_extract_memories_does_not_retry_valid_empty_result(monkeypatch):
    calls: list[dict] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return '{"memories": []}'

    monkeypatch.setattr(pp.db, "get_memory_settings", lambda: {"enabled": True})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid, "base_url": "u", "api_key": "k"})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)

    ctx = TurnContext(
        conversation_id="conv-1",
        user_message="q",
        assistant_content="a",
        assistant_reasoning="",
        model_id="model-1",
        turn_index=1,
    )
    asyncio.run(pp._extract_memories(ctx, PostProcessConfig()))
    assert len(calls) == 1, "合法空结果被误判成解析失败并发起重试"
