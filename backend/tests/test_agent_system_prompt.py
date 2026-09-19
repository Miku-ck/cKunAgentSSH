"""Agent system prompt 拼装测试（routes/agent.py::_build_agent_system_prompt）。

本文件锁住"运维方法论 + 目标机环境画像"两条新增注入的契约：
  - 变更任务 → OPS_METHOD_CHANGE（含影响说明/回滚），只读任务 → OPS_METHOD_READONLY；
  - is_change=None（未做意图识别）→ 按变更处理（两条误判路径代价不对称）；
  - 环境画像紧跟角色设定注入，且对话型任务不注入；
  - 原有工具/拆分/子Agent/输出格式引导不得被挤掉。
"""
import pytest

from prompts import OPS_METHOD_CHANGE, OPS_METHOD_READONLY
from routes.agent import AgentRunRequest, _build_agent_system_prompt

PROFILE = "[目标机环境] 发行版: Ubuntu 22.04.3 LTS; init: systemd; 包管理器: apt-get"


def _req(**kw) -> AgentRunRequest:
    base = dict(task="重启 nginx", conversation_id="c1", mode="ssh")
    base.update(kw)
    return AgentRunRequest(**base)


# ── 运维方法论二选一 ───────────────────────────────────────────────────────

def test_change_task_gets_change_method():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    assert OPS_METHOD_CHANGE in prompt
    assert OPS_METHOD_READONLY not in prompt


def test_readonly_task_gets_readonly_method():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=False)
    assert OPS_METHOD_READONLY in prompt
    assert OPS_METHOD_CHANGE not in prompt
    assert "变更任务" not in prompt


def test_unknown_is_change_defaults_to_change_method():
    """未做意图识别时按变更处理：宁可多说影响说明，不可给出"不改变目标机状态"的错误前提"""
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert OPS_METHOD_CHANGE in prompt
    assert OPS_METHOD_READONLY not in prompt


def test_change_method_is_self_conditioning():
    """变更方法论第 0 条允许模型在探测后判定为纯只读并自行降级 ——
    这是"宁滥勿缺"判定策略不至于污染只读追问的关键"""
    assert "纯只读" in OPS_METHOD_CHANGE
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    assert "纯只读" in prompt


# ── 目标机环境画像注入 ─────────────────────────────────────────────────────

def test_host_profile_injected():
    prompt = _build_agent_system_prompt(_req(), "", False, host_profile_text=PROFILE)
    assert PROFILE in prompt


def test_host_profile_placed_before_methodology_and_tool_guidance():
    prompt = _build_agent_system_prompt(_req(), "", False, host_profile_text=PROFILE)
    assert prompt.index(PROFILE) < prompt.index(OPS_METHOD_CHANGE)
    assert prompt.index(PROFILE) < prompt.index("工具使用效率")


def test_host_profile_omitted_when_empty():
    prompt = _build_agent_system_prompt(_req(), "", False, host_profile_text="")
    assert "[目标机环境]" not in prompt


def test_host_profile_not_injected_for_conversation_task():
    """对话型任务即便传了画像也不注入（不调工具，环境事实无意义）"""
    prompt = _build_agent_system_prompt(
        _req(task="你好"), "", True, host_profile_text=PROFILE,
    )
    assert PROFILE not in prompt
    assert "[目标机环境]" not in prompt


# ── 对话型分支 ─────────────────────────────────────────────────────────────

def test_conversation_task_skips_methodology_and_tool_guidance():
    prompt = _build_agent_system_prompt(_req(task="你好"), "", True, is_change=False)
    assert OPS_METHOD_CHANGE not in prompt
    assert OPS_METHOD_READONLY not in prompt
    assert "工具使用效率" not in prompt
    assert "子 Agent" not in prompt
    assert "纯对话型问题" in prompt


# ── 原有引导不得被挤掉（回归守护）─────────────────────────────────────────

@pytest.mark.parametrize("fragment", [
    "工具使用效率",
    "长任务规则",
    "任务拆分",
    "子 Agent",
    "输出格式",
])
def test_existing_guidance_preserved(fragment):
    prompt = _build_agent_system_prompt(
        _req(), "", False, host_profile_text=PROFILE, is_change=True,
    )
    assert fragment in prompt


def test_time_and_mode_role_present():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "[系统时间]" in prompt
    assert "cKunAgentSSH 的智能运维助手" in prompt


def test_user_system_prompt_and_reflection_still_appended():
    prompt = _build_agent_system_prompt(
        _req(system_prompt="[自定义] 严格模式"),
        "[反思] 上次用了错误的包管理器",
        False,
        host_profile_text=PROFILE,
    )
    assert "[自定义] 严格模式" in prompt
    assert "[反思] 上次用了错误的包管理器" in prompt
