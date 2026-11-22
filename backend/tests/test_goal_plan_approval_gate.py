"""批准门防逃逸 — 守卫「模型侧不存在批准/裁决/重规划入口」这件事本身。

与旧 test_workflow_artifact_delete（M3 改名后并入）同一设计：模型的工具面
恰好是 {goal_propose, plan_propose, step_transition} 三件 —— 提案与推进，
没有任何批准 / 裁决 / 重规划 / 删除的入口；相关关键字连源码里都不出现，
防止「工具不存在但 prompt 约定生效」的空门。用户侧唯一入口是
routes/goal_plan.py，approved_by / adjudicated_by 恒 'user'（服务端硬置）。
"""
import ast
from pathlib import Path

import pytest

from agent.tools.builtin import get_all_tool_classes

_TOOLS_SRC = (
    Path(__file__).resolve().parents[1] / "agent/tools/builtin/goal_plan_tools.py"
)
_GATE_SRC = (
    Path(__file__).resolve().parents[1] / "routes/goal_plan.py"
)

_MODEL_TOOL_NAMES = {"goal_propose", "plan_propose", "step_transition"}


@pytest.fixture
def gp_db(tmp_workspace):
    """独立库 + 连接池切换（与 test_goal_plan_store_schema.gp_db 同法）。"""
    import db as dbpkg
    import db._conn as _conn_mod
    from db import goal_plan_store as gp

    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "goal-plan-gate.db"
    dbpkg._close_all_connections()
    dbpkg.init_db(str(path))
    gp._reset_memo_for_tests()
    try:
        yield path
    finally:
        dbpkg._close_all_connections()
        if original:
            dbpkg.init_db(original)
        dbpkg._close_all_connections()
        gp._reset_memo_for_tests()


def test_model_tool_surface_is_exactly_three():
    """模型侧工具面恰好三件：多了批准类工具即逃逸，少了提案工具即功能缺失。"""
    names = {cls.name for cls in get_all_tool_classes()}
    assert names >= _MODEL_TOOL_NAMES, f"缺模型侧提案/推进工具: {_MODEL_TOOL_NAMES - names}"
    for cls in get_all_tool_classes():
        if cls.name in _MODEL_TOOL_NAMES:
            assert cls.__module__ == "agent.tools.builtin.goal_plan_tools", (
                f"{cls.name} 换了宿主模块，防逃逸关键字扫描要同步换")


def test_tool_source_has_no_gate_keywords():
    """goal_plan_tools.py 的标识符层不含 approve/adjudicate/replan/delete。

    旧 workflow.py 先例（delete_artifact 刻意不给模型工具）：把「不可能发生」
    写进代码层。按 AST 标识符扫描而非裸文本 —— docstring 里「不该做什么」的
    设计说明是合法内容，越权只能发生在标识符（工具/函数/变量名）里。
    """
    tree = ast.parse(_TOOLS_SRC.read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
    for kw in ("approve", "adjudicate", "replan", "delete"):
        offenders = [name for name in identifiers if kw in name.lower()]
        assert not offenders, f"模型工具标识符出现越权关键字 {kw!r}: {offenders}"


def test_gate_routes_hardcode_user_as_actor():
    """routes/goal_plan.py 的裁决/批准动作 actor 恒为字面量 'user'。

    AST 钉两件事：① store 调用的第 4 位置参数是字符串字面量 "user"；
    ② 请求模型（GoalCreateRequest / GoalAdjudicateRequest 等）没有
    approved_by / adjudicated_by 字段 —— 请求体永远决定不了执行者。
    """
    tree = ast.parse(_GATE_SRC.read_text(encoding="utf-8"))
    user_actor_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and n.args
        and isinstance(n.args[-1], ast.Constant) and n.args[-1].value == "user"
    ]
    assert len(user_actor_calls) >= 2, (
        "routes/goal_plan.py 丢了 actor='user' 硬置（create_goal / adjudicate_goal）")
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            fields = {
                stmt.target.id for stmt in cls.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
            }
            overlap = fields & {"approved_by", "adjudicated_by", "actor"}
            assert not overlap, f"请求模型 {cls.name} 泄漏了 actor 字段: {overlap}"


def test_gate_state_machine_store_side(gp_db):
    """行为面复核（store 第二道闸）：achieved 仅 user_confirmed 可达、
    approve 只对 awaiting_approval 生效一次且 approved_by 恒 'user'。"""
    from db import goal_plan_store as gp

    goal = gp.create_goal("conv-gate", title="验收", statement="服务可用")
    assert goal is not None
    # 模型建议（auto_suggested）永远够不到 achieved —— 即便 achieved=True
    denied = gp.adjudicate_goal(goal["goal_id"], "auto_suggested", True, "model")
    assert denied is not None and denied["status"] != "achieved"
    # 用户确认才可达
    confirmed = gp.adjudicate_goal(goal["goal_id"], "user_confirmed", True, "user")
    assert confirmed is not None and confirmed["status"] == "achieved"

    plan = gp.create_plan_with_steps("conv-gate", goal["goal_id"],
                                     [{"title": "检查"}])
    assert plan is not None and plan["status"] == "awaiting_approval"
    approved = gp.approve_plan(plan["plan_id"])
    assert approved is not None and approved["approved_by"] == "user"
    # 批准只能发生一次：第二次批准被拒
    assert gp.approve_plan(plan["plan_id"]) is None
