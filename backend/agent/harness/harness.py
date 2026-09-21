"""
Harness — 驾驭装备总成

组装所有组件，提供 run() 入口。

组件注入顺序：
1. LLMClient（必需）
2. ToolRegistry（必需）
3. Reins（策略层，默认注册 5 个内置拦截器）
4. Saddle（工具执行，包装 ToolRegistry）
5. Stirrups（错误恢复，包装 LLMClient）
6. Bridle（输入组装）
7. TelemetryBus（事件总线）
8. CostTracker（成本追踪）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from .contracts import (
    AgentConfig,
    LLMClient,
    RunContext,
    State,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from .telemetry import TelemetryBus
from .driver import Driver
from .bridle import Bridle, CompressionConfig
from .reins import Reins, create_default_reins
from .saddle import Saddle, Sandbox, UndoBuilder
from .stirrups import Stirrups

logger = logging.getLogger(__name__)


@dataclass
class HarnessConfig:
    """Harness 配置"""
    # LLM 配置
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 上下文窗口
    max_context_tokens: int = 16384
    compression: CompressionConfig = field(default_factory=CompressionConfig)


class Harness:
    """
    驾驭装备总成

    用法：
        harness = Harness(config)
        harness.assemble(llm_client, tool_registry)
        async for event in harness.run(task, mode, **opts):
            handle(event)
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.telemetry = TelemetryBus()
        self.bridle = Bridle(
            max_context_tokens=config.max_context_tokens,
            config=config.compression,
        )
        self.reins = create_default_reins()
        self.driver = Driver()
        self.cost_tracker: Any = None  # 由 assemble 注入
        self._llm_client: Optional[LLMClient] = None
        self._tool_registry: Any = None
        self._saddle: Optional[Saddle] = None
        self._stirrups: Optional[Stirrups] = None
        self._assembled = False
        # 流式 chunk 事件队列：StreamLLMClient 在 LLM 调用期间把逐 token 事件放到这里，
        # Harness.run 统一与 Driver 事件合并后 yield 给调用方。
        self._chunk_queue: Optional[asyncio.Queue[TraceEvent]] = None

    def assemble(
        self,
        llm_client: LLMClient,
        tool_registry: Any,
        cost_tracker: Any = None,
    ) -> None:
        """
        组装阶段 — 注入依赖，建立组件间连接

        Args:
            llm_client: LLM 客户端（实现 LLMClient 协议）
            tool_registry: 工具注册表（实现 ToolRegistry 协议）
            cost_tracker: 成本追踪器（原 agent.cost_tracker.CostTracker）
        """
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self.cost_tracker = cost_tracker

        # Saddle 包装 ToolRegistry
        self._saddle = Saddle(
            registry=tool_registry,
            sandbox=Sandbox(),
            undo_builder=UndoBuilder(),
        )

        # Stirrups 包装 LLMClient
        self._stirrups = Stirrups(llm_client=llm_client)
        # 注入压缩回调
        self._stirrups.set_compress_callback(self.bridle.compress)
        # 审查修复：压缩后按 ctx 重建消息 — 压缩只更新 ctx，Stirrups.call_llm
        # 持有的 messages 快照不随之缩小，不重建则上下文超限重试必然再次 400
        self._stirrups.set_messages_rebuilder(self.bridle.assemble)

        # 如果是 StreamLLMClient，注入 token 回调与 chunk 流式回调
        from .llm_client import StreamLLMClient
        if isinstance(llm_client, StreamLLMClient):
            if cost_tracker:
                llm_client.set_token_callback(
                    lambda p, c: cost_tracker.record_llm_call(p, c)
                )
            # 把 LLM 逐 token 输出放到 chunk 队列，由 Harness.run 统一 yield 给前端
            async def emit_chunk(content_delta: str, reasoning_delta: str) -> None:
                if not self._chunk_queue:
                    return
                if content_delta:
                    await self._chunk_queue.put(TraceEvent(
                        kind="content",
                        source="llm_client",
                        run_id="",
                        payload={"content": content_delta},
                    ))
                if reasoning_delta:
                    await self._chunk_queue.put(TraceEvent(
                        kind="reasoning",
                        source="llm_client",
                        run_id="",
                        payload={"reasoning": reasoning_delta},
                    ))
            llm_client.set_chunk_callback(emit_chunk)

        self._assembled = True
        logger.info("Harness 组装完成")

    @property
    def saddle(self) -> Saddle:
        if not self._saddle:
            raise RuntimeError("Harness 未 assemble，saddle 不可用")
        return self._saddle

    @property
    def stirrups(self) -> Stirrups:
        if not self._stirrups:
            raise RuntimeError("Harness 未 assemble，stirrups 不可用")
        return self._stirrups

    async def run(
        self,
        task: str,
        mode: str,
        conversation_id: str = "",
        ssh_session_id: Optional[str] = None,
        environment: str = "test",
        enable_visible_terminal: bool = False,
        system_prompt: str = "",
        history: Optional[list[dict[str, str]]] = None,
        memories: Optional[list[str]] = None,
        config: Optional[AgentConfig] = None,
        run_id: Optional[str] = None,
        skill_ids: Optional[list[str]] = None,
        auto_skill_match: bool = True,
    ) -> AsyncIterator[TraceEvent]:
        """
        主入口 — yield TraceEvent

        调用方（routes/agent.py）订阅 telemetry bus + 转发 driver 产出的事件。

        Args:
            run_id: 可选 run_id（与 DB 记录保持一致）；不传则自动生成
            skill_ids: 前端显式选择的技能 ID 列表，由 SkillRein 在 AGENT_START 注入
        """
        if not self._assembled:
            raise RuntimeError("Harness 未 assemble，请先调用 assemble()")

        # H13 修复 A2：注册日志订阅者（让 TelemetryBus.subscribe 不再是死代码）
        # 用途：把所有 TraceEvent 转为结构化日志，便于离线分析/调试
        # 原实现 TelemetryBus.subscribe/emit 已实现但生产路径零订阅者
        if not getattr(self, "_log_subscriber_registered", False):
            def _log_subscriber(event: TraceEvent) -> None:
                # 关键事件用 info，常规事件用 debug，避免日志爆炸
                if event.kind in ("error", "warning", "human_help_needed", "permission_needed"):
                    logger.info(
                        "[TRACE] run=%s kind=%s iter=%d source=%s payload_keys=%s",
                        event.run_id, event.kind, event.iteration, event.source,
                        list(event.payload.keys()) if event.payload else [],
                    )
                else:
                    logger.debug(
                        "[TRACE] run=%s kind=%s iter=%d source=%s",
                        event.run_id, event.kind, event.iteration, event.source,
                    )
            self.telemetry.subscribe(_log_subscriber)
            self._log_subscriber_registered = True

        # 用实际 run 配置重新初始化 reins（保证 DynamicBudgetRein 的参数正确）
        # create_default_reins 用 run 的 config 而非 Harness 构造时的默认 config
        # 传入 saddle：PermissionGate 从 ToolRegistry 读取 ToolMeta（单一数据源）
        actual_config = config or AgentConfig()
        self.reins = create_default_reins(
            config=actual_config,
            saddle=self._saddle,
        )

        # 构建 RunContext
        ctx = RunContext.new(
            task=task,
            mode=mode,
            conversation_id=conversation_id,
            ssh_session_id=ssh_session_id,
            environment=environment,
            visible_terminal=enable_visible_terminal,
            system_prompt=system_prompt,
            history=history or [],
            memories=memories or [],
            config=config or AgentConfig(),
            skill_ids=skill_ids or [],
            auto_skill_match=auto_skill_match,
        )
        # 如果调用方指定 run_id，覆盖自动生成的
        if run_id:
            ctx.run_id = run_id
        # 注入 LLM 配置
        ctx.llm_base_url = self.config.base_url
        ctx.llm_api_key = self.config.api_key
        ctx.llm_model = self.config.model

        # 审查 MINOR：per-run saddle 引用。PermissionGate 是进程级单例，
        # 其 saddle 字段会被后启动的并发 run 覆盖；拦截器改从 ctx.metadata 读取
        # 当前 run 的 Saddle，避免查错 ToolRegistry 误判"工具未注册"。
        ctx.metadata["saddle"] = self._saddle
        # 审批卡片的"迟到增益"通道：拦截器在卡片发出后异步补发事件（如命令的
        # LLM 后果解释），无法从 ReinsDecision.events 返回值搭车，需要主动向
        # chunk 队列投递。run 终态把 _chunk_queue 置 None（见下方 finally），
        # 届时投递自动成为 no-op —— 卡片已消失/已授权的迟到事件天然被丢弃。
        async def emit_event(event: TraceEvent) -> None:
            if self._chunk_queue is not None:
                await self._chunk_queue.put(event)
        ctx.metadata["emit_event"] = emit_event
        # C-review M10：注入 per-run 成本追踪器，CostBudgetRein 熔断读取实时累计
        # （ctx.metadata 是 per-run dict，不受并发 run 影响）
        if self.cost_tracker is not None:
            ctx.metadata["cost_tracker"] = self.cost_tracker

        # 首条消息：用户任务
        ctx.messages.append({"role": "user", "content": task})

        # 运行 Driver，并把 LLM 流式 chunk 事件与 Driver 事件合并后统一 yield
        self._chunk_queue = asyncio.Queue()
        driver_task: asyncio.Task | None = None

        async def _driver_pump() -> None:
            try:
                async for event in self.driver.drive(ctx, self):
                    if self._chunk_queue is not None:
                        await self._chunk_queue.put(event)
            finally:
                # 用 None 作为结束信号，让主循环的 queue.get() 能安全退出
                if self._chunk_queue is not None:
                    await self._chunk_queue.put(None)

        try:
            driver_task = asyncio.create_task(_driver_pump())
            while True:
                if self._chunk_queue is None:
                    break
                event = await self._chunk_queue.get()
                if event is None:
                    break
                yield event
        finally:
            self._chunk_queue = None
            if driver_task and not driver_task.done():
                driver_task.cancel()
                try:
                    await driver_task
                except asyncio.CancelledError:
                    pass
            # driver 异常会在 task 结束时抛出（通过 asyncio.gather 机制被事件循环记录），
            # 但这里主循环已经消费完所有事件；为保险起见检查一次结果
            if driver_task and driver_task.done() and not driver_task.cancelled():
                exc = driver_task.exception()
                if exc:
                    raise exc
