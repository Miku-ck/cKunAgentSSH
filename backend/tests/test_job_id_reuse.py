"""后台任务 id 复用（重启后撞号）+ 整型设置脏行读取。

两处都是「一次脏状态把功能永久埋掉」的形态：
  - JobManager._seq 是进程内计数，重启归零；_ensure_restored 会把 DB 里未终结的
    job-1…job-N 拉回内存。job_run 若先取号后恢复（原先根本没恢复），首发号必然
    撞上仍在跑的任务：mgr.add 覆盖条目、save_agent_job 的 upsert 覆盖 DB 行、
    远程 log_dir 同目录 —— 原任务的输出与 kill 通道一起丢。
  - memory.max_items / conv_retention.keep_count 由 routes 的 Field 区间管住写入，
    但升级前的旧行与直接改库不受管；读取侧裸 int() 会让 GET /settings/memory 500，
    用户连改回来的入口都没有。
"""
import types

import pytest

import database as db
from agent.tools.builtin import ops_job
from agent.tools.builtin.ops_job import JobEntry, JobManager, JobRunTool


def _entry(job_id: str) -> JobEntry:
    return JobEntry(job_id=job_id, args=["sleep 999"], start_ts=0.0, remote=True)


def _ctx(ssh_session_id=""):
    return types.SimpleNamespace(ssh_session_id=ssh_session_id, agent_run_id="run-1")


# ── job id 不复用 ───────────────────────────────────────────
def test_next_id_skips_ids_taken_by_restored_jobs():
    mgr = JobManager()
    mgr.add(_entry("1"))
    mgr.add(_entry("2"))
    # 旧实现：_seq 从 0 起 → 返回 "1"，直接盖掉仍在跑的 job-1
    assert mgr._next_id() == "3"
    assert mgr.get("1").args == ["sleep 999"]


def test_next_id_keeps_monotonic_after_gap():
    mgr = JobManager()
    mgr.add(_entry("1"))
    assert mgr._next_id() == "2"
    assert mgr._next_id() == "3"


@pytest.mark.asyncio
async def test_job_run_restores_persisted_jobs_before_taking_id(monkeypatch):
    """job_run 的前置与 job_list/job_output/job_kill 一致：先恢复再取号。"""
    calls: list[str] = []
    monkeypatch.setattr(JobManager, "_ensure_restored", lambda self: calls.append("restored"))
    # 本地分支会起真子进程：让参数解析失败，走到恢复/取号之后的第一个早返回
    monkeypatch.setattr(ops_job, "_split_cmd", lambda cmd: [])
    result = await JobRunTool().execute(_ctx(), cmd="sleep 1")
    assert result.success is False and "无法解析" in result.error
    assert calls == ["restored"]


@pytest.mark.asyncio
async def test_remote_launch_takes_a_fresh_id_not_the_running_one(monkeypatch):
    """撞号修好后，新任务拿到新号：在跑的那条 DB 行不被 upsert 覆盖、log_dir 不复用。"""
    mgr = JobManager()
    mgr.add(_entry("1"))
    monkeypatch.setattr(JobManager, "instance", classmethod(lambda cls: mgr))
    monkeypatch.setattr(JobManager, "_ensure_restored", lambda self: None)
    saved: list[JobEntry] = []
    launched: list[str] = []

    async def fake_launch(context, job_id, cmd):
        launched.append(job_id)
        return True, 4242, ""

    monkeypatch.setattr(ops_job, "_remote_launch", fake_launch)
    monkeypatch.setattr(ops_job, "_persist_save", lambda entry: saved.append(entry))

    result = await JobRunTool().execute(_ctx("sess-1"), cmd="sleep 1", target="remote")
    assert result.success is True, result.error
    assert launched == ["2"]
    assert [e.job_id for e in saved] == ["2"]
    assert saved[0].log_dir == f"{ops_job._JOBS_ROOT}/2"
    assert mgr.get("1").status == "running"  # 原任务条目未被动过


# ── 整型设置读取：脏行不炸接口 ──────────────────────────────
def _stub_settings(monkeypatch, values: dict):
    monkeypatch.setattr(
        db, "get_app_setting",
        lambda key, default="": values.get(key, default if default is not None else ""),
    )


def test_int_app_setting_falls_back_on_dirty_rows(monkeypatch):
    _stub_settings(monkeypatch, {
        "memory.max_items": "很多",
        "conv_retention.keep_count": "",
        "chat.context_window": "20",
    })
    assert db._int_app_setting("memory.max_items", 100) == 100
    assert db._int_app_setting("conv_retention.keep_count", 1000) == 1000
    assert db._int_app_setting("chat.context_window", 20) == 20
    assert db._int_app_setting("missing.key", 7) == 7


def test_memory_and_retention_reads_survive_dirty_rows(monkeypatch):
    """校验收紧前落库的越界值，不能让 GET /settings/* 变成 500。"""
    _stub_settings(monkeypatch, {
        "memory.max_items": "0",
        "conv_retention.keep_count": "abc",
        "conv_retention.enabled": "true",
    })
    # max_items 越界回默认而非原样读出：0 会让记忆清理删空整个 mode
    # （见 test_memory_max_items.py）
    assert db.get_memory_settings()["max_items"] == 100
    assert db.get_conv_retention() == {"enabled": True, "keep_count": 1000}
