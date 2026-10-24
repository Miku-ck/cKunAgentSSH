"""
Telemetry Bus — 遥测总线

统一事件总线，替代原 engine.run() 直接 yield SSE 的方式。

特性：
- 多订阅者：SSE 路由 / 日志 / 指标
- 故障隔离：单个订阅者异常不影响其他
- 内存缓冲：run 生命周期内保留最近 500 条事件（FIFO 截断），
  run 结束即 clear、从不落盘 —— 不支持离线重放整个 run
  （C-review MINOR-9：原"可重放"承诺不成立，已修订；审计需求见 audit.append_record）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .contracts import TraceEvent

logger = logging.getLogger(__name__)

# 订阅者回调签名 — 同步或异步均可
Subscriber = Callable[[TraceEvent], Any]


class TelemetryBus:
    """
    遥测总线 — 所有组件通过 emit() 发事件，订阅者通过 subscribe() 收

    用法：
        bus = TelemetryBus()
        bus.subscribe(lambda e: print(e.kind))           # 同步
        bus.subscribe(my_async_handler)                   # 异步
        await bus.emit(TraceEvent(...))
    """

    # 事件序列最大缓存数（防止内存泄漏）
    _MAX_EVENTS = 500

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._events: list[TraceEvent] = []  # 事件序列（内存缓冲，有上限，run 结束清空）
        self._lock = asyncio.Lock()

    def subscribe(self, callback: Subscriber) -> None:
        """订阅事件 — 回调可以是同步或异步函数"""
        self._subscribers.append(callback)

    async def emit(self, event: TraceEvent) -> None:
        """发事件 — 按订阅顺序通知，单个异常不影响其他"""
        async with self._lock:
            self._events.append(event)
            # 超过上限时丢弃最早的事件（FIFO）
            if len(self._events) > self._MAX_EVENTS:
                self._events = self._events[-self._MAX_EVENTS:]

        # 审查 NIT 修复：异步订阅者并发执行（gather）。原实现逐个 await —
        # 任一慢订阅者会阻塞后续所有订阅者（含 SSE 队列投递），拖慢整个事件流。
        # 同步订阅者保持 subscribe 顺序；各异步订阅者内部收到的事件顺序不变。
        pending: list = []
        for cb in self._subscribers:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    pending.append(result)
            except Exception as e:
                logger.warning("TelemetryBus 订阅者异常: %s", e, exc_info=True)
        if pending:
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            for r in outcomes:
                if isinstance(r, Exception):
                    logger.warning("TelemetryBus 异步订阅者异常: %s", r, exc_info=r)

    def events(self) -> list[TraceEvent]:
        """获取事件序列副本（用于调试；仅 run 生命周期内有效，不落盘）

        同步方法不能持 asyncio.Lock；list() 浅拷贝是原子的，与 emit 并发时最多多/少一个事件，
        不会结构损坏。可接受此竞态。
        """
        return list(self._events)

    def clear(self) -> None:
        """清理事件缓存（同步，被 routes/agent.py 同步调用）

        与 emit 并发时可能丢失部分已 append 事件——可接受（clear 仅用于 run 结束后防泄漏）。
        """
        self._events.clear()


class SSEAdapter:
    """
    SSE 适配器 — 把 TraceEvent 转成 SSE 格式供 routes/agent.py 使用

    保持与原 SSE 协议兼容（前端无感知）：
    - TraceEvent(kind="state") → SSE type=state
    - TraceEvent(kind="content") → SSE type=content
    - TraceEvent(kind="tool_call_start") → SSE type=tool_call_start
    - ...
    """

    # TraceEvent.kind → 前端 SSE event type 的映射
    _KIND_MAP = {
        "state": "state",
        "content": "content",
        "reasoning": "reasoning",
        "tool_call_start": "tool_call_start",
        "tool_call_result": "tool_call_result",
        "warning": "warning",
        "error": "error",
        "human_help_needed": "human_help_needed",
        "permission_needed": "permission_needed",
        # C-review M7：审批超时事件（超时按拒绝，前端标记对应授权卡片过期）
        "permission_expired": "permission_expired",
        # 审批卡片可读性：命令后果解释的迟到回填（卡片已弹出，解释生成完再补）
        "permission_explain": "permission_explain",
        "done": "done",
        # H13 修复 B3：补全三种事件类型映射
        "intention": "intention",
        "budget_extended": "budget_extended",
        "reflection_request": "reflection_request",
        # 可见终端模式：通知前端把命令写入真实 PTY/SSH 会话并等待回传
        "tool_deliver": "tool_deliver",
        # 下列三种原先只靠 to_sse_dict 的 `get(kind, kind)` 恒等兜底上 Wire ——
        # 行为一样，但清单不再是全集，跨层守卫（test_trace_projection_keys）
        # 就无从判断"某个事件从来没被前端声明过"是漏写还是有意。
        # 澄清向导：题元与进度（前端唯一可读来源）
        "clarification_needed": "clarification_needed",
        "clarification_done": "clarification_done",
        # plan 档下写操作被拦：只落 trace，前端暂无 case（新增事件请一并补 agent.ts）
        "plan_mode_block": "plan_mode_block",
    }

    @classmethod
    def to_sse_dict(cls, event: TraceEvent) -> dict[str, Any]:
        """
        把 TraceEvent 转成前端期望的 SSE dict 格式

        前端 AgentSSEEvent 接口字段：
        type / state / iteration / content / reasoning / tool / args / result /
        success / message / iterations / state_history / reason / cost / run_id
        """
        kind = event.kind
        payload = event.payload
        sse_type = cls._KIND_MAP.get(kind, kind)

        result: dict[str, Any] = {
            "type": sse_type,
            "iteration": event.iteration,
        }
        # 问题 1/4：全事件透传 run_id — 前端需实时捕获当前 run_id
        # （once 授权键控 / visible_terminal 拒绝标记 / 中止清理都依赖它）
        if event.run_id:
            result["run_id"] = event.run_id

        # 按 kind 填充字段（与原 engine.py yield 的 dict 结构保持一致）
        if kind == "state":
            result["state"] = payload.get("state", "")
        elif kind == "content":
            result["content"] = payload.get("content", "")
        elif kind == "reasoning":
            result["reasoning"] = payload.get("reasoning", "")
        elif kind == "tool_call_start":
            result["tool"] = payload.get("tool", "")
            result["args"] = payload.get("args", {})
            if payload.get("tool_call_id"):
                result["tool_call_id"] = payload.get("tool_call_id")
        elif kind == "tool_call_result":
            result["tool"] = payload.get("tool", "")
            result["result"] = payload.get("result", "")
            result["success"] = payload.get("success", True)
            result["duration_ms"] = payload.get("duration_ms", 0)
            if payload.get("tool_call_id"):
                result["tool_call_id"] = payload.get("tool_call_id")
        elif kind == "warning":
            result["message"] = payload.get("message", "")
        elif kind == "error":
            result["message"] = payload.get("message", "")
        elif kind == "human_help_needed":
            # S1-06：这里原先读 tool/args —— 生产者（ConfidenceMonitorRein）的 payload
            # 只有 reason + confidence，于是这两个字段在 Wire 上恒为空，而真写了的
            # confidence 反倒没被搬走。UI 只渲染 reason，故删幽灵字段、补真实那一个。
            result["reason"] = payload.get("reason", "")
            result["confidence"] = payload.get("confidence", 0.0)
            # 2026-10-23：求助卡带可执行建议（ConfidenceMonitor._diagnose_blocker
            # 产出），前端在 reason 下方渲染「建议：…」行
            if payload.get("suggestion"):
                result["suggestion"] = payload["suggestion"]
        elif kind == "permission_needed":
            result["tool"] = payload.get("tool", "")
            result["args"] = payload.get("args", {})
            result["risk"] = payload.get("risk", "")
            result["reversible"] = payload.get("reversible", True)
            result["message"] = payload.get("message", "")
            # 问题 2 接线补完：透传归一化展示命令（reins 已写入 payload，
            # 原实现未映射 → 前端授权卡片「授权命令」行永远收不到）
            result["normalized_command"] = payload.get("normalized_command", "")
            # 审批卡片可读性字段：中文后果说明 + 用户原任务 / 环境 / 目标会话 /
            # 命令原文（reins 由 permission_display_fields 写入，见 core.py）
            result["explain"] = payload.get("explain", "")
            result["explain_source"] = payload.get("explain_source", "")
            result["environment"] = payload.get("environment", "")
            result["task"] = payload.get("task", "")
            result["ssh_session_id"] = payload.get("ssh_session_id", "")
            result["command"] = payload.get("command", "")
            # 透传 run_id（路由层会对所有 SSE dict 统一注入，此处保持
            # to_sse_dict 自洽：脱离路由单测/其他消费方也能拿到）
            if event.run_id:
                result["run_id"] = event.run_id
        elif kind == "permission_explain":
            # 卡片已弹出后才生成的解释，按 tool+command 回填到对应卡片；
            # 带 tool_id = 交付卡（tool_deliver）回填，前端按 tool_id 精确找回
            result["tool"] = payload.get("tool", "")
            result["command"] = payload.get("command", "")
            result["normalized_command"] = payload.get("normalized_command", "")
            result["explanation"] = payload.get("explanation", "")
            if payload.get("tool_id"):
                result["tool_id"] = payload["tool_id"]
        elif kind == "permission_expired":
            result["tool"] = payload.get("tool", "")
            result["args"] = payload.get("args", {})
            # command 必须透传：前端 permCardKey(tool, command) 用它定位那张超时卡片，
            # 缺失时 key 永远对不上 → 后端已按拒绝处理，卡片却仍可点击（孤儿卡片）
            result["command"] = payload.get("command", "")
            result["message"] = payload.get("message", "")
        elif kind == "done":
            result["state"] = payload.get("state", "")
            result["iterations"] = payload.get("iterations", 0)
            result["state_history"] = payload.get("state_history", [])
            result["cost"] = payload.get("cost")
            result["run_id"] = payload.get("run_id", "")
            # 生产者在 driver 的 done 载荷里就写了 context_summary（spec §8：run 内
            # 滚动摘要随 done 出栈，由 routes 落到 agent_runs 行），routes 也一直在读
            # （:979 落库、:1077 喂 episodic job）。这一段 done 投影是**白名单**，
            # 当时没跟着补 —— 于是那条链两头都在、中间断了：agent_runs.context_summary
            # 恒为 NULL，跨 run 的滚动摘要永不复用，轨迹回放也拿不到摘要。
            # 不报错、不告警，只是永远读不到 —— 这一族的典型形态。
            result["context_summary"] = payload.get("context_summary", "")
        # H13 修复 B3：补全三种事件类型的字段提取
        elif kind == "intention":
            # H14: 嵌套在 intention 字段下，与前端 AgentIntentionInfo 接口对齐
            result["intention"] = {
                "level": payload.get("level", ""),
                "task_type": payload.get("task_type", ""),
                "reasoning_budget": payload.get("reasoning_budget"),
                "allow_planning_phase": payload.get("allow_planning_phase", False),
                "confidence": payload.get("confidence", 0.0),
            }
        elif kind == "budget_extended":
            # S1-06：原投影读 `reason` —— 生产者（DynamicBudgetRein）从来没写过这个键，
            # 于是线上永远带一个空 reason；而真正说明「为什么加预算」的 added / files_edited /
            # cap 反被丢掉。与 done 少投影 context_summary 是同一族：两头都在、中间断了，
            # 且不报错不告警。投影照生产者的实际键走。
            result["initial"] = payload.get("initial", 0)
            result["extended_to"] = payload.get("extended_to", 0)
            result["added"] = payload.get("added", 0)
            result["files_edited"] = payload.get("files_edited", 0)
            result["files_read"] = payload.get("files_read", 0)
            result["total_tool_calls"] = payload.get("total_tool_calls", 0)
            result["cap"] = payload.get("cap", 0)
        elif kind == "reflection_request":
            result["final_state"] = payload.get("final_state", "")
            result["iterations"] = payload.get("iterations", 0)
            result["max_iterations"] = payload.get("max_iterations", 0)
            result["tool_calls_count"] = payload.get("tool_calls_count", 0)
            result["unique_tools"] = payload.get("unique_tools", [])
            result["failed_tools"] = payload.get("failed_tools", [])
            result["has_warnings"] = payload.get("has_warnings", False)
            result["has_cost"] = payload.get("has_cost", False)
        elif kind == "tool_deliver":
            result["tool_id"] = payload.get("tool_id", "")
            result["command"] = payload.get("command", "")
            result["session_id"] = payload.get("session_id", "")
            result["requires_confirm"] = payload.get("requires_confirm", False)
            # P1b：整串只读证明（prod 自动下发依据 + 卡片徽章文案）
            result["readonly"] = payload.get("readonly", False)
            result["readonly_rule"] = payload.get("readonly_rule", "")
            # 命令后果说明（与审批卡同款解释层）：交付卡的「这条命令会做什么」行
            result["explain"] = payload.get("explain", "")
            result["explain_source"] = payload.get("explain_source", "")
            # 拆解层：复合命令逐段意图（单段为空数组）、聚合风险展示、发起任务上下文
            result["segments"] = payload.get("segments", [])
            result["risk"] = payload.get("risk", "")
            result["task"] = payload.get("task", "")
        elif kind == "clarification_needed":
            result["tool"] = payload.get("tool", "")
            result["tool_call_id"] = payload.get("tool_call_id", "")
            result["args"] = payload.get("args", {})
            # 生产端（reins.clarification._build_clarification_needed）的键是
            # step / total / steps[全量]，这里曾按 current_step / total_steps /
            # question / options 取值 —— 全部取不到 → 前端收到空题目空选项，
            # 澄清向导退化成只剩自定义输入框。现按真实契约取值并透传全量 steps
            # （前端本地逐步推进）。
            steps = payload.get("steps", []) or []
            idx = int(payload.get("step", 0) or 0)
            result["steps"] = steps
            result["current_step"] = idx
            result["total_steps"] = int(payload.get("total", 0) or len(steps))
            cur = steps[idx] if 0 <= idx < len(steps) else {}
            result["question"] = cur.get("question", "")
            result["options"] = cur.get("options", []) or []
            result["allow_custom"] = cur.get("allow_custom", True)
            result["reason"] = payload.get("reason", "")
        # clarification_done 无需投影字段：前端只按事件类型关闭向导，
        # answers/summary 只进生产端 payload 与注入消息，不上 SSE 线
        return result
