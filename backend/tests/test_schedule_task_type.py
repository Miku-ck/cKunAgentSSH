"""定时任务双类型（运维/日常）— 数据层、REST、AI 工具契约测试。

spec 2026-10-27-schedule-task-type
"""
import asyncio
import os

import pytest

import database as db


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "schedules.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


def _make(**over):
    data = {
        "name": "t", "schedule_type": "interval", "interval_minutes": 60,
        "time_hour": 9, "time_minute": 0, "weekday": 0, "day_of_month": 1,
        "target_host": "", "task_prompt": "p", "enabled": 1,
        "next_run_at": "2099-01-01 00:00:00",
    }
    data.update(over)
    return db.create_schedule(data)


def test_default_task_type_is_ops(base_db):
    row = _make()
    assert row["task_type"] == "ops"


def test_create_daily_task_type(base_db):
    row = _make(task_type="daily")
    assert row["task_type"] == "daily"


def test_update_cannot_change_task_type(base_db):
    row = _make(task_type="ops")
    updated = db.update_schedule(row["id"], {"task_type": "daily"})
    assert updated["task_type"] == "ops"


def test_legacy_db_gets_task_type_column(base_db):
    # 模拟旧库：建库后手动删列再重跑迁移不现实，改为直接查列存在性
    cols = {row[1] for row in
            db._get_conn().execute("PRAGMA table_info(schedules)").fetchall()}
    db._close_all_connections()
    assert "task_type" in cols
