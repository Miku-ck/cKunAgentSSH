"""
Stirrups — 马镫

错误恢复 + LLM 重试 + 操作回滚

迁移自原 error_recovery.py，扩展：
- LLM 调用包装（含分类重试 + 上下文压缩触发）
- 工具错误分类反馈
- Undo 回滚执行
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .contracts import ErrorType, LLMOutput, RunContext, TraceEvent, UndoAction

logger = logging.getLogger(__name__)


class ErrorClassifier:
    """错误分类器 — 迁移自 error_recovery.py"""

    @staticmethod
    def classify_tool_error(error: str, tool_name: str = "") -> ErrorType:
        error_lower = error.lower()
        # 瞬时（H13 修复 B7：扩充中文标记）
        transient_markers = [
            "timeout", "timed out", "connection", "temporarily", "503", "502", "reset",
            "超时", "连接", "暂时", "重试",  # 中文
        ]
        if any(m in error_lower for m in transient_markers):
            return ErrorType.TRANSIENT
        # 权限
        forbidden_markers = [
            "forbidden", "permission", "intercepted",
            "拦截", "权限", "禁止", "未授权",  # 中文
        ]
        if any(m in error_lower for m in forbidden_markers):
            return ErrorType.FORBIDDEN
        # 致命
        fatal_markers = [
            "crash", "segfault", "database", "sqlite", "disk full",
            "崩溃", "磁盘满", "数据库错误",  # 中文
        ]
        if any(m in error_lower for m in fatal_markers):
            return ErrorType.FATAL
        return ErrorType.INVALID_ARGS

    @staticmethod
    def classify_llm_error(error: str) -> ErrorType:
        error_lower = error.lower()
        # 上下文超限 → 触发压缩（H13 修复 B7：扩充中文标记）
        if "context length" in error_lower or "maximum context" in error_lower or "上下文超限" in error or "上下文长度" in error:
            return ErrorType.INVALID_ARGS
        # 服务不可用 / 网络瞬时错误（H13 修复 B7：扩充中文标记）
        # 注意：connection/连接 必须放这里，否则会被归为 FATAL 导致不重试直接失败
        transient_markers = [
            "503", "overloaded", "rate limit", "服务不可用", "限流",
            "connection", "connect", "timeout", "timed out", "reset", "refused",
            "连接", "超时", "重置", "拒绝",
        ]
        if any(m in error_lower for m in transient_markers):
            return ErrorType.TRANSIENT
        return ErrorType.FATAL


def build_error_feedback(error: str, tool_name: str, error_type: ErrorType) -> str:
    """为 LLM 构建错误反馈消息 — 迁移自 error_recovery.build_error_feedback"""
    if error_type == ErrorType.INVALID_ARGS:
        return f"工具 {tool_name} 执行失败（参数错误）: {error}\n请检查参数格式后重试，或换一种方法。"
    if error_type == ErrorType.FORBIDDEN:
        return f"工具 {tool_name} 被安全策略拦截: {error}\n请使用其他方法完成任务。"
    if error_type == ErrorType.TRANSIENT:
        return f"工具 {tool_name} 暂时不可用: {error}\n可以考虑稍后重试或使用替代方案。"
    return f"工具 {tool_name} 发生致命错误: {error}"


async def retry_with_backoff(
    func: Callable[[], Awaitable[Any]],
    classifier: Optional[ErrorClassifier] = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> tuple[Any, Optional[str]]:
    """工具执行的指数退避重试 — 模块级函数（H13 修复 C3）

    让 Saddle 能直接调用，无需实例化 Stirrups。

    Args:
        func: 待重试的异步函数
        classifier: 错误分类器（None 时新建）
        max_retries: 最大重试次数
        base_delay: 基础延迟（指数退避）

    Returns:
        (result, error) — result 不为 None 表示成功
    """
    clf = classifier or ErrorClassifier()
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        try:
            result = await func()
            return result, None
        except Exception as e:
            last_error = str(e)
            kind = clf.classify_tool_error(last_error, "")
            if kind in (ErrorType.FATAL, ErrorType.FORBIDDEN):
                return None, last_error
            if kind != ErrorType.TRANSIENT:
                return None, last_error
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
    return None, last_error


@dataclass
class Stirrups:
    """
    马镫 — 错误恢复 + LLM 重试 + 回滚

    职责：
    1. call_llm: 带分类重试的 LLM 调用（上下文超限时触发压缩）
    2. undo: 按 LIFO 顺序回滚操作
    3. undo_all: 回滚整个 run 的所有操作

    工具级指数退避重试见模块级 retry_with_backoff（Saddle 直接调用，无需实例化）。
    """

    llm_client: Any  # LLMClient 协议实现
    classifier: ErrorClassifier = field(default_factory=ErrorClassifier)
    # 上下文压缩回调（由 Bridle 注入）
    _compress_callback: Optional[Callable[[RunContext, bool], Awaitable[None]]] = None
    # 消息重建回调（由 Harness 注入）：压缩后按 ctx 重新组装消息列表。
    # 审查修复：压缩回调只更新 ctx（messages/摘要元数据），而 call_llm 的
    # messages 入参是压缩前快照 — 不重建则重试请求体不缩水，必然再次 400
    _messages_rebuilder: Optional[Callable[[RunContext], list[dict[str, str]]]] = None

    def set_compress_callback(
        self,
        cb: Callable[[RunContext, bool], Awaitable[None]],
    ) -> None:
        """Bridle 注入上下文压缩回调"""
        self._compress_callback = cb

    def set_messages_rebuilder(
        self,
        cb: Callable[[RunContext], list[dict[str, str]]],
    ) -> None:
        """Harness 注入消息重建回调（压缩后重取）"""
        self._messages_rebuilder = cb

    async def call_llm(
        self,
        ctx: RunContext,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> LLMOutput:
        """
        带分类重试的 LLM 调用

        - TRANSIENT: 指数退避重试
        - INVALID_ARGS (上下文超限): 触发压缩后重试
        - FATAL: 直接抛出

        思考预算：从 ctx.config.max_reasoning_tokens 读取（由意图识别器在 run 启动时
        写入），Driver 可在每轮调用前动态更新该字段实现"首轮宽、后续严"的策略。
        """
        max_retries = ctx.config.llm_max_retries
        last_error: Optional[Exception] = None
        # 审查 NIT：call_llm 内强制压缩次数守卫。原实现对每次 INVALID_ARGS（上下文超限）
        # 失败都触发一次 force 压缩 — 压缩无法进一步收缩时（摘要已最小）会反复执行
        # 昂贵的压缩 LLM 调用，直到重试耗尽；同一 call 内最多压一次，之后直接失败。
        compress_attempts = 0
        _MAX_COMPRESS_PER_CALL = 1

        # 思考预算（仅 enable_thinking=True 时有意义）
        # None 表示不限制；int 透传给 vLLM chat_template_kwargs.thinking_budget
        reasoning_budget = ctx.config.max_reasoning_tokens
        if not ctx.config.enable_thinking:
            # 思考未开启时显式传 None，避免无意义参数
            reasoning_budget = None

        for attempt in range(max_retries + 1):
            try:
                return await self.llm_client.call(
                    messages=messages,
                    tools=tools,
                    model=ctx.llm_model,
                    temperature=ctx.config.temperature,
                    max_tokens=ctx.config.max_tokens,
                    enable_thinking=ctx.config.enable_thinking,
                    reasoning_max_tokens=reasoning_budget,
                )
            except Exception as e:
                last_error = e
                err_str = str(e)
                kind = self.classifier.classify_llm_error(err_str)

                logger.warning(
                    "LLM 调用失败 [%s] attempt=%d/%d: %s",
                    kind.value, attempt + 1, max_retries + 1, err_str,
                )

                if kind == ErrorType.FATAL:
                    raise

                if kind == ErrorType.INVALID_ARGS and self._compress_callback:
                    if compress_attempts < _MAX_COMPRESS_PER_CALL:
                        # 上下文超限 → 强制压缩后重试
                        compress_attempts += 1
                        logger.info("触发上下文压缩后重试 (第 %d 次)", compress_attempts)
                        await self._compress_callback(ctx, force=True)
                        # 审查修复：压缩只改写 ctx，messages 入参仍是旧快照 →
                        # 必须重建，否则重试请求体未变小必然再次 400，且
                        # compress_attempts 耗尽后误报"压缩后仍超限"
                        if self._messages_rebuilder is not None:
                            messages = self._messages_rebuilder(ctx)
                        continue
                    # 压缩后仍超限 — 再压一次也是同样结果，直接失败
                    logger.error("上下文压缩后仍超限，放弃重试: %s", err_str)
                    raise

                if kind == ErrorType.TRANSIENT and attempt < max_retries:
                    delay = 2.0 ** attempt  # 1s, 2s, 4s
                    await asyncio.sleep(delay)
                    continue

                # 重试耗尽
                raise

        # 不应到达
        raise last_error or RuntimeError("LLM 调用失败（未知原因）")

    async def undo(self, actions: list[UndoAction]) -> None:
        """按 LIFO 顺序回滚指定操作"""
        for action in reversed(actions):
            try:
                await action.apply_reverse()
            except Exception as e:
                logger.warning("回滚失败 %s: %s", type(action).__name__, e)

    async def undo_all(self, ctx: RunContext) -> None:
        """回滚整个 run 的所有操作"""
        await self.undo(ctx.undo_stack)
        ctx.undo_stack.clear()
