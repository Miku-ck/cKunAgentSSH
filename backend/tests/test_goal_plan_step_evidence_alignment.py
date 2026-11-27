"""步骤级 goal 对齐 — 守卫「每步完成证据对照成功标准」的提示约束不被删。

收尾自检（[目标验收]，driver 收尾锚）只覆盖结束对账；过程对账靠
step_transition / plan_propose 的提示约束 —— 模型 completed 时在 evidence
里注明本步推进了哪条成功标准。对齐刻意不做 FSM 硬校验（证据写法各异，
字符串匹配硬闸会把合法证据挡在门外），只能靠源码文本守卫钉住。
与 test_goal_plan_approval_gate 同风格（读源码断言）。
"""
from pathlib import Path

_TOOLS_SRC = (
    Path(__file__).resolve().parents[1] / "agent/tools/builtin/goal_plan_tools.py"
)
_FSM_SRC = (
    Path(__file__).resolve().parents[1] / "agent/harness/goal_plan_fsm.py"
)

_ALIGNMENT_WORding = "注明本步推进了哪条成功标准"


def test_step_transition_evidence_requires_criteria_alignment():
    """step_transition 的 description 要求 completed 证据对照成功标准。"""
    src = _TOOLS_SRC.read_text(encoding="utf-8")
    assert _ALIGNMENT_WORding in src


def test_alignment_wording_appears_in_both_prompt_surfaces():
    """两处提示面都要在：工具 description（模型每次调用看到的约束）与
    plan_propose 的成功返回文案（提交计划时的执行提醒）—— 删任何一处，
    过程对账就退化成"做完才统一对账"。"""
    src = _TOOLS_SRC.read_text(encoding="utf-8")
    assert src.count(_ALIGNMENT_WORding) >= 2


def test_alignment_stays_out_of_fsm_hard_gate():
    """对齐是提示约束不是硬校验：can_transition 只守 evidence 非空，不做
    内容匹配 —— 字符串硬闸会把写法各异的合法证据挡在门外。"""
    import ast

    tree = ast.parse(_FSM_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "can_transition"
    )
    strings = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any(
        "标准" in s or "criterion" in s.lower() for s in strings
    ), "FSM 转移判定里出现了对 evidence 的内容匹配 —— 那是硬闸，不该存在"
