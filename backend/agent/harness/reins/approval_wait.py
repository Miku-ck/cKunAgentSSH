"""审批等待闸 —— 卡片弹出后让 run 真的停住，而不是继续往下跑。

现象（用户直接可观察到的）：说「清理临时日志」，模型发出 `rm -rf /tmp/xxx` 被判
dangerous，界面上弹出授权卡片；**与此同时** run 已经拿着「工具调用被拦截」继续
下一轮 —— 用户还在犹豫要不要点，AI 已经把后面的事做完了，甚至自动重试同一条
命令刷屏。审批卡在物理上存在，在时序上不起作用。

做法：Driver 在拦截后登记等待项并 await；grant / deny-command / release-run
（与 run 运行在同一个事件循环）写入裁决并唤醒。醒来后 Driver **重跑一次
PRE_TOOL_CALL**，由 gate / 拒绝标记自己给出「放行」或「曾被用户拒绝」——
授权状态是唯一权威，Driver 不复制第二套判定。被批准时本次调用直接执行，
不再依赖模型重发同一行命令。

只对交互式 run 生效（ctx.metadata["await_approval"]，由 routes 层置位）：
定时任务没有可点卡片的人，等下去就是挂死。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 裁决取值。字符串而非 enum：这份值只在本进程内流转，不落库、不过网。
ALLOWED = "allowed"
DENIED = "denied"
TIMEOUT = "timeout"
ABORTED = "aborted"


@dataclass
class _Waiter:
    """一个正在等用户点击的调用。

    tool 单独留着：授权端点只知道 tool + command，算出的键偶尔会与实际登记的
    键不一致（bound_command 的推导差异），同 run 同工具就足以认定是同一张卡 ——
    「等不到键就唤醒同工具的等待项」比「一直挂着」安全得多。
    """
    run_id: str
    tool: str
    key: str
    future: asyncio.Future


# (run_id, 授权键) → 等待项。同一 run 同一时刻只有一个调用在等（Driver 串行
# 执行工具调用），这里保留字典形态是为了并发 run 之间互不覆盖。
_WAITERS: dict[tuple[str, str], _Waiter] = {}


async def wait(run_id: str, key: str, tool: str, timeout: float) -> str:
    """登记并等待一次审批裁决。

    timeout > 0 时到期返回 TIMEOUT（调用方落回「审批过期」的老路径）；timeout <= 0
    表示永久等待 —— 与 approval_fallback_seconds 的 0 语义一致，此时只有用户点击
    或 run 被取消（客户端断连 / release-run）才会返回。

    Returns: ALLOWED / DENIED / TIMEOUT / ABORTED
    """
    loop_key = (run_id, key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（同步测试直调）→ 不阻塞，交回原有拦截路径
        return ABORTED
    fut: asyncio.Future = loop.create_future()
    _WAITERS[loop_key] = _Waiter(run_id=run_id, tool=tool, key=key, future=fut)
    try:
        if timeout and timeout > 0:
            try:
                # shield：外层被取消（SSE 断连）不能连带把 fut 取消掉，
                # 否则随后的迟到 grant 会把 set_result 打到已取消的 future 上
                return await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                fut.cancel()
                return TIMEOUT
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # run 被取消：finally 会摘掉等待项，这里不再吞掉 CancelledError
        raise
    finally:
        current = _WAITERS.get(loop_key)
        if current is not None and current.future is fut:
            _WAITERS.pop(loop_key, None)


def _wake(waiter: _Waiter, verdict: str) -> None:
    if not waiter.future.done():
        waiter.future.set_result(verdict)


def resolve(run_id: str, key: str, verdict: str) -> bool:
    """按 (run_id, 授权键) 唤醒等待项。返回是否唤醒了什么。

    没唤醒不算错误：用户可能在权限面板里手工撤销/登记，那一刻根本没有等待中的卡片。
    """
    waiter = _WAITERS.get((run_id, key))
    if waiter is None:
        return False
    _wake(waiter, verdict)
    return True


def resolve_for_call(run_id: str, tool: str, key: str, verdict: str) -> bool:
    """唤醒本次调用对应的等待项：先按授权键精确匹配，退而求其次按工具名。

    精确匹配是常态（两端同用 PermissionGate._grant_key 算键）。退路存在是因为
    「键算得不一样」的后果特别难查：用户点了允许、卡片消失，run 却一直挂着 ——
    比多唤醒一个同工具的等待项（醒来还会重判一次，判不过照样拒绝）糟得多。
    """
    if not run_id:
        return False
    if resolve(run_id, key, verdict):
        return True
    for waiter in list(_WAITERS.values()):
        if waiter.run_id == run_id and waiter.tool == tool:
            _wake(waiter, verdict)
            return True
    return False


def abort_run(run_id: str, verdict: str = ABORTED) -> int:
    """唤醒该 run 名下全部等待项（run 终态 / 用户点了停止 / 客户端断连）。

    不叫醒的后果：driver 被取消时等待项随 finally 摘掉，但 release-run 是
    主动收尾路径 —— run 已经不跑了，等待项还挂着，下一次 grant 会去 set_result
    一个已经没有人在等的 future，日志里只剩一句「已授权」而什么也没发生。
    """
    if not run_id:
        return 0
    n = 0
    for loop_key, waiter in list(_WAITERS.items()):
        if waiter.run_id != run_id:
            continue
        _wake(waiter, verdict)
        _WAITERS.pop(loop_key, None)
        n += 1
    return n


def pending_keys() -> list[tuple[str, str]]:
    """当前在等的 (run_id, 授权键) —— 只给测试与诊断用，生产路径不消费。"""
    return list(_WAITERS.keys())


def reset() -> None:
    """清空等待表（测试隔离用）。"""
    for waiter in list(_WAITERS.values()):
        if not waiter.future.done():
            waiter.future.cancel()
    _WAITERS.clear()


__all__ = [
    "ALLOWED",
    "DENIED",
    "TIMEOUT",
    "ABORTED",
    "wait",
    "resolve",
    "resolve_for_call",
    "abort_run",
    "pending_keys",
    "reset",
]
