"""goal_plan 存储层 schema 与约束 — 守卫「持久化结构成立」这件事本身。

- 三表存在、乐观锁生效（并发修订仅一者成功）；
- 同会话至多一个活动 plan（条件唯一索引 + 应用层双保险）；
- approve 只对 awaiting_approval 生效一次，approved_by 恒 'user'；
- step 跃迁走 FSM fail-closed：非法转移、缺 evidence 全部被拒。
"""
from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from db import goal_plan_store as gp


@pytest.fixture
def gp_db(tmp_workspace):
    """独立库 + 连接池切换（与 test_run_checkpoints.ck_db 同法）。"""
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "goal-plan.db"
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


def test_three_tables_exist(gp_db):
    gp.ensure_tables()  # 首用路径：store 函数内部都会触发，这里显式走一遍
    conn = _conn_mod._get_conn()
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE 'goal_plan_%'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"goal_plan_goals", "goal_plan_plans", "goal_plan_steps"} <= names


def test_goal_create_and_optimistic_lock(gp_db):
    goal = gp.create_goal("conv-1", title="恢复服务", statement="服务可用")
    assert goal is not None and goal["status"] == "draft"
    assert goal["adjudication"] == "pending" and goal["version"] == 1

    # 乐观锁：expected_version=1 的第一次修订成功（version → 2）
    revised = gp.revise_goal(goal["goal_id"], 1, {"statement": "服务可用且日志无错"})
    assert revised is not None and revised["version"] == 2

    # 第二个并发方还拿着 version=1，必须失败
    assert gp.revise_goal(goal["goal_id"], 1, {"statement": "过期的修订"}) is None

    # 同会话重复创建被拒（唯一索引）
    assert gp.create_goal("conv-1", title="x", statement="y") is None


def test_goal_status_patch_whitelist(gp_db):
    goal = gp.create_goal("conv-2", title="", statement="s")
    assert goal is not None
    # achieved 不能经修订入口写入（只走裁决）
    assert gp.revise_goal(goal["goal_id"], 1, {"status": "achieved"}) is None
    revised = gp.revise_goal(goal["goal_id"], 1, {"status": "active"})
    assert revised is not None and revised["status"] == "active"


def test_goal_adjudication_achieved_requires_user_confirm(gp_db):
    goal = gp.create_goal("conv-3", title="", statement="s")
    assert goal is not None
    # auto_suggested 声称达成：不生效
    out = gp.adjudicate_goal(goal["goal_id"], "auto_suggested", True, "model")
    assert out is not None and out["status"] != "achieved"
    # user_confirmed 才可达 achieved
    out = gp.adjudicate_goal(goal["goal_id"], "user_confirmed", True, "user")
    assert out is not None and out["status"] == "achieved"
    # 非法裁决取值拒绝
    assert gp.adjudicate_goal(goal["goal_id"], "model_said_so", False, "model") is None


def test_single_active_plan_per_conversation(gp_db):
    goal = gp.create_goal("conv-4", title="", statement="s")
    assert goal is not None
    steps = [{"title": "第一步"}, {"title": "第二步"}]
    plan = gp.create_plan_with_steps("conv-4", goal["goal_id"], steps)
    assert plan is not None and plan["status"] == "awaiting_approval"
    assert plan["version"] == 1

    # 活动名额占用中：第二个 plan 被拒
    assert gp.create_plan_with_steps("conv-4", goal["goal_id"], steps) is None

    # 重规划：先 supersede 旧的，再建新的（version 递增）
    assert gp.supersede_plan(plan["plan_id"]) is True
    plan2 = gp.create_plan_with_steps(
        "conv-4", goal["goal_id"], steps, supersedes_plan_id=plan["plan_id"])
    assert plan2 is not None and plan2["version"] == 2
    assert plan2["supersedes_plan_id"] == plan["plan_id"]
    assert gp.get_plan(plan["plan_id"])["status"] == "superseded"


def test_approve_gate_single_shot_and_user_only(gp_db):
    goal = gp.create_goal("conv-5", title="", statement="s")
    assert goal is not None
    plan = gp.create_plan_with_steps(
        "conv-5", goal["goal_id"], [{"title": "a"}])
    assert plan is not None

    approved = gp.approve_plan(plan["plan_id"])
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "user"
    assert approved["approved_at"]

    # 只能批准一次：approved → approve 返回 None
    assert gp.approve_plan(plan["plan_id"]) is None


def test_step_transition_fail_closed(gp_db):
    goal = gp.create_goal("conv-6", title="", statement="s")
    assert goal is not None
    plan = gp.create_plan_with_steps(
        "conv-6", goal["goal_id"],
        [{"title": "a"}, {"title": "b"}])
    assert plan is not None
    steps = gp.get_steps(plan["plan_id"])
    assert [s["ordinal"] for s in steps] == [1, 2]
    first = steps[0]

    # 非法转移：pending → completed 被拒（矩阵先于 evidence 校验）
    _, reason = gp.update_step_status(first["step_id"], "completed", evidence="x")
    assert "不允许" in reason

    # 合法链：先批准，再 pending → in_progress（plan approved → active）
    assert gp.approve_plan(plan["plan_id"]) is not None
    _, reason = gp.update_step_status(
        first["step_id"], "in_progress", run_id="run-1", iteration=1)
    assert not reason
    assert gp.get_plan(plan["plan_id"])["status"] == "active"

    # completed 缺 evidence 被拒
    _, reason = gp.update_step_status(first["step_id"], "completed")
    assert "evidence" in reason

    _, reason = gp.update_step_status(
        first["step_id"], "completed", evidence="回显输出")
    assert not reason
    steps = gp.get_steps(plan["plan_id"])
    first = next(s for s in steps if s["step_id"] == first["step_id"])
    assert first["finished_at"]

    # 终态不可再转移
    _, reason = gp.update_step_status(first["step_id"], "in_progress")
    assert reason


def test_snapshot_and_degrade(gp_db):
    assert gp.get_snapshot("conv-none") == gp.empty_snapshot()
    goal = gp.create_goal("conv-7", title="", statement="s")
    assert goal is not None
    plan = gp.create_plan_with_steps(
        "conv-7", goal["goal_id"], [{"title": "a"}])
    assert plan is not None
    snap = gp.get_snapshot("conv-7")
    assert snap["goal"]["goal_id"] == goal["goal_id"]
    assert snap["plan"]["plan_id"] == plan["plan_id"]
    assert snap["progress"]["total"] == 1
