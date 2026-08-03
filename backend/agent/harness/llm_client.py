"""
LLM Client Adapter — 把原 llm_adapter.chat_stream 包装成 LLMClient 协议

让 Stirrups 可以通过统一接口调 LLM，便于 mock。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .contracts import LLMClient, LLMOutput, ToolCall as HarnessToolCall

logger = logging.getLogger(__name__)

# chunk 回调签名：content_delta, reasoning_delta
ChunkCallback = Callable[[str, str], Awaitable[None]]


class StreamLLMClient:
    """
    流式 LLM 客户端 — 包装 llm_adapter.chat_stream

    实现 LLMClient 协议，供 Stirrups 调用。
    同时通过 set_chunk_callback 把逐 token 内容实时推送到 Harness 事件总线，
    避免 Agent 模式像普通 HTTP 请求一样整段返回。
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        # token 用量回调（由 Harness 注入，用于 cost_tracker）
        self._on_tokens: Any = None
        # chunk 流式回调（由 Harness 注入，用于实时 emit content/reasoning）
        self._on_chunk: ChunkCallback | None = None

    def set_token_callback(self, cb) -> None:
        """Harness 注入 token 用量回调"""
        self._on_tokens = cb

    def set_chunk_callback(self, cb: ChunkCallback) -> None:
        """Harness 注入 chunk 流式回调"""
        self._on_chunk = cb

    async def call(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        reasoning_max_tokens: int | None = None,
    ) -> LLMOutput:
        """调用 LLM，收集完整响应；期间通过 _on_chunk 实时推送 token

        Args:
            reasoning_max_tokens: thinking 阶段 token 上限（仅 enable_thinking=True 时生效）
                None 表示不限制；由意图识别器根据 task 复杂度推断
        """
        from llm_adapter import chat_stream
        from .bridle import estimate_messages_tokens

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[HarnessToolCall] = []
        prompt_tokens = 0
        completion_tokens = 0

        async for chunk in chat_stream(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            tools=tools if tools else None,
            reasoning_max_tokens=reasoning_max_tokens,
        ):
            if chunk.content:
                content_parts.append(chunk.content)
            if chunk.reasoning:
                reasoning_parts.append(chunk.reasoning)
            # 实时推送：让 Agent 运行过程可见
            if self._on_chunk and (chunk.content or chunk.reasoning):
                try:
                    await self._on_chunk(chunk.content or "", chunk.reasoning or "")
                except Exception as e:
                    logger.warning("chunk 回调异常: %s", e)
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    tool_calls.append(HarnessToolCall(
                        id=tc.id,
                        name=tc.name,
                        args=tc.parsed_args(),
                    ))
            if chunk.prompt_tokens:
                prompt_tokens = chunk.prompt_tokens
            if chunk.completion_tokens:
                completion_tokens = chunk.completion_tokens

        # 估算兜底（LLM 未返回 usage 时）
        if prompt_tokens == 0:
            prompt_tokens = estimate_messages_tokens(messages)
        if completion_tokens == 0:
            completion_tokens = estimate_messages_tokens(
                [{"role": "assistant", "content": "".join(content_parts)}]
            )

        # 通知 Harness 记录成本
        if self._on_tokens:
            try:
                self._on_tokens(prompt_tokens, completion_tokens)
            except Exception as e:
                logger.warning("token 回调异常: %s", e)

        return LLMOutput(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
