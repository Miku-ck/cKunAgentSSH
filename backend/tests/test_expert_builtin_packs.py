"""四个内置专家包的配置契约：运维包语义等价 / 领域包收紧 / general 全继承 / 尺寸自律。

红线基准从 general_ops 迁到 general（spec §8）：未选专家 ≡ general。
general_ops 不再是「等价于无专家」，而是真运维包——身份段含角色 + 方法论两变体 + 工具纪律。
"""
import json

import pytest

from agent.expert_policy import (
    DEFAULT_EXPERT_SLUG,
    _DOMAIN_READONLY_TOOLS,
    builtin_copywriting,
    builtin_general,
    builtin_general_ops,
    builtin_research,
    build_identity_section,
    filter_tool_names,
    resolve_expert,
)
from prompts import (
    OPS_METHOD_CHANGE,
    OPS_METHOD_READONLY,
)

# 真实注册表里的执行/远程文件工具（守卫按交集断言，spec §4.3）
EXECUTION_TOOLS = [
    "shell_exec", "file_read", "file_write", "file_edit", "file_glob", "file_grep",
    "sys_overview", "process_inspect", "network_probe", "log_read",
    "fleet_sessions", "fleet_overview", "service_manage", "schedule_op", "log_cleanup",
    "job_run", "job_list", "job_output", "job_kill", "undo_restore", "read_image",
]


def test_default_slug_is_general():
    assert DEFAULT_EXPERT_SLUG == "general"


def test_general_is_fully_inherited():
    policy = resolve_expert(builtin_general())
    assert policy.is_legacy
    assert policy.expert_slug == "general"
    assert build_identity_section(policy) == ""


def test_ops_pack_identity_contains_role_and_both_methods_and_discipline():
    """运维包语义等价：角色段关键句 + 方法论两变体全文 + 运维工具纪律齐全。
    证伪：删掉 OPS_IDENTITY_PROMPT 里任一变体 → 红。"""
    policy = resolve_expert(builtin_general_ops())
    section = build_identity_section(policy)
    assert "智能运维助手" in section            # 角色段关键句
    assert OPS_METHOD_READONLY in section       # 只读变体全文
    assert OPS_METHOD_CHANGE in section         # 变更变体全文
    assert "纯只读" in section                  # CHANGE 第 0 条自行降级
    assert "log_cleanup" in section             # 运维工具纪律
    assert "file_read" in section               # 工具名映射句
    assert "job_run" in section                 # 长任务规则
    assert "[任务性质]" in section              # 选择规则引用任务性质提示


def test_ops_pack_tools_and_permission_fully_inherited():
    policy = resolve_expert(builtin_general_ops())
    assert policy.tool_names is None
    assert policy.plugin_ids is None
    assert policy.permission_ceiling == "inherit"
    assert policy.memory_scope == "inherit"
    assert policy.max_iterations is None


@pytest.mark.parametrize("factory", [builtin_research, builtin_copywriting])
def test_domain_pack_has_no_execution_tools(factory):
    """领域包收紧：工具集 ∩ 执行工具 = ∅。证伪：给文案包加 shell_exec → 红。"""
    policy = resolve_expert(factory())
    assert policy.tool_names is not None
    assert not (set(policy.tool_names) & set(EXECUTION_TOOLS))
    # 与真实可用集求交后仍不含执行工具
    available = EXECUTION_TOOLS + list(_DOMAIN_READONLY_TOOLS)
    assert not (set(filter_tool_names(available, policy.tool_names)) & set(EXECUTION_TOOLS))


@pytest.mark.parametrize("factory", [builtin_research, builtin_copywriting])
def test_domain_pack_plugins_explicit_empty_and_memory_global(factory):
    """plugin_ids=() 显式空集（不是 None 继承）；memory_scope=global。
    证伪：置 plugin_ids=None → 红。"""
    policy = resolve_expert(factory())
    assert policy.plugin_ids == ()
    assert policy.memory_scope == "global"
    assert json.loads(factory()["plugin_ids_json"]) == []


@pytest.mark.parametrize("factory", [builtin_general, builtin_general_ops, builtin_research, builtin_copywriting])
def test_builtin_seed_enabled_and_marked(factory):
    seed = factory()
    assert seed["enabled"] == 1
    assert seed["is_builtin"] == 1
    assert seed["revision"] == 1


def test_ops_identity_within_api_limit():
    """内置种子虽走直连 SQL，仍按 API 上限 8000 自律（spec §4.2）。"""
    assert len(builtin_general_ops()["identity_prompt"]) < 8000