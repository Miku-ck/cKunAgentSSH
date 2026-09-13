# -*- coding: utf-8 -*-
"""可见终端执行模式 — 待交付注册表 + 确认规则单测。

覆盖：
1. PendingDeliveries.create / resume / wait 的完整生命周期（async）。
2. resume 未知 tool_id 返回 False（对应路由 404）。
3. 用户拒绝（reject）语义。
4. _compute_requires_confirm：prod 环境或 sudo 等非 safe 命令 → 需确认；
   安全命令（ls）在 test 环境 → 无需确认。

运行: python -m pytest backend/tests/test_visible_delivery.py -v
"""
import asyncio

from agent.pending_delivery import PendingDeliveries
from agent.tools.builtin.shell_exec import _compute_requires_confirm


def test_create_resume_wait_lifecycle():
    """create → wait 挂起 → resume 唤醒 → 读回结果"""
    async def _scenario():
        d = PendingDeliveries.create(
            command="echo hi", session_id="s1",
            requires_confirm=False, run_id="r1", timeout=300,
        )
        assert PendingDeliveries.get(d.tool_id) is not None
        assert PendingDeliveries.timeout_of(d.tool_id) == 300

        task = asyncio.create_task(PendingDeliveries.wait(d.tool_id))
        await asyncio.sleep(0.01)
        assert not task.done(), "resume 前 wait 不应返回"

        ok = PendingDeliveries.resume(
            d.tool_id, output="hello", success=True, exit_code=0,
        )
        assert ok
        await asyncio.wait_for(task, timeout=1.0)

        reg = PendingDeliveries.get(d.tool_id)
        assert reg.output == "hello"
        assert reg.success is True
        assert reg.exit_code == 0
        assert reg.rejected is False
        PendingDeliveries.remove(d.tool_id)

    asyncio.run(_scenario())


def test_resume_unknown_tool_id_returns_false():
    """未知 tool_id → resume 返回 False（路由侧据此 404）"""
    assert PendingDeliveries.resume("does-not-exist", output="x") is False
    assert PendingDeliveries.reject("does-not-exist") is False


def test_reject():
    """reject → 唤醒等待方并标记 rejected"""
    async def _scenario():
        d = PendingDeliveries.create(
            command="rm -rf tmp", session_id="s2",
            requires_confirm=True, run_id="r2",
        )
        task = asyncio.create_task(PendingDeliveries.wait(d.tool_id))
        await asyncio.sleep(0.01)
        assert PendingDeliveries.reject(d.tool_id)
        await asyncio.wait_for(task, timeout=1.0)
        reg = PendingDeliveries.get(d.tool_id)
        assert reg.rejected is True
        PendingDeliveries.remove(d.tool_id)

    asyncio.run(_scenario())


def test_clear_run_removes_pending():
    """run 中止/结束时 clear_run 清除该 run 的所有待交付项，无悬挂"""
    a = PendingDeliveries.create(
        command="echo a", session_id="sA", requires_confirm=False, run_id="r_abort",
    )
    b = PendingDeliveries.create(
        command="echo b", session_id="sB", requires_confirm=True, run_id="r_abort",
    )
    # 其他 run 的待交付项不受影响
    c = PendingDeliveries.create(
        command="echo c", session_id="sC", requires_confirm=False, run_id="r_other",
    )
    PendingDeliveries.clear_run("r_abort")
    assert PendingDeliveries.get(a.tool_id) is None
    assert PendingDeliveries.get(b.tool_id) is None
    assert PendingDeliveries.get(c.tool_id) is not None
    PendingDeliveries.clear_run("r_other")


def test_require_confirm_logic():
    """确认规则：非 safe（sudo/dangerous）或 prod 环境需确认；safe 命令在 test 免确认"""
    # sudo → dangerous → 需确认
    assert _compute_requires_confirm("sudo apt update", "test") is True
    # 安全只读命令在 test 环境 → 免确认
    assert _compute_requires_confirm("ls -la", "test") is False
    # 安全命令在 prod 环境 → 需确认
    assert _compute_requires_confirm("ls -la", "prod") is True