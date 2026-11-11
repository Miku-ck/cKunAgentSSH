"""Agent system prompt 组装测试（routes/agent.py::_build_agent_system_prompt）。

通用 harness 化后的新契约（spec §5/§8）：
  - 核心中性：general（identity_section=""）下不含运维身份/方法论/运维工具纪律；
  - 组装新序：时间 → 任务性质提示（仅非对话）→ 中性基础身份 → 专家身份段 → 姿态 → goal → 画像 → 用户 prompt → 分支引导；
  - 专家身份段紧随基础身份、先于 goal/画像（不再末尾追加）；
  - 任务性质提示：非对话含且先于身份段，None→按变更，对话分支不含；
  - 运维语义等价：传入运维身份段时角色 + 方法论两变体 + 工具纪律齐全；
  - 通用引导（记忆/拆分/子Agent/输出格式）不得被挤掉。
"""
import inspect

import pytest

from agent.expert_policy import build_identity_section, builtin_general_ops, resolve_expert
from prompts import BASE_IDENTITY_PROMPT, OPS_METHOD_CHANGE, OPS_METHOD_READONLY
from routes.agent import AgentRunRequest, _build_agent_system_prompt

PROFILE = "[目标机环境] 发行版: Ubuntu 22.04.3 LTS; init: systemd; 包管理器: apt-get"
OPS_SECTION = build_identity_section(resolve_expert(builtin_general_ops()))


def _req(**kw) -> AgentRunRequest:
    base = dict(task="重启 nginx", conversation_id="c1")
    base.update(kw)
    return AgentRunRequest(**base)


# ── 核心中性（general 策略）─────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", [
    "智能运维助手", "远程运维方法", "log_cleanup", "读文件用 file_read",
])
def test_core_prompt_is_domain_neutral(fragment):
    """证伪：把 SSH_MODE_PROMPT 注回核心 → 红。"""
    prompt = _build_agent_system_prompt(_req(), PROFILE, False, is_change=True)
    assert fragment not in prompt


def test_core_uses_neutral_base_identity():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert BASE_IDENTITY_PROMPT in prompt


# ── 任务性质提示 ───────────────────────────────────────────────────────────

def test_task_nature_hint_readonly():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=False)
    assert "[任务性质] 只读" in prompt


def test_task_nature_hint_change():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    assert "[任务性质] 可能变更" in prompt


def test_task_nature_hint_unknown_defaults_to_change():
    """证伪：删掉 None 分支 → 红。"""
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "[任务性质] 未判定（按变更对待）" in prompt


def test_task_nature_hint_before_identity_and_absent_in_conversation():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True, identity_section=OPS_SECTION)
    assert prompt.index("[任务性质]") < prompt.index(OPS_SECTION)
    conv = _build_agent_system_prompt(_req(task="你好"), "", True)
    assert "[任务性质]" not in conv


# ── 专家身份段位置 ─────────────────────────────────────────────────────────

def test_identity_section_right_after_base_before_goal_and_profile():
    """证伪：挪回末尾追加 → 红。"""
    prompt = _build_agent_system_prompt(
        _req(), PROFILE, False, is_change=True,
        goal_text="[当前目标] 梳理索引", identity_section=OPS_SECTION,
    )
    assert prompt.index(BASE_IDENTITY_PROMPT) < prompt.index(OPS_SECTION)
    assert prompt.index(OPS_SECTION) < prompt.index("[当前目标]")
    assert prompt.index(OPS_SECTION) < prompt.index(PROFILE)


def test_empty_identity_section_adds_no_bytes():
    with_empty = _build_agent_system_prompt(_req(), "", False, identity_section="")
    without = _build_agent_system_prompt(_req(), "", False)
    assert with_empty == without


# ── 运维语义等价（传入运维身份段）──────────────────────────────────────────

def test_ops_section_carries_role_and_both_methods():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True, identity_section=OPS_SECTION)
    assert "智能运维助手" in prompt
    assert OPS_METHOD_READONLY in prompt
    assert OPS_METHOD_CHANGE in prompt
    assert "纯只读" in prompt


# ── 画像注入 ───────────────────────────────────────────────────────────────

def test_host_profile_injected():
    assert PROFILE in _build_agent_system_prompt(_req(), PROFILE, False)


def test_host_profile_before_general_tool_guidance():
    prompt = _build_agent_system_prompt(_req(), PROFILE, False)
    assert prompt.index(PROFILE) < prompt.index("工具使用效率")


def test_host_profile_omitted_when_empty():
    assert "[目标机环境]" not in _build_agent_system_prompt(_req(), "", False)


def test_host_profile_not_injected_for_conversation():
    prompt = _build_agent_system_prompt(_req(task="你好"), PROFILE, True)
    assert PROFILE not in prompt


# ── 对话分支 ───────────────────────────────────────────────────────────────

def test_conversation_skips_tools_and_uses_neutral_identity():
    prompt = _build_agent_system_prompt(_req(task="你好"), "", True, is_change=False)
    assert "智能运维助手" not in prompt
    assert "工具使用效率" not in prompt
    assert "子 Agent" not in prompt
    assert "纯对话型问题" in prompt
    assert BASE_IDENTITY_PROMPT in prompt


# ── 通用引导回归守护 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", ["工具使用效率", "记忆使用", "任务拆分", "子 Agent", "输出格式"])
def test_general_guidance_preserved(fragment):
    assert fragment in _build_agent_system_prompt(_req(), PROFILE, False, is_change=True)


def test_memory_tool_guidance_names_tools():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    for name in ("memory_search", "memory_add", "block_update", "memory_invalidate"):
        assert name in prompt


def test_time_injection_present():
    assert "[系统时间]" in _build_agent_system_prompt(_req(), "", False)


def test_user_system_prompt_appended_and_no_backdoor_params():
    prompt = _build_agent_system_prompt(_req(system_prompt="[自定义] 严格模式"), PROFILE, False)
    assert "[自定义] 严格模式" in prompt
    params = inspect.signature(_build_agent_system_prompt).parameters
    assert "reflection_injection" not in params
    assert "host_profile_text" not in params


# ── 对话姿态（workflow_mode）──────────────────────────────────────────────

def test_goal_posture_injects_goal_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="goal"), "", False)
    assert "「目标」姿态" in prompt
    assert "「计划」姿态" not in prompt


def test_plan_posture_injects_plan_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="plan"), "", False)
    assert "「计划」姿态" in prompt
    assert "「目标」姿态" not in prompt


def test_absent_posture_injects_neither():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "「目标」姿态" not in prompt
    assert "「计划」姿态" not in prompt


def test_workflow_mode_field_accepts_null_and_rejects_unknown():
    from pydantic import ValidationError
    assert _req(workflow_mode=None).workflow_mode is None
    for junk in ("Goal", "goals", "any"):
        with pytest.raises(ValidationError):
            _req(workflow_mode=junk)