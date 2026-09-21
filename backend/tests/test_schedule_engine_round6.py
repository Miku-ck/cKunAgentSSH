"""ScheduleEngine 稳定性回归（第 6 轮审查）

覆盖两处会让定时功能整体失效的缺陷：
- trigger 的去重标记落在 `await asyncio.to_thread(...)` 之后 —— 轮询 tick 与
  手动「立即执行」同时到达时两边都通过 `sid in self._executing` 检查，
  同一个任务并发跑两个无头 Agent run。
- _run_loop 把整个 while 包在单个 try 里 —— 一次 tick 抛异常（DB 瞬时 locked、
  next_run_at 脏值）就让循环退出，自动调度此后静默停摆。
"""
import asyncio
import time

import database as db
from agent.schedule_engine import ScheduleEngine


def test_trigger_claims_dedupe_before_first_await(monkeypatch):
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()  # 跳过 best-effort 清理的 DB 调用
    started: list[str] = []

    async def fake_run_task(sched):
        started.append(sched["id"])

    def slow_set_next_run(*_args, **_kwargs):
        # 线程池里让出事件循环：原实现在这个 await 期间还没占下去重标记
        time.sleep(0.05)

    monkeypatch.setattr(engine, "_run_task", fake_run_task)
    monkeypatch.setattr(db, "set_schedule_next_run", slow_set_next_run)

    sched = {"id": "s1", "schedule_type": "interval", "interval_minutes": 60}

    async def _scenario():
        return await asyncio.gather(engine.trigger(sched), engine.trigger(sched))

    first, second = asyncio.run(_scenario())
    assert sorted([first, second]) == [False, True], "并发触发应只有一次真正起 run"
    assert started == ["s1"], f"重复触发起了多个 run: {started}"


def test_run_loop_survives_a_failing_tick(monkeypatch):
    """一次 tick 出错只能跳过这一轮，不能终止整个调度循环"""
    engine = ScheduleEngine(poll_seconds=0.01)
    calls: list[int] = []

    async def flaky_tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        if len(calls) >= 3:
            engine._stop_event.set()

    monkeypatch.setattr(engine, "_tick_once", flaky_tick)

    async def _scenario():
        engine.start()
        deadline = time.monotonic() + 2.0
        while len(calls) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await engine.stop()

    asyncio.run(_scenario())
    assert len(calls) >= 3, "首个 tick 异常后循环已退出，自动调度停摆"


# ── 手动触发不吞定时 + remove_schedule 取消在途执行体 ──────────────────────────

def test_trigger_manual_does_not_advance_next_run(monkeypatch):
    """「立即执行」不推进 next_run_at：手动跑不消耗本次定时，到期 occurrence
    由执行结束后的下一个 tick 补跑"""
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()
    writes: list[str] = []

    async def fake_run_task(sched):
        pass

    monkeypatch.setattr(engine, "_run_task", fake_run_task)
    monkeypatch.setattr(db, "set_schedule_next_run",
                        lambda sid, nxt: writes.append(sid))

    sched = {"id": "s1", "schedule_type": "interval", "interval_minutes": 60}

    async def _scenario():
        assert await engine.trigger(sched, manual=True) is True
        assert await engine.trigger({**sched, "id": "s2"}) is True

    asyncio.run(_scenario())
    assert writes == ["s2"], "manual 触发不应写 next_run_at，自动 tick 必须写"


def test_remove_schedule_cancels_running_task_and_deletes(monkeypatch):
    """删除任务必须先取消在途无头 run、落定 running 行，再删 schedules 行"""
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()
    events: list[str] = []

    async def hanging_run_task(sched):
        try:
            await asyncio.sleep(30)
        finally:
            engine._executing.discard(sched["id"])
            engine._running_tasks.pop(sched["id"], None)
            engine._deleted_sids.discard(sched["id"])

    monkeypatch.setattr(engine, "_run_task", hanging_run_task)
    monkeypatch.setattr(db, "set_schedule_next_run", lambda *_: None)
    monkeypatch.setattr(db, "fail_running_schedule_runs",
                        lambda sid, err: events.append(f"fail:{sid}") or 1)
    monkeypatch.setattr(db, "delete_schedule",
                        lambda sid: events.append(f"del:{sid}") or True)

    sched = {"id": "s1", "schedule_type": "interval", "interval_minutes": 60}

    import pytest

    async def _scenario():
        assert await engine.trigger(sched) is True
        t = engine._running_tasks["s1"]
        assert await engine.remove_schedule("s1") is True
        with pytest.raises(asyncio.CancelledError):
            await t
        assert t.cancelled()
        await asyncio.sleep(0)  # 让 done_callback 落地回收

    asyncio.run(_scenario())
    assert events == ["fail:s1", "del:s1"], "先落定 running 行再删（CASCADE 后无从更新）"
    assert engine._executing == set() and engine._running_tasks == {}
    assert engine._deleted_sids == set(), "finally 必须回收删除标记"


def test_remove_schedule_without_running_task_still_purges_rows(monkeypatch):
    """无在途 task 的兜底：库里滞留 running 的 run 行同样被标 failed 后删除"""
    engine = ScheduleEngine(poll_seconds=1)
    calls: list[tuple] = []

    monkeypatch.setattr(db, "fail_running_schedule_runs",
                        lambda sid, err: calls.append((sid, err)) or 0)
    monkeypatch.setattr(db, "delete_schedule", lambda sid: False)

    assert asyncio.run(engine.remove_schedule("nope")) is False
    assert calls == [("nope", "任务已删除，执行被取消")]
