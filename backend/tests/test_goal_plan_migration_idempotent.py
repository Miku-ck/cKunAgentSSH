"""goal_plan 旧工件迁移 — 守卫「一次性、幂等、不自动放行、原文件保留」。

旧 Spec/Plan/Goal markdown 工件（~/.ckunagentic/workflow/{cid}/*.md）迁入
结构化三表时的不变量：
- 会话已有结构化 goal 时跳过（幂等，二次迁移不产生新行）；
- 迁移产物 goal=draft/adjudication=pending、plan=awaiting_approval ——
  必须经用户显式裁决与批准才生效；
- 旧 `.md` 文件永久保留（回退凭据），迁移只读不删；
- plan.md 无可拆编号行时不造空 plan。
"""
from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from db import goal_plan_store as gp


@pytest.fixture
def gp_db(tmp_workspace):
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "goal-plan-migrate.db"
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


@pytest.fixture
def legacy_dir(tmp_workspace):
    """仿 ~/.ckunagentic/workflow 的旧工件目录（含一个会话的三份 .md）。"""
    base = Path(tmp_workspace) / "workflow"
    conv = base / "conv-mig-1"
    conv.mkdir(parents=True)
    (conv / "spec.md").write_text("需求：重启 web 服务并确认健康。\n", encoding="utf-8")
    (conv / "plan.md").write_text(
        "执行计划：\n"
        "1. 检查服务状态\n"
        "2. 重启服务\n"
        "3. 验证健康检查通过\n",
        encoding="utf-8",
    )
    (conv / "goal.md").write_text("服务恢复且健康检查通过。\n", encoding="utf-8")
    return base


def test_migration_creates_pending_goal_and_awaiting_plan(gp_db, legacy_dir):
    plan = gp.migrate_legacy_artifacts("conv-mig-1", base_dir=str(legacy_dir))
    assert plan is not None
    assert plan["status"] == "awaiting_approval"
    assert plan["approved_by"] == ""

    snap = gp.get_snapshot("conv-mig-1")
    goal = snap["goal"]
    assert goal is not None
    assert goal["status"] == "draft"
    assert goal["adjudication"] == "pending"
    assert "服务恢复" in goal["statement"]
    assert goal["criteria"] and goal["criteria"][0]["met"] is False
    # spec.md 并入 context
    assert "重启 web 服务" in goal["context"]

    # plan.md 的编号行拆成 steps
    assert snap["progress"]["total"] == 3
    assert [s["title"] for s in snap["steps"]] == [
        "检查服务状态", "重启服务", "验证健康检查通过"]
    assert all(s["status"] == "pending" for s in snap["steps"])


def test_migration_is_idempotent_and_keeps_files(gp_db, legacy_dir):
    first = gp.migrate_legacy_artifacts("conv-mig-1", base_dir=str(legacy_dir))
    assert first is not None
    second = gp.migrate_legacy_artifacts("conv-mig-1", base_dir=str(legacy_dir))
    assert second is None  # 已有结构化 goal：跳过

    snap = gp.get_snapshot("conv-mig-1")
    assert snap["progress"]["total"] == 3  # 没有翻倍

    # 旧文件永久保留（回退凭据）
    conv = legacy_dir / "conv-mig-1"
    for name in ("spec.md", "plan.md", "goal.md"):
        assert (conv / name).is_file()


def test_migration_noop_without_artifacts_or_conversation(gp_db, legacy_dir):
    assert gp.migrate_legacy_artifacts("", base_dir=str(legacy_dir)) is None
    # 目录存在但该会话无任何工件
    (legacy_dir / "conv-empty").mkdir()
    assert gp.migrate_legacy_artifacts("conv-empty", base_dir=str(legacy_dir)) is None
    # 会话不存在
    assert gp.migrate_legacy_artifacts("conv-missing", base_dir=str(legacy_dir)) is None


def test_migration_without_numbered_plan_lines_skips_plan(gp_db, legacy_dir):
    conv = legacy_dir / "conv-mig-2"
    conv.mkdir()
    (conv / "goal.md").write_text("只写目标不写计划。\n", encoding="utf-8")
    plan = gp.migrate_legacy_artifacts("conv-mig-2", base_dir=str(legacy_dir))
    assert plan is None  # goal 已迁，plan 无可拆步骤不造空 plan
    snap = gp.get_snapshot("conv-mig-2")
    assert snap["goal"] is not None and snap["plan"] is None
