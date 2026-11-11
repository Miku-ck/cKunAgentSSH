"""专家策略的安全不变量：只能收紧，不能放宽；配置损坏 fail-closed。

钉住的是「专家可以做什么」的负向边界 —— 不能复活被禁用的工具/技能、
不能把权限往上抬、不能突破迭代上限、不能读到 persona 记忆、不能把坏配置
静默当成「继承现状」。
"""
import pytest

from agent.expert_policy import (
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

REGISTERED = ["shell_exec", "file_read", "file_edit", "web_search"]


def make_expert(**overrides):
    row = dict(builtin_general())
    row.update({"slug": "custom", "name": "自定义", "is_builtin": 0})
    row.update(overrides)
    return row


# ── 工具：求交而非并集 ────────────────────────────────────────────────────

def test_tool_intersection_never_resurrects_disabled_tools():
    policy = resolve_expert(make_expert(tool_names_json='["file_read", "not_registered"]'))
    result = filter_tool_names(REGISTERED, policy.tool_names)
    assert result == ["file_read"]
    assert "not_registered" not in result


def test_tool_intersection_keeps_registry_order_not_expert_order():
    policy = resolve_expert(make_expert(tool_names_json='["web_search", "shell_exec"]'))
    assert filter_tool_names(REGISTERED, policy.tool_names) == ["shell_exec", "web_search"]


def test_empty_tool_list_disables_everything():
    policy = resolve_expert(make_expert(tool_names_json="[]"))
    assert policy.tool_names == frozenset()
    assert filter_tool_names(REGISTERED, policy.tool_names) == []


def test_missing_tool_field_means_inherit_not_empty():
    policy = resolve_expert(make_expert(tool_names_json=None))
    assert policy.tool_names is None
    assert filter_tool_names(REGISTERED, policy.tool_names) == REGISTERED


# ── 权限：取更严格者 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("current,ceiling,expected", [
    ("auto", "plan", "plan"),
    ("normal", "plan", "plan"),
    ("plan", "plan", "plan"),
    ("auto", "normal", "normal"),
    ("normal", "normal", "normal"),
    ("plan", "normal", "plan"),      # 专家不能把 plan 放宽成 normal
    ("auto", "inherit", "auto"),
    ("plan", "inherit", "plan"),
])
def test_permission_never_loosened(current, ceiling, expected):
    assert clamp_permission_mode(current, ceiling) == expected


def test_invalid_ceiling_rejected():
    with pytest.raises(ValueError):
        clamp_permission_mode("auto", "yolo")
    with pytest.raises(ValueError):
        resolve_expert(make_expert(permission_ceiling="yolo"))


# ── 迭代：只能往下压 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("current,cap,expected", [
    (100, 10, 10),
    (100, 500, 100),
    (100, None, 100),
    (100, 0, 100),
    (100, -5, 100),
    (3, 1, 1),
])
def test_iteration_cap(current, cap, expected):
    assert clamp_max_iterations(current, cap) == expected


def test_non_positive_iteration_cap_in_config_rejected():
    with pytest.raises(ValueError):
        resolve_expert(make_expert(max_iterations=-3))


# ── 技能：允许集 + 禁用过滤在交集之后 ─────────────────────────────────────

def test_user_skill_selection_cannot_escape_expert_allowlist():
    policy = resolve_expert(make_expert(skill_ids_json='["skill-a"]'))
    result = resolve_skill_ids(["skill-a", "skill-b"], policy.skill_ids)
    assert result == ["skill-a"]


def test_expert_skills_preloaded_when_user_selected_none():
    policy = resolve_expert(make_expert(skill_ids_json='["skill-a", "skill-b"]'))
    assert resolve_skill_ids([], policy.skill_ids) == ["skill-a", "skill-b"]


def test_mode_disabled_skill_wins_over_expert_allowlist():
    policy = resolve_expert(make_expert(skill_ids_json='["skill-a", "skill-b"]'))
    assert resolve_skill_ids([], policy.skill_ids, {"skill-b"}) == ["skill-a"]


def test_empty_expert_skill_list_means_no_skills():
    policy = resolve_expert(make_expert(skill_ids_json="[]"))
    assert resolve_skill_ids(["skill-a"], policy.skill_ids) == []


# ── 记忆：只能收缩，永远碰不到 persona ────────────────────────────────────

def test_memory_scope_variants():
    assert resolve_memory_scopes("global", "sess-1") == ["global"]
    assert resolve_memory_scopes("none", "sess-1") == []
    assert resolve_memory_scopes("host", "sess-1") == ["global", "host:sess-1"]


def test_host_scope_without_session_is_empty_not_global():
    assert resolve_memory_scopes("host", None) == []
    assert resolve_memory_scopes("host", "") == []


@pytest.mark.parametrize("scope", ["global", "host", "none", "inherit"])
def test_no_scope_can_reach_persona_memories(scope):
    result = resolve_memory_scopes(scope, "sess-1", ["global"])
    assert not any(str(s).startswith("persona:") for s in result)


def test_invalid_memory_scope_rejected():
    with pytest.raises(ValueError):
        resolve_memory_scopes("persona:1", "sess-1")
    with pytest.raises(ValueError):
        resolve_expert(make_expert(memory_scope="everything"))


# ── 配置损坏：fail-closed，不得当成「继承」 ──────────────────────────────

@pytest.mark.parametrize("bad", ["{not json", '{"a":1}', '"skill-a"'])
def test_corrupt_json_field_raises(bad):
    with pytest.raises(ValueError):
        resolve_expert(make_expert(tool_names_json=bad))


def test_model_override_only_when_non_empty():
    assert resolve_expert(make_expert(model_id="m-1")).model_id_override == "m-1"
    assert resolve_expert(make_expert(model_id="")).model_id_override is None
    assert resolve_expert(make_expert(model_id=None)).model_id_override is None


# ── 身份段与快照 ──────────────────────────────────────────────────────────

def test_identity_section_marks_tool_output_untrusted():
    policy = resolve_expert(make_expert(identity_prompt="你是数据库运维专家"))
    section = build_identity_section(policy)
    assert "数据库运维专家" in section
    assert "不可信" in section
    assert "不改变任何安全约束" in section


def test_snapshot_carries_no_secret_fields():
    policy = resolve_expert(make_expert(plugin_ids_json='["plug-1"]'))
    snap = build_snapshot(
        policy, model_id="m-1", permission_mode="normal", max_iterations=50,
        tool_names=["file_read"], skill_ids=[], memory_scopes=["global"],
    )
    assert snap["plugin_ids"] == ["plug-1"]
    forbidden = {"api_key", "apiKey", "env", "env_ciphertext", "secret", "token", "password"}
    assert not (set(snap) & forbidden)
    assert "command" not in snap
