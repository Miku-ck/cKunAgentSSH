"""子 Agent 的失败不许长成成功（spawn_subagent 的三条诚实性回归）。

本文件早就定过一条口径（D 修复注释）：失败必须以异常上抛，否则 `execute` 会包成
`ToolResult(True)`，主 Agent 把那句失败说明当成子 Agent 的分析读进正文并计入成功徽章。
这轮把同一口径补齐到另外两个漏点：

- 空正文：`return result.content or "[子 Agent 未返回内容]"` 就是一条被计成成功的失败；
  这里 `enable_thinking=False`，「没有正文」= 没有产出，不是产出为空。
- 模型配置**读不到**：原先 `_get_model_config` 吞异常后返回 None，调用方据此报
  「未找到可用模型配置」—— 一句关于用户设置状态的假话（S11-01 同族）。
- 按 id 重取失败时退回那份**脱敏** cfg：等于一定拿一个必 401 的 key 去调用，
  还把故障点推远到「LLM 调用失败」。
"""
import asyncio
from types import SimpleNamespace

import pytest

import agent.harness.llm_client as llm_client
import agent.tools.builtin.spawn_subagent as sub
import database as db


def _tool() -> sub.SpawnSubagentTool:
    return sub.SpawnSubagentTool()


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(cost_tracker=None)


def _run_with_content(content: str):
    class _StubLLM:
        def __init__(self, *_a, **_k):
            pass

        async def call(self, **_kw):
            return SimpleNamespace(content=content, prompt_tokens=1, completion_tokens=1)

    t = _tool()
    t._get_model_config = lambda: {
        "id": "m1", "model_id": "gpt-test", "base_url": "http://x/v1", "api_key": "k",
    }
    real = llm_client.StreamLLMClient
    llm_client.StreamLLMClient = _StubLLM
    try:
        return asyncio.run(t._run_subagent(
            task="查一下", system_prompt="你是运维", extra_context="", tool_ctx=_ctx(),
        ))
    finally:
        llm_client.StreamLLMClient = real


# ── 空正文 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_blank_content_is_a_failure_not_an_empty_success(content):
    with pytest.raises(RuntimeError, match="空内容"):
        _run_with_content(content)


def test_real_content_passes_through_verbatim():
    assert _run_with_content("磁盘水位正常") == "磁盘水位正常"


def test_execute_reports_blank_subagent_run_as_failure(monkeypatch):
    """端到端：异常必须在 execute 落成 ToolResult(False)，不能被包成成功徽章。"""
    async def _raise(**_kw):
        raise RuntimeError("[子 Agent 返回空内容]")

    t = _tool()
    monkeypatch.setattr(t, "_run_subagent", _raise)
    res = asyncio.run(t.execute(
        context=_ctx(), task="查一下", system_prompt="你是运维", extra_context="",
    ))
    assert res.success is False
    assert "空内容" in res.error
    assert res.output == ""


# ── 模型配置：没读到 ≠ 没配 ──────────────────────────────────────────────────

def test_model_config_read_failure_propagates(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("database is locked")

    monkeypatch.setattr(db, "get_default_model", _boom)
    with pytest.raises(OSError):
        _tool()._get_model_config()


def test_model_config_genuinely_empty_still_returns_none(monkeypatch):
    monkeypatch.setattr(db, "get_default_model", lambda *_a, **_k: None)
    monkeypatch.setattr(db, "list_models_config", lambda *a, **k: [])
    assert _tool()._get_model_config() is None


def test_masked_config_is_never_handed_to_the_subagent(monkeypatch):
    """按 id 重取拿不到就是没读到，不许退回那份脱敏 cfg（带着它调用必然 401）。"""
    masked = {"id": "row-9", "model_id": "gpt-test", "base_url": "http://x/v1",
              "api_key": "***masked***"}
    monkeypatch.setattr(db, "get_default_model", lambda *_a, **_k: masked)
    monkeypatch.setattr(db, "get_model_config", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="row-9"):
        _tool()._get_model_config()


def test_unmasked_config_is_used(monkeypatch):
    fresh = {"id": "row-9", "api_key": "real-key", "base_url": "http://x/v1"}
    monkeypatch.setattr(db, "get_default_model",
                        lambda *_a, **_k: {"id": "row-9", "api_key": "***"})
    monkeypatch.setattr(db, "get_model_config", lambda *_a, **_k: fresh)
    assert _tool()._get_model_config() is fresh
