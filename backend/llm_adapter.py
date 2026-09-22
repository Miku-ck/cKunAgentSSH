"""
LLM 适配器 — OpenAI 兼容异步流式客户端
对接 vLLM 服务 (OpenAI-compatible API)

支持：
- 流式文本输出 (content + reasoning)
- Function Calling / Tool Calls（流式分片累积）
"""
import json
import logging
import httpx
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncGenerator

import openai

logger = logging.getLogger(__name__)

# H5 修复：客户端复用池，按 (base_url, api_key) 缓存 AsyncOpenAI 实例
# 避免每次 chat_stream 都新建客户端导致连接池泄漏
# LRU 淘汰：频繁切换模型配置时限制缓存项数量，防止连接池无限增长
_CLIENT_CACHE_MAX = 8  # 最大缓存客户端数
_client_cache: "OrderedDict[tuple[str, str], openai.AsyncOpenAI]" = OrderedDict()
# C-review MINOR-2：每个缓存客户端的进行中请求计数（key 同 _client_cache）。
# LRU 淘汰时若计数 > 0（还有流在占用该 httpx 连接池），跳过 aclose，
# 避免掐断进行中的流；该客户端将由 GC 回收，或下次同 key 重建时复用。
_client_in_flight: dict[tuple[str, str], int] = {}


def _normalize_system_first(messages: list[dict]) -> list[dict]:
    """部分 OpenAI 兼容服务要求 system 消息必须位于消息列表最前。

    Agent 组装路径（bridle._build）会把 ctx.messages 原样追加到末尾，而
    上下文压缩保留的 system 纠偏消息恰好位于 ctx.messages 末尾；chat 路由的
    history 也可能含有持久化的 system 消息。此处统一重排：所有 system 消息
    合并为单条置顶（保持相对顺序），其余消息保持原序。

    合并而非仅前置的原因：部分 vLLM 版本要求 system 消息"恰好一条"——
    即便全部排在开头区域，出现第二条 system 仍报
    400 "System message must be at the beginning"。
    """
    systems = [m for m in messages if m.get("role") == "system"]
    if not systems:
        return messages
    others = [m for m in messages if m.get("role") != "system"]
    if len(systems) == 1:
        return [systems[0]] + others
    # 多条 system 合并为一条（换行分隔，保持相对顺序）
    merged_content = "\n\n".join(
        m.get("content", "") for m in systems if m.get("content")
    )
    merged: dict = {"role": "system", "content": merged_content}
    # 保留首条 system 的其他字段（如 name），避免信息丢失
    for k, v in systems[0].items():
        if k not in ("role", "content"):
            merged.setdefault(k, v)
    return [merged] + others


async def _get_client(base_url: str, api_key: str) -> openai.AsyncOpenAI:
    """获取共享的 AsyncOpenAI 客户端（LRU 缓存，最多保留 _CLIENT_CACHE_MAX 个）"""
    key = (base_url, api_key or "EMPTY")
    client = _client_cache.get(key)
    if client is not None:
        # 命中：移到末尾（最近使用）
        _client_cache.move_to_end(key)
        return client
    # 未命中：创建新客户端
    client = openai.AsyncOpenAI(
        api_key=api_key or "EMPTY",
        base_url=base_url,
        # 修复：分别配置连接超时和读取超时，流式输出时读取超时可能过短
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
    )
    _client_cache[key] = client
    # 超容量时淘汰最久未使用的
    if len(_client_cache) > _CLIENT_CACHE_MAX:
        evicted_key, evicted = _client_cache.popitem(last=False)
        # 修复：显式关闭被淘汰客户端的 httpx 连接池，避免连接泄漏
        # C-review MINOR-2：仍有进行中的流时不能 aclose（会掐断其他请求的
        # HTTP 流）— 跳过关闭，依赖 GC 回收；计数归零前的淘汰一律不关
        if _client_in_flight.get(evicted_key, 0) > 0:
            logger.warning(
                "LRU 淘汰客户端 %s 时仍有 %d 个进行中请求，跳过 aclose（依赖 GC）",
                evicted_key[0], _client_in_flight.get(evicted_key, 0),
            )
            # 计数原样留着，由这些请求各自的 finally 递减。原实现在这里 pop：
            # 计数按 (base_url, api_key) 而非按客户端键控，pop 等于把在途请求清零 —
            # 同 key 新建的客户端 B 只计到自己的 1，A 的流随后把计数扣成 0，
            # B 被淘汰时就走了「无在途请求」分支被 aclose，掐断 B 仍在跑的流。
        else:
            try:
                if hasattr(evicted, '_client') and hasattr(evicted._client, 'aclose'):
                    await evicted._client.aclose()
            except Exception:
                pass  # 关闭失败时依赖 GC 回收
            _client_in_flight.pop(evicted_key, None)
    return client


