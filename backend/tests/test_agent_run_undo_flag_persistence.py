"""agent_runs.undo_recorded 列的落库与列表投影回归（功能 B 消费侧闭环）。

守护点：
- update_agent_run 显式传 undo_recorded 才写（``is not None`` 语义，与 C6/R2
  修复口径一致：status-only 调用不得把已写值冲掉）；
- 0 与 1 都如实落库 —— 0 是「快照失败/无副作用」的合法值，不是「跳过不写」；
- get_agent_runs 默认投影列含 undo_recorded（运行列表「回滚」标记的数据源），
  与 _RUN_LIST_COLUMNS 漂移即红。

运行: python -m pytest backend/tests/test_agent_run_undo_flag_persistence.py -v
"""
import os

import pytest

import database as db


def _ensure_conv() -> str:
    """建一条会话并返回其真实 id（agent_runs.conversation_id 挂 FK，父行必须先存在）"""
    return str(db.create_conversation(title="t", mode="ssh")["id"])


@pytest.fixture
def run_db(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "undo-flag.db"))
    yield
    db._close_all_connections()


def _make_run() -> str:
    run_id = "run-undo-flag-1"
    db.create_agent_run(run_id, _ensure_conv(), "任务")
    return run_id


def test_undo_recorded_roundtrip_and_list_projection(run_db):
    run_id = _make_run()
    db.update_agent_run(run_id, "running")
    db.update_agent_run(run_id, "completed", undo_recorded=1)
    rows = db.get_agent_runs(run_id=run_id)
    assert rows and rows[0]["undo_recorded"] == 1


def test_undo_recorded_zero_is_written_not_skipped(run_db):
    run_id = _make_run()
    db.update_agent_run(run_id, "completed", undo_recorded=1)
    db.update_agent_run(run_id, "completed", undo_recorded=0)
    rows = db.get_agent_runs(run_id=run_id)
    assert rows[0]["undo_recorded"] == 0


def test_status_only_update_does_not_clobber_undo_flag(run_db):
    run_id = _make_run()
    db.update_agent_run(run_id, "completed", undo_recorded=1)
    db.update_agent_run(run_id, "failed")
    rows = db.get_agent_runs(run_id=run_id)
    assert rows[0]["undo_recorded"] == 1
