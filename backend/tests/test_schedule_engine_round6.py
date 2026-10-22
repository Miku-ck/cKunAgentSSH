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


def test_maybe_prune_registers_strong_task_ref_and_throttles(monkeypatch):
    """回归锁：_maybe_prune_runs 的 create_task 结果必须登记进 _bg_tasks——事件循环
    只持弱引用，裸 task 可能在跑完前被 GC 回收（CPython 文档警告），best-effort 清理
    会静默不执行。同时锁住 6h 节流与 done-callback 自动摘除。"""
    engine = ScheduleEngine()
    engine._last_prune_at = time.monotonic() - 6 * 3600 - 10  # 远在节流窗口外 → 本次应真正调度
    ran: list[int] = []
    monkeypatch.setattr(db, "prune_schedule_runs", lambda *a, **k: ran.append(1) or 0)

    async def scenario():
        engine._maybe_prune_runs()
        assert len(engine._bg_tasks) == 1, "prune task 未登记强引用（旧实现此处为空）"
        engine._maybe_prune_runs()  # 刚刷新 _last_prune_at → 节流跳过
        assert len(engine._bg_tasks) == 1, "节流失效：重复排了第二个 prune task"
        for _ in range(100):  # 等 to_thread 完成 + done-callback 摘除
            await asyncio.sleep(0.01)
            if not engine._bg_tasks:
                break
        return set(engine._bg_tasks)

    leftover = asyncio.run(scenario())
    assert not leftover, "任务完成后未从 _bg_tasks 摘除"
    assert ran, "prune 未实际执行"


# ── S7-01 / S7-02 / S7-03 / S7-04（纵向切片战役 S7 片四条 Major）──

def test_run_task_按_done_终态定成败_而非正文是否非空(monkeypatch):
    """被熔断 / 撞迭代上限的 run 不能再记 completed。

    driver 的四条终止路径都只发 warning + done、不发 error，且 MAX_REACHED 在正文
    为空时会用工具日志合成一份摘要当 content。旧判据 `not snippet and err` 因此
    把腰斩的巡检记成一次正常完成，执行历史里躺着一份看起来完全正常的报告。
    """
    engine = ScheduleEngine(poll_seconds=1)
    updates: list[dict] = []
    marks: list[tuple[str, str]] = []

    async def fake_agent_once(_sched, _run_id):
        return "## 巡检报告（被迭代上限截断）", "", "max_reached", ""

    monkeypatch.setattr(engine, "_run_agent_once", fake_agent_once)
    monkeypatch.setattr(db, "create_schedule_run", lambda *a, **k: None)
    monkeypatch.setattr(
        db, "mark_schedule_run",
        lambda sid, st, **k: marks.append((sid, st)))
    monkeypatch.setattr(
        db, "update_schedule_run", lambda rid, **kw: updates.append(kw))

    asyncio.run(engine._run_task({"id": "s1", "task_prompt": "巡检"}))

    assert updates, "run 结果未落库"
    assert updates[-1]["status"] == "failed", "非 completed 终态被记成 completed"
    assert "max_reached" in updates[-1]["error"]
    assert ("s1", "failed") in marks, "schedules.last_status 未同步为 failed"


