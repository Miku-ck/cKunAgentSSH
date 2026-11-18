"""O1：全局并发闸回归 —— 多会话并发不得打爆 LLM 端点。

原状态：routes/agent.py 只有 `_in_flight_runs` 的「同会话单 run」防护，
多个不同会话可同时起任意多个 run，全局无上限。这里钉住：
- 并发上限可配、脏值回默认；
- 抢槽原子（并发调用不会超发）；
- 满时返回 False（调用方回 429，不排队）；
- 释放后槽位可回收（含多释放的下限钳制）。
"""
import asyncio

import routes.agent as agent_routes


def test_global_slot_cap_and_release(monkeypatch):
    monkeypatch.setattr(agent_routes, "_global_run_count", 0)
    monkeypatch.setattr(
        agent_routes, "_agent_run_concurrency_limit", lambda: 2
    )

    acquire = agent_routes._acquire_global_run_slot
    assert asyncio.run(acquire()) is True
    assert asyncio.run(acquire()) is True
    assert asyncio.run(acquire()) is False, "第三个必须被拒"

    agent_routes._release_global_run_slot_sync()
    assert asyncio.run(acquire()) is True, "释放后应可回收"


def test_global_slot_acquire_is_atomic(monkeypatch):
    """同时抢 N+1 次，恰好 N 个成功 —— 防止 check-then-set 竞态超发。"""
    monkeypatch.setattr(agent_routes, "_global_run_count", 0)
    monkeypatch.setattr(
        agent_routes, "_agent_run_concurrency_limit", lambda: 3
    )

    async def grab_many():
        return await asyncio.gather(
            *[agent_routes._acquire_global_run_slot() for _ in range(10)]
        )

    results = asyncio.run(grab_many())
    assert sum(1 for r in results if r) == 3


def test_release_never_goes_negative(monkeypatch):
    monkeypatch.setattr(agent_routes, "_global_run_count", 0)
    agent_routes._release_global_run_slot_sync()
    assert agent_routes._global_run_count == 0


def test_concurrency_limit_config(monkeypatch):
    import database as db

    monkeypatch.setattr(db, "get_app_setting", lambda _k: None)
    assert agent_routes._agent_run_concurrency_limit() == 4

    monkeypatch.setattr(db, "get_app_setting", lambda _k: "8")
    assert agent_routes._agent_run_concurrency_limit() == 8

    monkeypatch.setattr(db, "get_app_setting", lambda _k: "999")
    assert agent_routes._agent_run_concurrency_limit() == 4, "越界回默认"

    monkeypatch.setattr(db, "get_app_setting", lambda _k: "junk")
    assert agent_routes._agent_run_concurrency_limit() == 4, "脏值回默认"
