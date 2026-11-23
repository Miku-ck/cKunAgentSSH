"""路线 B：按模型动态调整输出预算与思考强度（后台任务链路）回归。

背景：reconcile 等后台任务原先写死 max_tokens=1024 且不关思考，思考型模型把
预算烧在 reasoning 上（finish_reason=length + 空内容），job 循环失败。

覆盖：
- _call_llm_with_timeout：max_tokens=None 随模型 max_output_tokens 动态（缺省 1024）
- thinking 三态策略透传："off" / "limited" / "model_default"
- 思考耗尽输出预算的专项诊断日志
- reconcile._call_llm：声明 thinking="off"，首查失败/解析失败按 2x 预算重试一次

运行: python -m pytest backend/tests/test_reconcile_dynamic_thinking.py -v
"""
import asyncio
import json

import post_processor as pp
from llm_adapter import StreamChunk
from agent.memory import reconcile


def _cfg(**extra):
    return {"base_url": "u", "api_key": "k", **extra}


def _capture_stream(monkeypatch):
    captured = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield StreamChunk(content="ok", finish_reason="stop", completion_tokens=1)

    monkeypatch.setattr(pp, "chat_stream", fake_stream)
    return captured


# ── 1. 动态预算：max_tokens=None 随模型配置走 ────────────────────────────────


def test_dynamic_budget_reads_model_max_output_tokens(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(max_output_tokens=4096),
        model_id="m", temperature=0.1, max_tokens=None,
        chunk_timeout=1, total_timeout=2, task_name="reconcile",
    ))
    assert captured["max_tokens"] == 4096, "max_tokens=None 未按模型 max_output_tokens 动态取值"


def test_dynamic_budget_falls_back_to_1024_without_config(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(),
        model_id="m", temperature=0.1, max_tokens=None,
        chunk_timeout=1, total_timeout=2, task_name="reconcile",
    ))
    assert captured["max_tokens"] == 1024, "模型未配 max_output_tokens 时应兜底 1024"


def test_explicit_max_tokens_still_wins(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(max_output_tokens=4096),
        model_id="m", temperature=0.1, max_tokens=256,
        chunk_timeout=1, total_timeout=2, task_name="title",
    ))
    assert captured["max_tokens"] == 256, "显式指定的 max_tokens 被动态解析覆盖"


# ── 2. thinking 三态策略透传 ─────────────────────────────────────────────────


def test_thinking_off_sends_disable_thinking(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(supports_thinking=1),
        model_id="m", temperature=0.1, max_tokens=1024,
        chunk_timeout=1, total_timeout=2, task_name="reconcile",
        thinking="off",
    ))
    assert captured["disable_thinking"] is True, \
        "thinking='off' 未显式下发关思考（enable_thinking=False 默认值语义关不掉思考）"
    assert captured["enable_thinking"] is False
    assert captured["reasoning_max_tokens"] is None


def test_thinking_limited_reads_model_reasoning_tokens(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(reasoning_tokens=512),
        model_id="m", temperature=0.1, max_tokens=2048,
        chunk_timeout=1, total_timeout=2, task_name="reconcile",
        thinking="limited",
    ))
    assert captured["enable_thinking"] is True
    assert captured["disable_thinking"] is False
    assert captured["reasoning_max_tokens"] == 512, \
        "thinking='limited' 未按模型 reasoning_tokens 取思考预算"


def test_thinking_limited_explicit_budget_wins(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(reasoning_tokens=512),
        model_id="m", temperature=0.1, max_tokens=2048,
        chunk_timeout=1, total_timeout=2, task_name="reconcile",
        thinking="limited", thinking_budget=128,
    ))
    assert captured["reasoning_max_tokens"] == 128, "显式 thinking_budget 应优先于模型配置"


def test_thinking_model_default_keeps_old_behavior(monkeypatch):
    captured = _capture_stream(monkeypatch)
    asyncio.run(pp._call_llm_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        model_cfg=_cfg(reasoning_tokens=512),
        model_id="m", temperature=0.1, max_tokens=1024,
        chunk_timeout=1, total_timeout=2, task_name="title",
    ))
    assert captured["enable_thinking"] is False
    assert captured["disable_thinking"] is False, \
        "默认策略不该下发任何思考参数（旧行为：非 vLLM 端点兼容）"
    assert captured["reasoning_max_tokens"] is None


# ── 3. 思考耗尽输出预算的专项诊断 ────────────────────────────────────────────


