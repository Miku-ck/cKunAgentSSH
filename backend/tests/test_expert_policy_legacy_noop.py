"""专家策略的兼容性红线：未选专家 / 选中 general，运行行为与现状完全一致。

「一致」指六个运行面 —— system prompt 追加段、模型解析、工具集、技能选择、
权限姿态、迭代上限、记忆范围。归因元数据（快照里的专家名/revision）允许不同，
那是新增信息，不改变任何下发给模型的内容。
"""
import pytest

from agent.expert_policy import (
    DEFAULT_EXPERT_SLUG,
    ResolvedRunPolicy,
    build_identity_section,
    build_snapshot,
    builtin_general,
    clamp_max_iterations,
    clamp_permission_mode,
    filter_tool_names,
    resolve_expert,
    resolve_memory_scopes,
    resolve_skill_ids,
)

ALL_TOOLS = ["shell_exec", "file_read", "file_edit", "web_search"]


def test_none_expert_is_fully_inherited():
    policy = resolve_expert(None)
    assert policy.is_legacy
    assert policy.expert_slug is None
    assert policy.model_id_override is None
    assert policy.identity_prompt == ""
    assert policy.skill_ids is None
    assert policy.tool_names is None
    assert policy.plugin_ids is None
    assert policy.permission_ceiling == "inherit"
    assert policy.max_iterations is None
    assert policy.memory_scope == "inherit"


def test_general_matches_none_on_every_runtime_dimension():
    legacy = resolve_expert(None)
    default = resolve_expert(builtin_general())

    assert default.expert_slug == DEFAULT_EXPERT_SLUG
    assert default.is_legacy
    # 归因字段允许不同，运行面必须逐项相同
    for field_name in (
        "model_id_override", "identity_prompt", "skill_ids", "tool_names",
        "plugin_ids", "permission_ceiling", "max_iterations", "memory_scope",
    ):
        assert getattr(default, field_name) == getattr(legacy, field_name), field_name


@pytest.mark.parametrize("mode", ["auto", "normal", "plan"])
def test_legacy_permission_is_untouched(mode):
    assert clamp_permission_mode(mode, resolve_expert(None).permission_ceiling) == mode
    assert clamp_permission_mode(mode, "inherit") == mode


def test_legacy_iteration_is_untouched():
    assert clamp_max_iterations(100, resolve_expert(None).max_iterations) == 100
    assert clamp_max_iterations(37, None) == 37


def test_legacy_tool_list_keeps_order_and_content():
    assert filter_tool_names(ALL_TOOLS, None) == ALL_TOOLS


def test_legacy_skill_filter_matches_existing_mode_filter():
    # 现有 _setup_skills 的语义：显式 skill_ids 去掉模式禁用项，顺序不变
    requested = ["skill-a", "skill-b", "skill-c"]
    assert resolve_skill_ids(requested, None, {"skill-b"}) == ["skill-a", "skill-c"]
    assert resolve_skill_ids([], None, {"skill-b"}) == []
    assert resolve_skill_ids(None, None, set()) == []


def test_legacy_memory_scope_uses_inherited_scopes():
    inherited = ["global", "host:sess-1"]
    assert resolve_memory_scopes("inherit", "sess-1", inherited) == inherited


def test_legacy_identity_section_is_empty_string():
    assert build_identity_section(resolve_expert(None)) == ""
    assert build_identity_section(resolve_expert(builtin_general())) == ""
    blank = dict(builtin_general(), identity_prompt="   \n  ")
    assert build_identity_section(resolve_expert(blank)) == ""


def test_general_snapshot_differs_only_in_attribution():
    common = dict(
        model_id="model-1", permission_mode="normal", max_iterations=100,
        tool_names=ALL_TOOLS, skill_ids=["skill-a"], memory_scopes=["global"],
    )
    legacy_snap = build_snapshot(resolve_expert(None), **common)
    default_snap = build_snapshot(resolve_expert(builtin_general()), **common)
    assert legacy_snap["slug"] is None
    assert default_snap["slug"] == DEFAULT_EXPERT_SLUG
    for key in ("model_id", "permission_mode", "max_iterations",
                "tool_names", "skill_ids", "plugin_ids", "memory_scopes"):
        assert legacy_snap[key] == default_snap[key], key


def test_builtin_seed_is_enabled_and_readonly_marked():
    seed = builtin_general()
    assert seed["enabled"] == 1
    assert seed["is_builtin"] == 1
    assert seed["plugin_ids_json"] is None


def test_policy_dataclass_defaults_are_legacy():
    assert ResolvedRunPolicy().is_legacy
