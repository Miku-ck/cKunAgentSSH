"""[TRACE] 日志节流守卫（P1-2）。

钉两条不变量：
1. 关键事件（error/warning/human_help_needed/permission_needed）直通，永不节流 ——
   排查现场最需要的信号漏一条都可能误判；
2. 常规事件（content 等逐 chunk 事件）按 (run_id, kind) 时间窗节流，放行时带
   suppressed=N 报出被吞的条数，不静默丢总量。

实现是 harness.run() 内的就地闭包，测试用「启动 run 任务到订阅者注册点 →
取出订阅者直接调用 → 取消任务」的方式触达真实闭包，不复制实现。
"""
import asyncio
import logging

import pytest

from agent.harness.contracts import TraceEvent
from agent.harness.harness import Harness, HarnessConfig, _TRACE_LOG_WINDOW_S


def _make_registered_harness(monkeypatch, clock):
    """构造 Harness 并让 run() 的 async generator 执行到订阅者注册点。

    run() 是 async generator，body 在首次 __anext__() 时才跑；用 create_task 包
    __anext__() 驱动它越过注册点（L319），拿到 subscriber 后 cancel 掉这个驱动。
    clock 是 time.monotonic 的替身（列表 [值]），monkeypatch 到 harness 模块。
    """
    import agent.harness.harness as harness_mod

    # 只替换 monotonic，保留 time 模块其它属性（run() 后续还会用到 perf_counter 等）。
    monkeypatch.setattr(harness_mod.time, "monotonic", lambda: clock[0])

    h = Harness(HarnessConfig())
    h._assembled = True  # 绕过 assemble：本测试只验日志订阅者
    agen = h.run("probe")
    driver = asyncio.create_task(agen.__anext__())
    return h, agen, driver


@pytest.mark.asyncio
async def test_critical_events_never_throttled(monkeypatch, caplog):
    """error/warning 等关键事件逐条 info 落地，不受时间窗影响。"""
    clock = [1000.0]
    h, agen, driver = _make_registered_harness(monkeypatch, clock)
    try:
        for _ in range(20):
            await asyncio.sleep(0)  # 让 run() 推进到订阅者注册（L319）
            if h._log_subscriber_registered:
                break
        assert h._log_subscriber_registered, "订阅者未注册，无法测试节流"

        subscriber = h.telemetry._subscribers[0]
        with caplog.at_level(logging.INFO, logger="agent.harness.harness"):
            for i in range(3):
                subscriber(TraceEvent(kind="error", source="test", run_id="r1",
                                      payload={"i": i}))
                clock[0] += 0.01  # 远小于窗口，仍要全部记录
        infos = [r for r in caplog.records
                 if r.levelno >= logging.INFO and "[TRACE]" in r.getMessage()]
        assert len(infos) == 3, "关键事件被错误节流"
    finally:
        driver.cancel()
        await agen.aclose()


@pytest.mark.asyncio
async def test_regular_events_throttled_with_suppressed_count(monkeypatch, caplog):
    """content 常规事件在窗口内只记首条，窗口外放行时带 suppressed=N。"""
    clock = [2000.0]
    h, agen, driver = _make_registered_harness(monkeypatch, clock)
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if h._log_subscriber_registered:
                break
        assert h._log_subscriber_registered
        subscriber = h.telemetry._subscribers[0]

        with caplog.at_level(logging.DEBUG, logger="agent.harness.harness"):
            # 窗口内连发 100 条 → 只有首条 debug，其余累计 suppressed
            for _ in range(100):
                subscriber(TraceEvent(kind="content", source="test", run_id="r2"))
            # 越过窗口后再发 1 条（同一 run）→ 放行，且 suppressed=99
            clock[0] += _TRACE_LOG_WINDOW_S + 0.01
            subscriber(TraceEvent(kind="content", source="test", run_id="r2"))

        debugs = [r for r in caplog.records
                  if "[TRACE]" in r.getMessage() and "kind=content" in r.getMessage()]
        assert len(debugs) == 2, f"节流后应只剩 2 条 content 日志，实际 {len(debugs)}"
        assert "suppressed=0" in debugs[0].getMessage()
        assert "suppressed=99" in debugs[1].getMessage()
    finally:
        driver.cancel()
        await agen.aclose()
