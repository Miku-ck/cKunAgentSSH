# -*- coding: utf-8 -*-
"""S1-03：/agent/tool-result 的 run 归属与 requires_confirm 回查。

守护点：
- 认领对了但越权（回传方自称另一个 run）⇒ 403，挂起的那条交付不被闭合；
- requires_confirm 的交付未经确认 ⇒ 403，且**不 set event**（不能把「没发生」写成成功）；
- 声明了确认 ⇒ 200，confirmed_by_user 落在交付项上供 driver/审计读取；
- 无需确认的交付照旧闭合成 200（不新增无谓门槛）；
- 「拒绝」这条腿永远放行 —— 把 403 加在拒绝上等于罚用户点不了拒绝；
- 不声明 run_id 时按旧口径只认 tool_id（前端未升级的失败上报路径不该被拦）。
"""
import asyncio

import pytest
from fastapi import HTTPException

import routes.agent as ra
from agent.pending_delivery import PendingDelivery, PendingDeliveryManager


@pytest.fixture
def registry(monkeypatch):
    mgr = PendingDeliveryManager()
    monkeypatch.setattr(ra, "PendingDeliveries", mgr)
    return mgr


def _add(registry, tool_id="t-1", run_id="run-A", requires_confirm=False):
    registry._registry[tool_id] = PendingDelivery(
        tool_id=tool_id,
        command="rm -rf /srv/cache",
        session_id="ssh-1",
        requires_confirm=requires_confirm,
        run_id=run_id,
    )
    return registry._registry[tool_id]


def _post(req_kwargs):
    return asyncio.run(ra.agent_tool_result(ra.ToolResultRequest(**req_kwargs)))


def test_unknown_tool_id_is_404(registry):
    with pytest.raises(HTTPException) as ei:
        _post({"tool_id": "nope"})
    assert ei.value.status_code == 404


def test_cross_run_claim_is_rejected(registry):
    d = _add(registry, run_id="run-A")
    with pytest.raises(HTTPException) as ei:
        _post({"tool_id": "t-1", "run_id": "run-B", "output": "x", "success": True})
    assert ei.value.status_code == 403
    # 越权的回传不得闭合挂起循环：交付原样待着，由本 run 的超时/收尾处理
    assert d.event.is_set() is False
    assert d.success is False


def test_matching_run_id_closes_delivery(registry):
    _add(registry, run_id="run-A")
    r = _post({"tool_id": "t-1", "run_id": "run-A", "output": "ok", "success": True})
    assert r["status"] == "ok"


def test_unconfirmed_requires_confirm_cannot_close(registry):
    """这条就是 S1-03 的正身：服务端原先分不清「没确认」与「确认并执行」。"""
    d = _add(registry, requires_confirm=True)
    with pytest.raises(HTTPException) as ei:
        _post({"tool_id": "t-1", "run_id": "run-A", "output": "did it", "success": True})
    assert ei.value.status_code == 403
    assert d.event.is_set() is False
    assert d.success is False
    assert d.confirmed_by_user is False


def test_confirmed_claim_is_recorded_on_delivery(registry):
    d = _add(registry, requires_confirm=True)
    _post({"tool_id": "t-1", "run_id": "run-A", "output": "did it", "success": True,
           "confirmed": True})
    assert d.confirmed_by_user is True
    assert d.event.is_set() is True


def test_no_confirm_needed_path_unchanged(registry):
    d = _add(registry, requires_confirm=False)
    _post({"tool_id": "t-1", "output": "ls ok", "success": True, "exit_code": 0})
    assert d.success is True and d.event.is_set() is True


def test_rejection_always_closes_even_without_confirmation(registry):
    d = _add(registry, requires_confirm=True)
    _post({"tool_id": "t-1", "run_id": "run-A", "rejected": True})
    assert d.rejected is True
    assert d.event.is_set() is True


def test_resume_marks_confirmed_and_clear_run_still_wakes(registry):
    d = _add(registry, requires_confirm=True)
    assert registry.resume("t-1", output="x", success=True, confirmed=True) is True
    assert d.confirmed_by_user is True
    registry.clear_run("run-A")
    assert registry.get("t-1") is None