def test_thinking_exhausted_budget_gets_dedicated_warning(monkeypatch, caplog):
    """reasoning 有产出 + content 为空 + length 结束 = 思考吃光预算，须专项提示。"""
    async def fake_stream(**_kwargs):
        yield StreamChunk(
            content="", reasoning="thinking", finish_reason="length",
            completion_tokens=50,
        )

    monkeypatch.setattr(pp, "chat_stream", fake_stream)
    with caplog.at_level("WARNING", logger=pp.logger.name):
        result = asyncio.run(pp._call_llm_with_timeout(
            messages=[{"role": "user", "content": "hi"}],
            model_cfg=_cfg(),
            model_id="m", temperature=0.3, max_tokens=128,
            chunk_timeout=1, total_timeout=2, task_name="reconcile",
        ))

    assert result is None
    assert "思考耗尽输出预算" in caplog.text
    assert "reasoning_len=8" in caplog.text
    assert "thinking='off'" in caplog.text


# ── 4. reconcile._call_llm：关思考 + 首查/重试双预算 ─────────────────────────


def test_reconcile_llm_thinking_off_with_retry_budget(monkeypatch):
    """首查空内容（旧故障形态）→ 2x 预算重试一次成功。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        if kwargs.get("task_name") == "reconcile":
            return None  # 模拟思考吃光预算 → 空内容
        assert kwargs.get("task_name") == "reconcile_retry"
        return json.dumps({"decisions": [{"index": 0, "op": "ADD"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    decisions = asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=2048)))

    assert decisions == [{"index": 0, "op": "ADD"}]
    assert [c["max_tokens"] for c in calls] == [2048, 4096], \
        "未按「模型 max_output_tokens → 2x 重试」双预算执行"
    assert all(c["thinking"] == "off" for c in calls), "对账未声明关思考"


def test_reconcile_llm_retries_on_parse_failure(monkeypatch):
    """解析失败（截断 JSON）→ 2x 预算重试一次成功。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        if kwargs.get("task_name") == "reconcile":
            return '{"decisions": [{"index": 0, "op": "ADD" '  # 截断
        return json.dumps({"decisions": [{"index": 0, "op": "NOOP"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    decisions = asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=1024)))

    assert decisions == [{"index": 0, "op": "NOOP"}]
    assert [c["max_tokens"] for c in calls] == [1024, 2048]


def test_reconcile_llm_both_attempts_fail_returns_none(monkeypatch):
    """两次都失败 → 返回 None（job 级重试语义保持不变），不做无限重试。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    decisions = asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=1024)))

    assert decisions is None
    assert len(calls) == 2, "受控重试：只允许首查 + 一次升预算重试"


def test_reconcile_llm_first_attempt_success_no_retry(monkeypatch):
    """首查即成功 → 不发起重试（不白烧一次调用）。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return json.dumps({"decisions": [{"index": 0, "op": "ADD"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    decisions = asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=2048)))

    assert decisions == [{"index": 0, "op": "ADD"}]
    assert len(calls) == 1


# ── 5. 输出预算按上下文窗口封顶（防 vLLM 400）────────────────────────────────


def test_reconcile_llm_caps_budget_to_context_window(monkeypatch):
    """max_output_tokens 被误配成接近窗口（262144 = max_model_len）时不得原样下发。

    实测故障：vLLM 400 `max_tokens=524288 cannot be greater than
    max_model_len=262144`，job 每 5 分钟重试一次、永远停在待重试。
    口径与 bot/chat_service、routes/agent 的 safe_max_tokens 一致。
    """
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return json.dumps({"decisions": [{"index": 0, "op": "ADD"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=262144, max_tokens=262144)))

    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 65536, \
        "误配 max_output_tokens=窗口 时未按 safe_max_tokens 降级封顶"


def test_reconcile_llm_retry_budget_also_capped(monkeypatch):
    """重试的 2x 预算同样封顶：400 属参数非法，翻倍重试不可能成功。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        if kwargs.get("task_name") == "reconcile":
            return None  # 首查空内容 → 触发重试
        return json.dumps({"decisions": [{"index": 0, "op": "NOOP"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    decisions = asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=262144, max_tokens=262144)))

    assert decisions == [{"index": 0, "op": "NOOP"}]
    assert [c["max_tokens"] for c in calls] == [65536, 65536], \
        "重试预算未封顶，仍会出现 max_tokens > 窗口的 400"


def test_reconcile_llm_small_window_capped_and_deduped(monkeypatch):
    """窗口 8k + 误配 8k：封顶到窗口 1/2，且两个预算档去重后只调一次。"""
    calls = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return json.dumps({"decisions": [{"index": 0, "op": "ADD"}]})

    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    asyncio.run(reconcile._call_llm(
        "PROMPT", "CANDS", _cfg(max_output_tokens=8192, max_tokens=8192)))

    assert [c["max_tokens"] for c in calls] == [4096], \
        "小窗口下未封顶，且 2x 预算未去重"
