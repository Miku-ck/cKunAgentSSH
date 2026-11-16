"""备用模型回退 harness 层 — TRANSIENT 耗尽切换的回归测试。

守护点：
- 主模型 TRANSIENT 重试耗尽 → 切换回调被调、新客户端接管、ctx.llm_model 更新；
- 切换后 warning 事件带 code=model_fallback 与 from/to（前端零协议改动依据）；
- 回调迁移：新客户端的 _on_tokens / _on_chunk 必须就位（成本与流式的生命线）；
- 备用也失败 → 抛失败链路的错误；FATAL / INVALID_ARGS 不触发切换；
- 每 run 只切一次（第二次 TRANSIENT 耗尽直接抛，不反复横跳）；
- 回调本身失败（密钥不可用等）→ 抛原始错误，绝不伪装成功。

替身说明：harness 的切换闭包在调用期构建 StreamLLMClient —— 测试 monkeypatch
该符号，避免打真实网络（真端点不可达会让重试链路跑满 2 分钟）。

运行: python -m pytest backend/tests/test_harness_model_fallback.py -v
"""
import asyncio

import pytest

from agent.harness import llm_client as llm_client_mod
from agent.harness.contracts import (
    AgentConfig,
    LLMOutput,
    RunContext,
)
from agent.harness.harness import Harness
from agent.harness.stirrups import Stirrups


class _FlakyClient:
    """按调用次数抛 TRANSIENT 或成功的假主客户端（记录 model 实参）"""

    def __init__(self, transient_times: int, model: str = "primary"):
        self._transient_times = transient_times
        self._calls = 0
        self._on_tokens = None
        self._on_chunk = None
        self.models_seen: list[str] = []

    def set_token_callback(self, cb):
        self._on_tokens = cb

    def set_chunk_callback(self, cb):
        self._on_chunk = cb

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, reasoning_max_tokens=None, top_p=0.9):
        self._calls += 1
        self.models_seen.append(model)
        if self._calls <= self._transient_times:
            raise RuntimeError("connection refused (transient)")
        return LLMOutput(content=f"答案来自 {model}")


class _FakeStreamClient:
    """替身：拦住 harness 切换闭包里的真实 StreamLLMClient 构造（防打真网络）"""

    behavior = "ok"  # "ok" | "transient"
    instances: list = []

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self._on_tokens = None
        self._on_chunk = None
        _FakeStreamClient.instances.append(self)

    def set_token_callback(self, cb):
        self._on_tokens = cb

    def set_chunk_callback(self, cb):
        self._on_chunk = cb

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, reasoning_max_tokens=None, top_p=0.9):
        if _FakeStreamClient.behavior == "transient":
            raise RuntimeError("connection refused (fallback down)")
        return LLMOutput(content=f"答案来自 {model}")


@pytest.fixture
def fake_stream(monkeypatch):
    _FakeStreamClient.behavior = "ok"
    _FakeStreamClient.instances = []
    monkeypatch.setattr(llm_client_mod, "StreamLLMClient", _FakeStreamClient)
    return _FakeStreamClient


FB = {"model_id": "fallback-m", "base_url": "http://fb.invalid/v1", "api_key": "sk-fb"}


class _StubCostTracker:
    def __init__(self):
        self.model_id = "primary"
        self.calls: list[tuple] = []

    def record_llm_call(self, prompt_tokens, completion_tokens):
        self.calls.append((prompt_tokens, completion_tokens))


def _harness(fb_cfg, primary_transient=99):
    primary = _FlakyClient(primary_transient, model="primary")

    h = Harness.__new__(Harness)
    # 绕开 __init__ 的组件装配，只装测试所需最小面（telemetry/bridle/stirrups）
    from agent.harness.bridle import Bridle
    from agent.harness.telemetry import TelemetryBus

    h.telemetry = TelemetryBus()
    h.bridle = Bridle(max_context_tokens=100_000)
    h._stirrups = Stirrups(llm_client=primary)
    h._llm_client = primary
    h.cost_tracker = _StubCostTracker()
    events: list = []

    async def emit_event(ev):
        events.append(ev)

    config = AgentConfig(llm_max_retries=1, fallback_model_cfg=fb_cfg)
    ctx = RunContext.new(task="测试任务", config=config)
    ctx.llm_model = "primary"
    h.stirrups.set_fallback_switch(h._make_fallback_switch(config, ctx, emit_event))
    return h, ctx, events


