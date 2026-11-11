"""头less 专家接线守卫：schedule 去硬编码 / runbook 解析运维身份 / slug fail-closed。

证伪：把 SSH_MODE_PROMPT 硬编码注回 schedule 构建器 → 红。
"""
import pytest

from agent.schedule_engine import _build_schedule_system_prompt
from prompts import BASE_IDENTITY_PROMPT, SSH_MODE_PROMPT


def test_schedule_prompt_no_hardcoded_ops_identity():
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": "h"})
    assert SSH_MODE_PROMPT not in prompt
    assert "智能运维助手" not in prompt
    assert BASE_IDENTITY_PROMPT in prompt


def test_schedule_prompt_keeps_no_host_guard_and_report_format():
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": ""})
    assert "未指定目标主机" in prompt
    assert "定时任务" in prompt


def test_schedule_prompt_inserts_identity_section():
    section = "专家角色设定（运维专家）：\n运维身份测试段"
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": "h"}, section)
    assert section in prompt
    assert prompt.index(BASE_IDENTITY_PROMPT) < prompt.index(section)


def test_schedule_prompt_empty_identity_adds_no_bytes():
    a = _build_schedule_system_prompt({"name": "s", "target_host": "h"}, "")
    b = _build_schedule_system_prompt({"name": "s", "target_host": "h"})
    assert a == b


_MODEL_CFG = {
    "base_url": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 8000,
}


async def _fake_resolution(*a, **k):
    from agent.environment_policy import EnvironmentResolution
    return EnvironmentResolution(effective="test", requested="test", source="none", host="")


def _stub_assembly(monkeypatch, hr, captured):
    """run_headless_once 装配链全量打桩，只留专家解析走真实 base_db。

    create_registry 在函数体内 `from agent.tools import create_registry`，故桩
    必须打到 agent.tools 模块缝上（与 test_runbook_engine_headless_shared 同口径）。
    """
    class _FakeHarness:
        def __init__(self, *a, **k):
            pass

        def assemble(self, *a, **k):
            pass

        async def run(self, **kw):
            captured["system_prompt"] = kw.get("system_prompt", "")
            yield type("Ev", (), {
                "kind": "done",
                "payload": {"content": "ok", "state": "completed"},
            })()

    monkeypatch.setattr(hr, "Harness", _FakeHarness)
    monkeypatch.setattr(hr, "resolve_model", lambda: dict(_MODEL_CFG))
    monkeypatch.setattr(hr, "StreamLLMClient", lambda **k: None)
    monkeypatch.setattr(hr, "CostTracker", lambda **k: None)
    monkeypatch.setattr("agent.tools.create_registry", lambda: None)
    monkeypatch.setattr(hr, "resolve_run_environment", _fake_resolution)


@pytest.mark.asyncio
async def test_runbook_resolves_ops_identity(base_db, monkeypatch):
    """runbook 无头 run 传 general_ops → system_prompt 含运维身份段。"""
    import agent.headless_run as hr

    captured = {}
    _stub_assembly(monkeypatch, hr, captured)
    await hr.run_headless_once(
        "步骤任务", run_id="r1", conversation_id="runbook-x",
        ssh_session_id="", expert_slug="general_ops",
    )
    assert "智能运维助手" in captured["system_prompt"]
    assert "远程运维方法" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_headless_unknown_slug_fail_closed(base_db, monkeypatch):
    import agent.headless_run as hr

    monkeypatch.setattr(hr, "resolve_model", lambda: dict(_MODEL_CFG))
    with pytest.raises(ValueError):
        await hr.run_headless_once(
            "t", run_id="r2", conversation_id="c", expert_slug="no-such-expert",
        )