def test_trigger_未推进_next_run_时不起跑(monkeypatch):
    """S7-03：next_run 没写成就照常起跑 = 每个 tick 重复一次无头 run。

    行仍是到期态 ⇒ 下个 tick 依旧 due ⇒ 无限循环，每轮一次 Agent run + SSH 会话 +
    模型计费，且全仓没有任何失败计数或退避。触发条件是 DB locked 或存量脏行
    （compute_next_run 对 time_hour=25 抛 ValueError），两种都不需要先做什么坏事。
    """
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()
    started: list[str] = []

    async def fake_run_task(sched):
        started.append(sched["id"])

    def locked(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(engine, "_run_task", fake_run_task)
    monkeypatch.setattr(db, "set_schedule_next_run", locked)

    sched = {"id": "s1", "schedule_type": "interval", "interval_minutes": 60}
    ok = asyncio.run(engine.trigger(sched))

    assert ok is False
    assert started == [], "推进失败仍起跑：无人值守地对目标机反复执行远程命令"
    assert "s1" not in engine._executing, "跳过起跑时必须释放去重标记"


def test_system_prompt_未指定主机时告知没有会话():
    """S7-02：任务书让模型去巡检，却没告诉它这次没有可巡检的对象。"""
    from agent.schedule_engine import _build_schedule_system_prompt

    without = _build_schedule_system_prompt({"target_host": ""})
    assert "没有可用的 SSH 会话" in without
    assert "没有可用的 SSH 会话" not in _build_schedule_system_prompt(
        {"target_host": "web-01"})


def test_reap_stale_runs_启动时收尸遗留_running(monkeypatch):
    """S7-04：running 的唯一出口是那个已经随进程死掉的 task。

    后端被杀/崩溃重启后没人回收，last_status 停在 running，前端据此把「编辑」
    「立即执行」置灰，用户只能删了重建；自愈要等下次到期（daily ≈ 最长 24 小时）。
    """
    engine = ScheduleEngine(poll_seconds=1)
    reaped: list[str] = []
    marks: list[tuple[str, str]] = []

    monkeypatch.setattr(db, "list_schedules", lambda: [
        {"id": "s1", "last_status": "running"},
        {"id": "s2", "last_status": "completed"},
    ])
    monkeypatch.setattr(
        db, "fail_running_schedule_runs",
        lambda sid, _err: reaped.append(sid) or 1)
    monkeypatch.setattr(
        db, "mark_schedule_run", lambda sid, st, **k: marks.append((sid, st)))

    asyncio.run(engine._reap_stale_runs())

    assert reaped == ["s1", "s2"]
    assert marks == [("s1", "failed")], "只收 run 行会漏掉 last_status，按钮仍被置灰"


def test_first_tick_fanout_is_capped_globally(monkeypatch):
    """S7-05：开机首 tick 把 N 个到期任务一次性扇出时，同时在跑的无头 run 有上限。

    _executing 只是 per-schedule 去重，原先没有任何全局闸门：一次 tick 能拉起
    N 条 connect_ssh + N 份模型调用 + N 份熔断预算，叠上 ssh-manager 的
    SESSION_LIMIT=64，连用户自己的终端都连不上。
    """
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()
    peak = 0
    live = 0

    async def fake_run_agent_once(sched, run_id):
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return "ok", "", "completed", ""

    monkeypatch.setattr(engine, "_run_agent_once", fake_run_agent_once)
    for fn in ("create_schedule_run", "mark_schedule_run", "update_schedule_run"):
        monkeypatch.setattr(db, fn, lambda *_a, **_k: None)

    async def _scenario():
        await asyncio.gather(*[
            engine._run_task({"id": f"s{i}", "schedule_type": "interval"})
            for i in range(12)
        ])

    asyncio.run(_scenario())
    assert peak <= 4, f"全局并发闸门没生效：同时跑了 {peak} 个无头 run"
    assert peak >= 2, "闸门是限流不是串行化，一次一个会把补跑拖成分钟级"
    assert not engine._executing, f"run 结束后去重标记没释放: {engine._executing}"


def test_task_cancelled_while_waiting_its_slot_is_not_stuck(monkeypatch):
    """排队期间被删除/关停取消的任务，不能永久占着「执行中」。

    _run_task_inner 的 finally 才是回收 _executing 的地方，而它压根没开始跑 ——
    那一支必须 _run_task 自己补，否则这个 schedule 到进程重启前再也不会触发。
    """
    engine = ScheduleEngine(poll_seconds=1)
    engine._last_prune_at = time.monotonic()
    hold = asyncio.Event()

    async def fake_run_agent_once(sched, run_id):
        if str(sched["id"]).startswith("holder"):
            await hold.wait()
        return "ok", "", "completed", ""

    monkeypatch.setattr(engine, "_run_agent_once", fake_run_agent_once)
    for fn in ("create_schedule_run", "mark_schedule_run", "update_schedule_run"):
        monkeypatch.setattr(db, fn, lambda *_a, **_k: None)

    async def _scenario():
        holders = [asyncio.create_task(engine._run_task({"id": f"holder-{i}"}))
                   for i in range(4)]
        await asyncio.sleep(0.05)  # 四个槽位全部占住
        queued_id = "queued"
        engine._executing.add(queued_id)  # 模拟 trigger 已打的去重标记
        queued = asyncio.create_task(engine._run_task({"id": queued_id}))
        await asyncio.sleep(0.02)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        hold.set()
        await asyncio.gather(*holders, return_exceptions=True)

    asyncio.run(_scenario())
    assert "queued" not in engine._executing, "排队中被取消的任务把「执行中」留住了"