def test_transient_exhausted_switches_to_fallback(fake_stream):
    h, ctx, events = _harness(FB, primary_transient=99)
    out = asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
    assert out.content == "答案来自 fallback-m"
    assert ctx.llm_model == "fallback-m"
    assert ctx.metadata.get("model_fallback_attempted") is True
    warn = [e for e in events if e.kind == "warning" and e.payload.get("code") == "model_fallback"]
    assert len(warn) == 1
    assert warn[0].payload["from"] == "primary"
    assert warn[0].payload["to"] == "fallback-m"


def test_fallback_client_gets_callbacks_transferred(fake_stream):
    """回调迁移：新客户端的 _on_chunk/_on_tokens 必须就位，否则成本与流式断裂。"""
    h, ctx, _events = _harness(FB, primary_transient=99)
    primary = h._llm_client
    chunks: list[str] = []

    async def on_chunk(c, r):
        chunks.append(c)

    primary.set_chunk_callback(on_chunk)
    primary.set_token_callback(lambda p, c: None)

    asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
    new_client = _FakeStreamClient.instances[-1]
    assert new_client.base_url == FB["base_url"]
    assert new_client._on_chunk is on_chunk
    assert new_client._on_tokens is not None
    assert h.bridle._summarizer is new_client
    assert h.stirrups.llm_client is new_client
    # 成本记账切到备用模型口径
    assert h.cost_tracker.model_id == "fallback-m"


def test_fatal_error_does_not_switch(fake_stream):
    h, ctx, events = _harness(FB)

    class _FatalClient(_FlakyClient):
        async def call(self, *a, **kw):
            raise RuntimeError("invalid api key (fatal)")

    h.stirrups.llm_client = _FatalClient(0)
    with pytest.raises(RuntimeError, match="invalid api key"):
        asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
    assert not [e for e in events if e.kind == "warning" and e.payload.get("code") == "model_fallback"]


def test_context_overflow_does_not_switch(fake_stream):
    """上下文超限（INVALID_ARGS）切端点解决不了，不触发回退。"""
    h, ctx, events = _harness(FB)

    class _OverflowClient(_FlakyClient):
        async def call(self, *a, **kw):
            raise RuntimeError("maximum context length exceeded")

    h.stirrups.llm_client = _OverflowClient(0)
    with pytest.raises(RuntimeError, match="maximum context"):
        asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
    assert not [e for e in events if e.kind == "warning" and e.payload.get("code") == "model_fallback"]


def test_switch_happens_only_once_per_run(fake_stream):
    """备用也持续失败 → 第二次 TRANSIENT 耗尽直接抛，不反复横跳。"""
    _FakeStreamClient.behavior = "transient"
    h, ctx, _events = _harness(FB, primary_transient=99)

    async def main():
        with pytest.raises(RuntimeError, match="connection refused"):
            await h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], [])
        # 第一次已切到备用（也持续失败）；第二次不再尝试切换
        with pytest.raises(RuntimeError, match="connection refused"):
            await h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], [])

    asyncio.run(main())
    assert ctx.metadata.get("model_fallback_attempted") is True
    assert len(_FakeStreamClient.instances) == 1  # 只构建过一个备用客户端


def test_switch_failure_raises_original_error(fake_stream):
    """回调失败（密钥不可用）→ 抛原始错误，绝不把「切备用失败」当失败原因。"""
    bad_fb = {"model_id": "fallback-m", "base_url": "http://fb.invalid/v1", "api_key": ""}
    h, ctx, events = _harness(bad_fb, primary_transient=99)
    with pytest.raises(RuntimeError, match="connection refused \\(transient\\)"):
        asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
    assert not [e for e in events if e.kind == "warning"]
    assert len(_FakeStreamClient.instances) == 0  # 密钥不可用：根本没构造备用客户端


def test_no_fallback_config_behaves_as_before():
    """未配置备用（None / 缺 base_url）→ 行为与引入前一致（重试耗尽直接抛）。"""
    for fb in (None, {"model_id": "m", "base_url": ""}):
        h, ctx, _events = _harness(fb)
        h.stirrups.llm_client = _FlakyClient(99)
        with pytest.raises(RuntimeError, match="connection refused"):
            asyncio.run(h.stirrups.call_llm(ctx, [{"role": "user", "content": "hi"}], []))
