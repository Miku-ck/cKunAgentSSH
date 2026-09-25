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
    base = dict(task="重启 nginx", conversation_id="c1")
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
    prompt = _build_agent_system_prompt(_req(), PROFILE, False)
    assert PROFILE in prompt


def test_host_profile_placed_before_methodology_and_tool_guidance():
    prompt = _build_agent_system_prompt(_req(), PROFILE, False)
    assert prompt.index(PROFILE) < prompt.index(OPS_METHOD_CHANGE)
    assert prompt.index(PROFILE) < prompt.index("工具使用效率")


def test_host_profile_omitted_when_empty():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "[目标机环境]" not in prompt


def test_host_profile_not_injected_for_conversation_task():
    """对话型任务即便传了画像也不注入（不调工具，环境事实无意义）"""
    prompt = _build_agent_system_prompt(_req(task="你好"), PROFILE, True)
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
    prompt = _build_agent_system_prompt(_req(), PROFILE, False, is_change=True)
    assert fragment in prompt


def test_time_and_mode_role_present():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "[系统时间]" in prompt
    assert "cKunAgentSSH 的智能运维助手" in prompt


def test_user_system_prompt_appended_and_reflection_backdoor_gone():
    """v2 反思记忆走统一检索（[相关记忆]），不再有 system prompt 的独立注入参数。

    原 reflection_injection 后门会把 agent_strategies 的近 3 条反思无条件拼进
    系统提示 —— 与记忆库同源却绕过去重/相关性/预算三道闸，两通道迟早互相矛盾。
    """
    import inspect

    prompt = _build_agent_system_prompt(
        _req(system_prompt="[自定义] 严格模式"),
        PROFILE,
        False,
    )
    assert "[自定义] 严格模式" in prompt
    params = inspect.signature(_build_agent_system_prompt).parameters
    assert "reflection_injection" not in params
    assert "host_profile_text" not in params


def test_memory_tool_guidance_present():
    """记忆双通道的用法引导必须随运维任务注入，否则模型不知道有工具可主动查/记"""
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    assert "记忆使用" in prompt
    for name in ("memory_search", "memory_add", "block_update", "memory_invalidate"):
        assert name in prompt
    conv = _build_agent_system_prompt(_req(task="你好"), "", True)
    assert "记忆使用" not in conv


# ── 对话姿态（workflow_mode：goal / plan / 未表态）──────────────────────────

def test_goal_posture_injects_goal_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="goal"), "", False)
    assert "「目标」姿态" in prompt
    assert "「计划」姿态" not in prompt


def test_plan_posture_injects_plan_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="plan"), "", False)
    assert "「计划」姿态" in prompt
    assert "「目标」姿态" not in prompt


def test_absent_posture_injects_neither_prompt():
    """未表态（字段缺省 → None）时两种姿态都不注入。

    这是「目标」chip 的 × 能真正关掉的前提：若 None 退化成 goal，前端关掉姿态
    与保持原样发出的是同一份提示词 —— 开关能拨，但拨的是个摆设。
    """
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "「目标」姿态" not in prompt
    assert "「计划」姿态" not in prompt
    # 姿态缺席不影响其余注入
    assert OPS_METHOD_CHANGE in prompt


def test_workflow_mode_field_accepts_null_and_rejects_unknown():
    """Optional[Literal] —— null 合法，脏值 422 而不是静默退化成某个姿态。"""
    from pydantic import ValidationError

    assert _req(workflow_mode=None).workflow_mode is None
    for junk in ("Goal", "goals", "any"):
        with pytest.raises(ValidationError):
            _req(workflow_mode=junk)
