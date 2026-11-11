"""专家技能允许集对 SkillRein 的守卫：触发词自动匹配也不得越过允许集。

routes 层只收紧显式 skill_ids 是不够的 —— SkillRein 在 AGENT_START 还会把
自动匹配到的技能目录注入 system prompt，不在这里过滤就等于开了后门。
"""
import asyncio

from agent.harness.contracts import RunContext
from agent.harness.reins.lifecycle import SkillRein


class _FakeRegistry:
    def __init__(self):
        self.catalog_ids = None
        self.preload_ids = None

    def match_skills(self, task, mode, max_results=3):
        return [{"id": "s1"}, {"id": "s9"}]

    def build_skill_catalog(self, ids, mode):
        self.catalog_ids = set(ids)
        return "CATALOG"

    def build_skill_preload(self, ids, mode):
        self.preload_ids = set(ids)
        return "PRELOAD"


def _ctx(allowed):
    ctx = RunContext.new(
        task="检查磁盘", skill_ids=["s1", "s2"],
        auto_skill_match=True, system_prompt="BASE",
    )
    if allowed is not None:
        ctx.metadata["expert_skill_ids"] = allowed
    return ctx


def _check(ctx, monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr("skill_registry.get_skill_registry", lambda: registry)
    asyncio.run(SkillRein().check(ctx))
    return registry


def test_expert_allowlist_filters_explicit_and_auto_matches(monkeypatch):
    ctx = _ctx(["s1"])
    registry = _check(ctx, monkeypatch)
    assert registry.catalog_ids == {"s1"}
    assert registry.preload_ids == {"s1"}
    assert "CATALOG" in ctx.system_prompt


def test_no_expert_leaves_skill_injection_unchanged(monkeypatch):
    ctx = _ctx(None)
    registry = _check(ctx, monkeypatch)
    assert registry.catalog_ids == {"s1", "s2", "s9"}
    assert registry.preload_ids == {"s1", "s2"}


def test_empty_allowlist_injects_nothing(monkeypatch):
    ctx = _ctx([])
    registry = _check(ctx, monkeypatch)
    assert registry.catalog_ids is None
    assert registry.preload_ids is None
    assert ctx.system_prompt == "BASE"
