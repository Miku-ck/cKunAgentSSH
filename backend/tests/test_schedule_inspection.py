"""定时巡检判读闭环单测（方案 Phase 3）。

覆盖：标记行解析（含中英文冒号 / 多次出现取最后）、正常结论零痕迹、
异常沉淀（kind='inspection_report'，title 含主机与时间，last_abnormal_at 置位）、
缺标记 fail-open 按正常、巡检任务识别启发式、system prompt 标记段注入。
"""
import asyncio
from pathlib import Path

import pytest

import database as db
from agent.memory import mstore
from agent.schedule_engine import (
    ScheduleEngine,
    _build_schedule_system_prompt,
    _extract_inspect_verdict,
    _is_inspection_task,
)


@pytest.fixture
def memdb():
    """与 test_memory_v2 共用同一个库文件 + 每测试清表（线程本地连接不能换路径）。"""
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    path = base / "memv2-test.db"
    original = db._db_path
    # 全量回归时前面的模块可能把线程绑定到别的库：close-all 让所有线程
    # 在下一次 _get_conn 时按当前 _db_path 重绑（自定义路径测试的既有约定）
    db._close_all_connections()
    db.init_db(str(path))
    conn = db._get_conn()
    for t in ("memories", "memory_jobs", "memory_audit", "schedules"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    db._close_all_connections()
    if original:
        db.init_db(original)


def _sched(**kw) -> dict:
    data = {"name": "每日巡检", "schedule_type": "interval",
            "interval_minutes": 60, "target_host": "host-a", "task_prompt": ""}
    data.update(kw)
    return db.create_schedule(data)


def _report(verdict: str | None = "正常", summary: str = "") -> str:
    body = "## 总体结论\n系统运行平稳。\n## 发现\n无。"
    if verdict:
        suffix = f"[巡检结论] {verdict}" + (f": {summary}" if summary else "")
        return f"{body}\n{suffix}"
    return body


# ── 标记行解析 ──


def test_extract_verdict_variants():
    assert _extract_inspect_verdict("报告\n[巡检结论] 正常") == ("正常", "")
    assert _extract_inspect_verdict("报告\n[巡检结论] 异常: 磁盘 92%") == ("异常", "磁盘 92%")
    assert _extract_inspect_verdict("报告\n[巡检结论] 异常：内存告警") == ("异常", "内存告警")
    # 多次出现取最后一个（模型重复输出时以收尾结论为准）
    twice = "[巡检结论] 正常\n中间内容\n[巡检结论] 异常: 端口不通"
    assert _extract_inspect_verdict(twice) == ("异常", "端口不通")
    assert _extract_inspect_verdict("没有任何标记的旧格式报告") is None
    assert _extract_inspect_verdict("") is None


def test_normal_verdict_leaves_no_trace(memdb):
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _report("正常")))
    assert mstore.list_memories(kind="inspection_report", limit=10) == []
    assert db.get_schedule(s["id"])["last_abnormal_at"] is None


def test_no_marker_fails_open(memdb):
    """缺标记按正常处理（fail-open）：旧任务格式兼容，不报错不沉淀。"""
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _report(None)))
    assert mstore.list_memories(kind="inspection_report", limit=10) == []
    assert db.get_schedule(s["id"])["last_abnormal_at"] is None


def test_abnormal_verdict_persists_report(memdb):
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(
        s, _report("异常", "根分区使用率 92%，超过阈值")))
    rows = mstore.list_memories(kind="inspection_report", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "inspection_report"
    assert row["scope"] == "global"        # 会话 ID 跨进程回收，持久知识落 global
    assert row["source"] == "schedule"
    assert "host-a" in row["title"] and "巡检异常" in row["title"]
    assert "根分区使用率 92%" in row["content"]   # 摘要进正文
    assert "总体结论" in row["content"]           # 完整报告进正文
    assert db.get_schedule(s["id"])["last_abnormal_at"]


def test_abnormal_without_summary_falls_back(memdb):
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(s, _report("异常")))
    row = mstore.list_memories(kind="inspection_report", limit=10)[0]
    assert "见报告正文" in row["content"]


def test_report_content_capped(memdb):
    s = _sched()
    asyncio.run(ScheduleEngine._handle_inspection_verdict(
        s, _report("异常") + "长" * 9000))
    row = mstore.list_memories(kind="inspection_report", limit=10)[0]
    assert len(row["content"]) < 4500


# ── 巡检任务识别与提示词 ──


def test_inspection_task_heuristic():
    assert _is_inspection_task({"task_prompt": ""})                # 空 = 默认例行检查
    assert _is_inspection_task({"task_prompt": "巡检一下磁盘"})
    assert _is_inspection_task({"task_prompt": "每周健康检查"})
    assert not _is_inspection_task({"task_prompt": "每周五备份 /etc 目录"})


def test_prompt_injects_verdict_hint_for_inspection():
    sched = {"name": "巡检", "target_host": "h", "task_prompt": ""}
    p = _build_schedule_system_prompt(sched)
    assert "[巡检结论]" in p
    # 非巡检任务不注入
    p2 = _build_schedule_system_prompt(
        {"name": "备份", "target_host": "h", "task_prompt": "每周五备份 /etc"})
    assert "[巡检结论]" not in p2
