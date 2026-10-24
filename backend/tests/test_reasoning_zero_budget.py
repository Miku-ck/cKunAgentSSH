"""
S10-06 回归：reasoning_tokens=0 的语义必须是「跟随输出预算」，不是「不设上限」。

链路：面板表单（`min="0"`）/ `routes/settings.py` 的 `Field(ge=0)` 说明
「0 表示使用 max_output_tokens」→ `config.max_reasoning_tokens` → `llm_client.call`
→ `llm_adapter._chat_stream_impl`。原实现在最后一层用
`reasoning_max_tokens is not None and > 0` 判下发，0 被当成「未设」⇒ 请求里
根本没有 `thinking_budget` ⇒ vLLM 按**无上限**出思考 —— 与它自己两端的文档相反。
`schedule_engine` 上一轮把 `if rt else None` 改成 `if rt is not None`，只填了
0-塌陷族的上半段，0 在这一层又被吞回去。

测试打法：`_get_client` 换成假客户端，`chat.completions.create` 记下 kwargs 后抛
哨兵异常 —— 只验请求组装，不碰网络。
"""
import asyncio
from types import SimpleNamespace

import llm_adapter


class _Sentinel(Exception):
    """create() 记录完 kwargs 就用它中断，避免实现流式协议"""


def _capture(monkeypatch, **call_kwargs) -> dict:
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        raise _Sentinel()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))

    async def fake_get_client(base_url, api_key):
        return client

    monkeypatch.setattr(llm_adapter, "_get_client", fake_get_client)

    async def drive():
        agen = llm_adapter._chat_stream_impl(
            base_url="http://x", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            **call_kwargs,
        )
        try:
            async for _ in agen:
                pass
        except _Sentinel:
            pass

    asyncio.run(drive())
    return captured


def _budget(captured: dict):
    return captured.get("extra_body", {}).get("chat_template_kwargs", {}).get("thinking_budget")


def test_zero_means_follow_output_budget(monkeypatch):
    """0 = 跟随本次 max_tokens（Field 说明与面板文案的既定语义）"""
    captured = _capture(
        monkeypatch, max_tokens=4096, enable_thinking=True, reasoning_max_tokens=0
    )
    assert _budget(captured) == 4096, (
        "reasoning_max_tokens=0 又塌成「不下发 thinking_budget」⇒ vLLM 无上限出思考"
    )


def test_positive_budget_passes_through(monkeypatch):
    captured = _capture(
        monkeypatch, max_tokens=4096, enable_thinking=True, reasoning_max_tokens=512
    )
    assert _budget(captured) == 512


def test_none_means_unlimited(monkeypatch):
    """None 才是「不限制」—— 与 0 区分开，两种含义不得互塌"""
    captured = _capture(
        monkeypatch, max_tokens=4096, enable_thinking=True, reasoning_max_tokens=None
    )
    assert "thinking_budget" not in captured.get("extra_body", {}).get(
        "chat_template_kwargs", {}
    )
    # enable_thinking 本身仍要下发
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_zero_without_output_budget_stays_unlimited(monkeypatch):
    """无 max_tokens 可跟时维持原样（不下发），不发明第三种含义"""
    captured = _capture(
        monkeypatch, max_tokens=None, enable_thinking=True, reasoning_max_tokens=0
    )
    assert "thinking_budget" not in captured.get("extra_body", {}).get(
        "chat_template_kwargs", {}
    )


def test_thinking_off_emits_no_template_kwargs(monkeypatch):
    captured = _capture(
        monkeypatch, max_tokens=4096, enable_thinking=False, reasoning_max_tokens=0
    )
    assert "extra_body" not in captured


def test_schedule_side_zero_reaches_adapter_intact(monkeypatch):
    """定时任务侧 `min(int(rt), safe_out) if rt is not None` 保住 0，
    到这一层也不该被当未设 —— 两处对 0 的解释必须一致（S10-06 的反义对照组：
    safe_tokens 把 0 读成「不从输出空间扣」，而 >0 守卫读成「无上限」）。"""
    rt = 0
    safe_out = 2048
    safe_reasoning = min(int(rt), safe_out) if rt is not None else None
    assert safe_reasoning == 0
    captured = _capture(
        monkeypatch, max_tokens=safe_out, enable_thinking=True,
        reasoning_max_tokens=safe_reasoning,
    )
    assert _budget(captured) == 2048
