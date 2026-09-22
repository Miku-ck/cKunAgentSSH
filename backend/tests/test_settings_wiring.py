"""设置项生效性回归测试 — 前端设置面板的开关必须真实改变后端行为。

覆盖上一轮设置审查发现的两条"纯摆设"接线的修复：
  1. chat.autoSkillMatch（自动技能匹配）→ /agent/run auto_skill_match
     → RunContext.auto_skill_match → SkillRein 跳过/执行触发词匹配。
     回归守护：关闭后 match_skills 不得被调用；显式 skill_ids 仍须注入。
  2. chat.contextWindow（上下文窗口）→ /agent/run context_window
     的默认值与边界校验（1-100）。

历史消息条数由 routes/agent.py 的 get_recent_messages(limit=req.context_window)
消费，此处校验请求模型契约；DB 读取行为不重复覆盖。
"""

import pytest

from agent.harness.contracts import RunContext
from agent.harness.reins import SkillRein
from routes.agent import AgentRunRequest


# ── SkillRein：auto_skill_match 开/关 ──────────────────────────────────────

class _FakeRegistry:
    """假技能注册表：记录 match_skills 调用次数，返回固定技能。"""

    def __init__(self, auto_skills):
        self.auto_skills = auto_skills
        self.match_calls = 0

    def match_skills(self, message, mode, max_results=3):
        self.match_calls += 1
        return self.auto_skills

    def build_skill_catalog(self, skill_ids, mode):
        return "CATALOG " + ",".join(skill_ids)

    def build_skill_preload(self, skill_ids, mode):
        return "PRELOAD " + ",".join(skill_ids)


def _make_ctx(auto_skill_match, skill_ids=()):
    ctx = RunContext.new(
        task="帮我写个 git 脚本",
        mode="ssh",
        skill_ids=list(skill_ids),
        auto_skill_match=auto_skill_match,
    )
    ctx.system_prompt = "BASE"
    return ctx


@pytest.mark.asyncio
async def test_auto_match_on_injects_auto_and_explicit(monkeypatch):
    """默认/开启：自动匹配 + 显式选择都注入。"""
    import skill_registry as reg_mod
    fake = _FakeRegistry([{"id": "sk-auto"}])
    monkeypatch.setattr(reg_mod, "get_skill_registry", lambda: fake)

    ctx = _make_ctx(True, skill_ids=["sk-explicit"])
    await SkillRein().check(ctx)

    assert fake.match_calls == 1
    assert "sk-auto" in ctx.system_prompt
    assert "sk-explicit" in ctx.system_prompt


@pytest.mark.asyncio
async def test_auto_match_off_skips_trigger_matching(monkeypatch):
    """关闭：match_skills 不被调用（设置真正生效的核心断言）；
    显式选择的技能仍须注入（语义：关闭自动，不关闭手动）。"""
    import skill_registry as reg_mod
    fake = _FakeRegistry([{"id": "sk-auto"}])
    monkeypatch.setattr(reg_mod, "get_skill_registry", lambda: fake)

    ctx = _make_ctx(False, skill_ids=["sk-explicit"])
    await SkillRein().check(ctx)

    assert fake.match_calls == 0
    assert "sk-auto" not in ctx.system_prompt
    assert "sk-explicit" in ctx.system_prompt


@pytest.mark.asyncio
async def test_auto_match_off_no_explicit_no_injection(monkeypatch):
    """关闭且无显式选择：完全不注入（catalog 为空 → system_prompt 不变）。"""
    import skill_registry as reg_mod
    fake = _FakeRegistry([{"id": "sk-auto"}])
    monkeypatch.setattr(reg_mod, "get_skill_registry", lambda: fake)

    ctx = _make_ctx(False)
    await SkillRein().check(ctx)

    assert fake.match_calls == 0
    assert ctx.system_prompt == "BASE"


def test_runcontext_auto_skill_match_default_true():
    """未传参时默认开启（与请求模型默认值一致，兼容旧调用方）。"""
    assert RunContext.new(task="t", mode="ssh").auto_skill_match is True
    assert RunContext.new(task="t", mode="ssh", auto_skill_match=False).auto_skill_match is False


# ── AgentRunRequest：context_window / auto_skill_match 契约 ─────────────────

def test_request_defaults():
    req = AgentRunRequest(task="t", conversation_id="c")
    assert req.auto_skill_match is True
    assert req.context_window == 20


def test_request_custom_values():
    req = AgentRunRequest(
        task="t", conversation_id="c",
        context_window=50, auto_skill_match=False,
    )
    assert req.context_window == 50
    assert req.auto_skill_match is False


def test_request_context_window_bounds():
    """1-100 之外拒绝（防止恶意/异常值拉空或拖垮历史查询）。"""
    from pydantic import ValidationError
    for bad in (0, -1, 101, 10000):
        with pytest.raises(ValidationError):
            AgentRunRequest(task="t", conversation_id="c", context_window=bad)
    # 边界值合法
    assert AgentRunRequest(task="t", conversation_id="c", context_window=1).context_window == 1
    assert AgentRunRequest(task="t", conversation_id="c", context_window=100).context_window == 100
