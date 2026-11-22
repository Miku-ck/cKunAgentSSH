"""goal/plan 断点恢复 — run 崩溃后结构化状态必须可续。

store（SQLite）是唯一真值；ctx.metadata 只存轻量指针（_gp_*）。本文件钉：
- 装配（load_goal_plan_state → fill_run_metadata）在崩溃重启的新 run 里
  产出与库内一致的指针与 criteria_json（幂等，连跑两次结果相同）；
- 快照读取幂等：连续 get_snapshot 逐键相等，force_plan 状态与 plan 行一致；
- 归档证据可续：用户重置后，get_last_superseded_steps 返回带 evidence 的
  已完成步骤 —— 重规划不是从零开始。
"""
import json
from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from routes._goal_plan_setup import fill_run_metadata, load_goal_plan_state
from db import goal_plan_store as gp


@pytest.fixture
def gp_db(tmp_workspace):
    """独立库 + 连接池切换（与 test_goal_plan_store_schema.gp_db 同法）。"""
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "goal-plan-recovery.db"
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


def _seed_active_plan(cid: str = "conv-recover"):
    goal = gp.create_goal(cid, title="清错误", statement="把磁盘错误清干净")
    assert goal is not None
    plan = gp.create_plan_with_steps(cid, goal["goal_id"], [
        {"title": "定位错误源", "detail": "dmesg / journalctl"},
        {"title": "修复并验证"},
    ])
    assert plan is not None
    steps = gp.get_steps(plan["plan_id"])
    step, reason = gp.update_step_status(steps[0]["step_id"], "in_progress")
    assert step is not None, reason
    return goal, plan, steps


def test_assembly_is_idempotent_across_restart(gp_db):
    """崩溃 → 新 run 重新装配：指针与库内真值一致，且连装两次结果相同。"""
    cid = "conv-recover"
    _seed_active_plan(cid)

    # fill_run_metadata 原地写 dict（无返回值）：两次独立装配结果必须一致
    md_first: dict = {}
    fill_run_metadata(md_first, load_goal_plan_state(cid))
    md_second: dict = {}
    fill_run_metadata(md_second, load_goal_plan_state(cid))
    assert md_first == md_second, "同一库态两次装配结果不同 —— 指针装配不可重入"

    plan = gp.get_active_plan(cid)
    assert plan is not None
    assert md_first["_gp_goal_id"] and md_first["_gp_plan_id"] == plan["plan_id"]
    assert md_first["_gp_plan_version"] == plan["version"]
    assert md_first["_gp_active_step_id"]  # 有 in_progress 步骤 → 指针非空
    criteria = json.loads(md_first["_gp_criteria_json"])
    assert isinstance(criteria, list)
    assert md_first["_goal_text"] == "把磁盘错误清干净"
    assert md_first["_goal_self_checked"] is False


def test_snapshot_read_is_idempotent_and_force_plan_holds(gp_db):
    cid = "conv-recover"
    _seed_active_plan(cid)
    s1 = gp.get_snapshot(cid)
    s2 = gp.get_snapshot(cid)
    assert s1 == s2
    # awaiting_approval 属于强制只读面：装配层 force_plan 必须为 True
    state = load_goal_plan_state(cid)
    assert state["force_plan"] is True
    assert "[计划]" in state["injection"] and "[目标]" in state["injection"]


def test_archived_steps_survive_replan(gp_db):
    """用户重置后：归档步骤连同 evidence 可读，重新规划有输入材料。"""
    cid = "conv-archive"
    goal, plan, steps = _seed_active_plan(cid)
    gp.update_step_status(steps[0]["step_id"], "completed", evidence="dmesg 定位到 oom")
    assert gp.supersede_plan(plan["plan_id"]) is True

    archived = gp.get_last_superseded_steps(cid)
    assert len(archived) == 1
    assert archived[0]["title"] == "定位错误源"
    assert archived[0]["evidence"] == "dmesg 定位到 oom"

    # 重置后无活动 plan：装配降级为「计划重置」注入，不强制只读
    state = load_goal_plan_state(cid)
    assert state["force_plan"] is False
    assert "[计划重置]" in state["injection"]
    # 新版本计划可在同一 goal 下重建（version 递增）
    plan2 = gp.create_plan_with_steps(cid, goal["goal_id"], [{"title": "换思路再来"}])
    assert plan2 is not None and plan2["version"] == plan["version"] + 1
