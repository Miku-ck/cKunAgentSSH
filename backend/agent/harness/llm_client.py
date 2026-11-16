"""
LLM Client Adapter — 把原 llm_adapter.chat_stream 包装成 LLMClient 协议

让 Stirrups 可以通过统一接口调 LLM，便于 mock。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .contracts import LLMOutput, ToolCall as HarnessToolCall

logger = logging.getLogger(__name__)

# chunk 回调签名：content_delta, reasoning_delta
ChunkCallback = Callable[[str, str], Awaitable[None]]

# 上游拒绝图片输入的典型错误标记（vLLM/OpenAI 兼容端点文案差异大，只做提示性匹配）
_VISION_REJECT_MARKERS = (
    "image", "vision", "multimodal", "visual", "图片", "视觉", "多模态",
)


def _looks_like_vision_reject(error: str) -> bool:
    text = (error or "").lower()
    return any(m in text for m in _VISION_REJECT_MARKERS)


def _attach_images_to_messages(
    messages: list[dict], images: list[dict],
) -> list[dict]:
    """把 image_url 内容块并入首条非空 user 消息（OpenAI 多模态 content 数组）。

    只在 LLM 请求边界做：ctx.messages / 压缩 / 历史持久化全链路都按 str content
    处理，数组内容一旦进 ctx 会破坏这些路径。每轮调用从 str 重新构造 —— 图片
    在工具循环的每次迭代、压缩重建后的重试里都保持可见，且不污染任何持久状态。
    """
    out: list[dict] = []
    merged = False
    for msg in messages:
        if (not merged and msg.get("role") == "user"
                and isinstance(msg.get("content"), str) and msg.get("content")):
            out.append({
                "role": "user",
                "content": [{"type": "text", "text": msg["content"]}, *images],
            })
            merged = True
        else:
            out.append(msg)
    if not merged:
        # 消息里没有可挂载的 user 文本（理论上不可达）：追加独立一条，绝不静默丢图
        out = [*out, {"role": "user", "content": list(images)}]
    return out


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
        # 放在末尾并带默认值：既有调用方（含测试替身）不传也能跑，
        # 而 harness 的真实调用点会显式把 models_config.top_p 递进来。
        top_p: float = 0.9,
        # 视觉多模态：image_url 内容块（由 Stirrups.images 注入，见 harness.assemble）。
        # None = 纯文本请求，与引入前逐字节等价。
        images: list[dict] | None = None,
    ) -> LLMOutput:
        """调用 LLM，收集完整响应；期间通过 _on_chunk 实时推送 token

        Args:
            reasoning_max_tokens: thinking 阶段 token 上限（仅 enable_thinking=True 时生效）
                None 表示不限制；由意图识别器根据 task 复杂度推断
        """
        from contextlib import aclosing

        from llm_adapter import chat_stream
        from .bridle import estimate_messages_tokens

        if images:
            messages = _attach_images_to_messages(messages, images)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[HarnessToolCall] = []
        prompt_tokens = 0
        completion_tokens = 0

        # aclosing 必加：Agent 运行可被用户中止，本协程会在 async for 处被取消，
        # 生成器就只能等 GC 收尾 —— 期间 chat_stream 的 _client_in_flight 不递减，
        # llm_adapter 的 LRU 淘汰据此拒绝 aclose 该客户端，连接池泄漏到进程结束。
        async with aclosing(chat_stream(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            # 模型配置里的 top_p 此前在这一层被丢弃：chat_stream 自己带 0.9 默认，
            # 于是设置页改成什么都是 0.9。
            top_p=top_p,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            tools=tools if tools else None,
            reasoning_max_tokens=reasoning_max_tokens,
        )) as chunk_iter:
            try:
                async for chunk in chunk_iter:
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
            except Exception as e:
                # fail-open 的代价：不支持视觉的模型要到这一步才暴露。错误文案里
                # 有图片/视觉类标记时补一句人话提示，避免用户对着 400 原文猜。
                if images and _looks_like_vision_reject(str(e)):
                    raise RuntimeError(
                        "模型未接受图片输入（可能不支持视觉多模态）——请改用多模态模型"
                        "或去掉图片附件后重试。原始错误: " + str(e)
                    ) from e
                raise

        # 估算兜底（LLM 未返回 usage 时）
        if prompt_tokens == 0:
            prompt_tokens = estimate_messages_tokens(messages)
        if completion_tokens == 0:
            # reasoning 与 content 同为补全输出，只估 content 会把 thinking 模型的
            # 输出 token 漏掉一大截（成本/用量统计随之偏低）
            completion_tokens = estimate_messages_tokens(
                [{"role": "assistant", "content": "\n".join([
                    "".join(reasoning_parts),
                    "".join(content_parts),
                ])}]
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
