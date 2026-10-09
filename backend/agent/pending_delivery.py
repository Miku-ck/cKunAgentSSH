"""
可见终端执行模式 — 待交付命令内存注册表

Agent 在"可见终端"模式下，不直接 headless 执行命令，而是把命令"交付"
给前端（由前端写入真实 PTY/SSH 会话），挂起 ReAct 循环等待前端回传结果。

本模块是 asyncio 事件循环安全的内存注册表（dict + threading.Lock）：
- 每个待交付项持有 tool_id / command / session_id / requires_confirm / run_id，
  一个 asyncio.Event，以及结果字段（output/success/exit_code/rejected）。
- create() 时生成 uuid tool_id 并注册；resume()/reject() 填好结果后 set 事件，
  挂起的 Driver 通过 wait() 醒来读取结果。
- 无需持久化；run 结束/中止时用 clear_run() 清理该 run 的所有待交付项。

线程安全级别：单 asyncio 事件循环内使用即可满足（FastAPI 单循环）。
锁只保护 dict 的读写；Event 一律在循环线程内 set。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 等待前端回传的默认秒数 —— create() 的默认值与 timeout_of() 的兜底共用，
# 原先写成两处字面量，改一处会让两处口径分叉
DEFAULT_DELIVERY_TIMEOUT = 300


@dataclass
class PendingDelivery:
    """单次待交付命令"""
    tool_id: str                    # 前端回传时定位用（uuid）
    command: str
    session_id: str
    requires_confirm: bool
    run_id: str
    timeout: int = DEFAULT_DELIVERY_TIMEOUT
    # 前端回传结果
    output: str = ""
    success: bool = False
    exit_code: Optional[int] = None
    rejected: bool = False          # 用户拒绝执行
    # requires_confirm 的交付被用户显式确认后才允许闭合成 success（S1-03）。
    # 原先 resume() 什么都不回查，服务端分不出「用户从未确认」与「确认并已执行」，
    # 于是一次没发生的执行可以被本地任意持 token 的调用方直接写成成功，
    # 而 driver 随后把 "[已写入可见终端] …" 喂给模型并落审计。
    confirmed_by_user: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class PendingDeliveryManager:
    """待交付命令注册表 — 线程安全单例"""

    def __init__(self) -> None:
        self._registry: dict[str, PendingDelivery] = {}
        self._lock = threading.Lock()

    def create(
        self,
        command: str,
        session_id: str,
        requires_confirm: bool,
        run_id: str,
        timeout: int = DEFAULT_DELIVERY_TIMEOUT,
    ) -> PendingDelivery:
        """创建并注册一条待交付命令，返回 PendingDelivery"""
        tool_id = uuid.uuid4().hex
        delivery = PendingDelivery(
            tool_id=tool_id,
            command=command,
            session_id=session_id,
            requires_confirm=requires_confirm,
            run_id=run_id,
            timeout=timeout,
        )
        with self._lock:
            self._registry[tool_id] = delivery
        logger.debug(
            "登记可见终端待交付 (tool_id=%s, run=%s, confirm=%s)",
            tool_id, run_id, requires_confirm,
        )
        return delivery

    def get(self, tool_id: str) -> Optional[PendingDelivery]:
        with self._lock:
            return self._registry.get(tool_id)

    def remove(self, tool_id: str) -> None:
        with self._lock:
            self._registry.pop(tool_id, None)

    def timeout_of(self, tool_id: str) -> int:
        delivery = self.get(tool_id)
        return delivery.timeout if delivery else DEFAULT_DELIVERY_TIMEOUT

    async def wait(self, tool_id: str) -> None:
        """挂起直到该 tool_id 的待交付被 resume/reject，或被 clear_run 唤醒。

        本方法不带超时 —— 超时由调用方 asyncio.wait_for 负责（见 driver）。
        """
        delivery = self.get(tool_id)
        if delivery is None:
            return
        await delivery.event.wait()

    def resume(
        self,
        tool_id: str,
        output: str = "",
        success: bool = False,
        exit_code: Optional[int] = None,
        confirmed: bool = False,
    ) -> bool:
        """前端回传：填充结果并唤醒等待方。返回 False 表示 tool_id 未知

        `confirmed` 由路由侧判定后传入（只有 HTTP 请求那一侧知道调用方声明了什么），
        这里把它记到交付项上，供 driver 与审计读取「这次闭合到底有没有经过用户」。
        """
        delivery = self.get(tool_id)
        if delivery is None:
            return False
        delivery.output = output or ""
        delivery.success = bool(success)
        delivery.exit_code = exit_code
        delivery.rejected = False
        delivery.confirmed_by_user = bool(confirmed)
        delivery.event.set()
        logger.debug(
            "可见终端待交付已恢复 (tool_id=%s, success=%s, exit_code=%s)",
            tool_id, success, exit_code,
        )
        return True

    def reject(self, tool_id: str) -> bool:
        """用户拒绝执行：标记 rejected 并唤醒等待方。返回 False 表示 tool_id 未知"""
        delivery = self.get(tool_id)
        if delivery is None:
            return False
        delivery.rejected = True
        delivery.event.set()
        logger.info("用户拒绝可见终端命令 (tool_id=%s, command=%s)", tool_id, delivery.command)
        return True

    def clear_run(self, run_id: str) -> None:
        """移除某 run 的所有待交付项（run 结束/中止时调用）"""
        with self._lock:
            doomed = [tid for tid, d in self._registry.items() if d.run_id == run_id]
            cleared = []
            for tid in doomed:
                d = self._registry.pop(tid, None)
                if d is not None:
                    cleared.append(d)
            # 必须唤醒等待方：原实现只 pop 不 set，Driver 已经 await 在 event.wait()
            # 上时没人再碰它 —— 只能等自己的 wait_for 超时（默认 300s）才退出，
            # run 早已结束却挂满五分钟。set 后等待方按 get()==None 走「未确认」分支。
            for d in cleared:
                d.event.set()
        if doomed:
            logger.info("清理可见终端待交付 (run=%s, count=%d)", run_id, len(doomed))


# 进程级单例 — 与 permission gate 等同级，供工具/Driver/路由共享
PendingDeliveries = PendingDeliveryManager()