@dataclass
class ToolCall:
    """工具调用（解析后的完整结构）"""
    id: str = ""
    name: str = ""
    arguments: str = ""  # JSON 字符串

    def to_dict(self) -> dict:
        """转为 OpenAI 消息格式"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }

    def parsed_args(self) -> dict:
        """解析 arguments JSON，失败时返回空 dict"""
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            logger.warning("tool_call arguments JSON 解析失败: %s", self.arguments[:200])
            return {}


@dataclass
class StreamChunk:
    """流式输出块"""
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # token 用量（仅最后一个 chunk 通常携带，来自 stream_options include_usage）
    prompt_tokens: int = 0
    completion_tokens: int = 0


async def chat_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
    # 默认为 None：不传 max_tokens，由 vLLM 自行决定输出上限。
    max_tokens: int | None = None,
    top_p: float = 0.9,
    enable_thinking: bool = False,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    # 思考阶段 token 上限（仅 thinking 模型生效，限制 reasoning_content 长度）
    # None 表示不限制；vLLM/Qwen3 通过 chat_template_kwargs.thinking_budget 传递
    reasoning_max_tokens: int | None = None,
) -> AsyncGenerator[StreamChunk, None]:
    """流式聊天补全（公开入口）

    C-review MINOR-2：包装 _chat_stream_impl，维护客户端 in-flight 计数 —
    LRU 淘汰期间有流在用则不 aclose 连接池。内层生成器在此处统一 aclose，
    确保消费者断连时底层 HTTP 流确定性释放（不依赖 GC 收尾）。
    """
    _key = (base_url, api_key or "EMPTY")
    _client_in_flight[_key] = _client_in_flight.get(_key, 0) + 1
    inner = _chat_stream_impl(
        base_url, api_key, model, messages,
        temperature=temperature, max_tokens=max_tokens, top_p=top_p,
        enable_thinking=enable_thinking, tools=tools, tool_choice=tool_choice,
        reasoning_max_tokens=reasoning_max_tokens,
    )
    try:
        async for chunk in inner:
            yield chunk
    finally:
        try:
            await inner.aclose()
        except Exception:
            pass
        _client_in_flight[_key] = max(0, _client_in_flight.get(_key, 0) - 1)


async def _chat_stream_impl(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int | None = None,
    top_p: float = 0.9,
    enable_thinking: bool = False,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    reasoning_max_tokens: int | None = None,
) -> AsyncGenerator[StreamChunk, None]:
    """
    流式聊天补全（实际实现，in-flight 计数由外层 chat_stream 包装维护）

    Args:
        base_url: vLLM 服务地址 (如 http://192.168.150.40:9030/v1)
        api_key: API 密钥 (vLLM 通常为 "EMPTY")
        model: 模型名称
        messages: OpenAI 格式消息列表
        temperature: 采样温度
        max_tokens: 最大输出 token，None 表示不传该参数
        top_p: 核采样参数
        enable_thinking: 是否启用思考模式 (Thinking 模型)
        tools: OpenAI function calling 格式的工具定义列表
        tool_choice: 工具选择策略 ("auto" / "none" / "required")
        reasoning_max_tokens: thinking 阶段 token 上限（仅 thinking 模型生效）
            通过 vLLM chat_template_kwargs.thinking_budget 传递，限制 reasoning_content 长度
            None 表示不限制；用于意图识别后的思考预算控制
    """
    # H5 修复：复用共享客户端
    client = await _get_client(base_url, api_key)

    extra_body: dict = {}
    if enable_thinking:
        # chat_template_kwargs 会被 vLLM 注入到 chat template 渲染上下文
        # Qwen3 系列支持 enable_thinking（布尔）+ thinking_budget（int，思考 token 上限）
        tpl_kwargs: dict = {"enable_thinking": True}
        if reasoning_max_tokens is not None and reasoning_max_tokens > 0:
            tpl_kwargs["thinking_budget"] = reasoning_max_tokens
        extra_body["chat_template_kwargs"] = tpl_kwargs

    kwargs: dict = {
        "model": model,
        # system 消息统一前置，避免部分服务端 400 拒绝
        "messages": _normalize_system_first(messages),
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if extra_body:
        kwargs["extra_body"] = extra_body
    if tools:
        kwargs["tools"] = tools
        # 仅当 tool_choice 非 "auto" 默认值时才显式传入；
        # "auto" 是 OpenAI 默认行为，但 vLLM 若未启用 --enable-auto-tool-choice
        # 会因收到 tool_choice="auto" 而返回 400 错误。
        # 不传该字段时 vLLM 会按服务端默认行为处理（仍能识别 tools）。
        if tool_choice and tool_choice != "auto":
            kwargs["tool_choice"] = tool_choice

    try:
        # 诊断日志：打印角色序列，用于确认 system 合并修复已生效
        # （v1.1 = _normalize_system_first 合并版；若仍见 400 且日志缺失，说明运行的是旧进程）
        logger.debug(
            "[llm_adapter v1.1] %s roles=%s",
            model,
            [m.get("role") for m in kwargs["messages"]],
        )
        stream = await client.chat.completions.create(**kwargs)
    except openai.BadRequestError as e:
        # vLLM 未启用 --enable-auto-tool-choice 时，传 tools 也可能被拒
        # 降级：移除 tools/tool_choice 后重试（纯文本对话）
        err_msg = str(e).lower()
        if tools and ("tool" in err_msg or "tool_choice" in err_msg):
            logger.warning(
                "vLLM 拒绝 tool 调用（可能未启用 --enable-auto-tool-choice），"
                "降级为无工具模式重试: %s", e
            )
            kwargs.pop("tools", None)
            kwargs.pop("tool_choice", None)
            stream = await client.chat.completions.create(**kwargs)
        else:
            raise

    # 流式 tool_calls 分片累积：index → {id, name, arguments_buffer}
    accumulated: dict[int, dict] = {}

    # B-9 修复：内层 openai 流式迭代包 try/finally — 消费者提前 break/断连时
    # 显式 aclose 释放底层 HTTP 连接（原实现依赖 GC，连接可能长期挂起）
    try:
        async for chunk in stream:
            # OpenAI 流式: 最后一个 chunk 没有 choices 但携带 usage
            if not chunk.choices:
                usage = getattr(chunk, "usage", None)
                if usage:
                    yield StreamChunk(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    )
                continue
            delta = chunk.choices[0].delta

            # 解析 tool_calls 分片（流式中 arguments 是逐字符到达的）
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    # H6 修复：tc_delta.index 可能为 None，使用 0 作为兜底
                    idx = tc_delta.index if tc_delta.index is not None else 0
                    if idx not in accumulated:
                        accumulated[idx] = {
                            "id": tc_delta.id or "",
                            "name": tc_delta.function.name if tc_delta.function else "",
                            "arguments": "",
                        }
                    if tc_delta.id:
                        accumulated[idx]["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        accumulated[idx]["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        accumulated[idx]["arguments"] += tc_delta.function.arguments

            # vLLM --reasoning-parser qwen3 返回 'reasoning' 字段
            reasoning = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or ""
            )
            content = delta.content or ""

            if content or reasoning:
                yield StreamChunk(content=content, reasoning=reasoning)
    finally:
        try:
            await stream.aclose()
        except Exception:
            pass

    # 流结束后，如果有完整的 tool_calls，yield 一个合并后的 chunk
    if accumulated:
        tool_calls = [
            ToolCall(
                id=tc["id"] or f"call_{idx}",
                name=tc["name"],
                arguments=tc["arguments"],
            )
            for idx, tc in sorted(accumulated.items())
        ]
        yield StreamChunk(tool_calls=tool_calls)
