"""
Bridle — 笼头

输入组装 + 上下文压缩

迁移自原 context.py 的 ContextManager，扩展：
- assemble(): 统一组装 system prompt + memories + history + task → messages
- compress(): 上下文压缩（Stirrups 在 LLM 上下文超限时回调）
- build_tool_result_message(): 工具结果消息构建
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass, field

from agent.truncation_limits import TRUNC_THRESHOLD, TRUNC_HEAD, TRUNC_TAIL
from typing import Optional

from ..memory import budget as memory_budget
from .contracts import RunContext
# 复用 cost_tracker 的更精确 token 估算（区分中英文），避免重复实现
from ..cost_tracker import estimate_tokens

logger = logging.getLogger(__name__)

# 滚动再摘要：将既有摘要作为合成 user 消息前置参与压缩；以下哨兵用于压缩后剔除该合成消息
_PRIOR_SUMMARY_SENTINEL = "\x00__PRIOR_SUMMARY__\x00"
_SUMMARY_PREFIX = "以下是之前对话的摘要:\n"


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算 messages 总 token 数"""
    total = 0
    for msg in messages:
        total += 4  # 每条消息格式开销
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""))
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                total += estimate_tokens(json.dumps(tc, ensure_ascii=False))
    return total


@dataclass
class CompressionConfig:
    """压缩配置"""
    compress_threshold: float = 0.7          # 触发压缩的阈值（占上下文窗口比例）
    keep_recent_turns: int = 3               # 压缩后保留的最近完整消息轮数
    tool_result_summary_chars: int = 500     # 工具结果摘要最大字符数
    # C-review NIT：截断常量单一数据源 agent.truncation_limits（原四处手工复制）
    tool_result_max_chars: int = TRUNC_THRESHOLD  # 单条工具结果最大字符数
    head_chars: int = TRUNC_HEAD                 # 智能截断时保留的头部字符数
    tail_chars: int = TRUNC_TAIL                 # 智能截断时保留的尾部字符数


@dataclass
class Bridle:
    """
    笼头 — 输入组装 + 上下文压缩

    职责：
    1. assemble(): 组装 LLM 输入 messages（system + memories + history + task）
    2. compress(): 上下文压缩（Stirrups 在 LLM 上下文超限时回调）
    3. build_tool_result_message(): 工具结果消息构建
    """

    max_context_tokens: int = 16384
    config: CompressionConfig = field(default_factory=CompressionConfig)

    @property
    def compress_threshold_tokens(self) -> int:
        return int(self.max_context_tokens * self.config.compress_threshold)

    @functools.cached_property
    def budget(self) -> memory_budget.MemoryBudget:
        """本次 run 的统一预算（画像块/注入/摘要/history），按窗口一次性分配。

        Bridle 是 per-run 构造的，所以缓存在这里是安全的 —— 改设置下一条消息才生效。
        """
        return memory_budget.current(self.max_context_tokens)

    def should_compress(self, messages: list[dict]) -> bool:
        return estimate_messages_tokens(messages) > self.compress_threshold_tokens

    @staticmethod
    def _strip_tool_priority(messages: list[dict]) -> list[dict]:
        """剥离 tool 消息的 priority 字段（浅拷贝，避免污染 ctx.messages）

        priority 是非标准 OpenAI 字段，发给严格 vLLM 端点会触发 400。
        build_tool_result_message 保留 priority 供 compress_messages 差异化截断，
        但输出给 LLM 前必须剥离。
        """
        result = []
        for msg in messages:
            if msg.get("role") == "tool" and "priority" in msg:
                m = dict(msg)
                m.pop("priority", None)
                result.append(m)
            else:
                result.append(msg)
        return result

    def assemble(self, ctx: RunContext) -> list[dict[str, str]]:
        """
        组装 LLM 输入 messages

        顺序：system_prompt → memories → history → [上下文摘要] → 当前运行消息

        当开启 compress_on_context_overflow 且上下文溢出时，会原地压缩本次 run
        消息（摘要存入 ctx.metadata["context_summary"]，history 始终保留、不参与
        压缩，避免重复丢弃摘要与近期消息）。因此本方法在溢出时会产生副作用，
        调用方不应假定它是纯只读的。
        """
        if ctx.config.compress_on_context_overflow:
            built = self._build(ctx)
            if self.should_compress(built):
                before = len(ctx.messages)
                self._compress_run_into(ctx, force=False)
                # 若压缩未减少 run 消息（如 history 本身过大），不再重复压缩，避免死循环
                if len(ctx.messages) == before:
                    return self._strip_tool_priority(built)
        return self._strip_tool_priority(self._build(ctx))

    def _build(self, ctx: RunContext) -> list[dict[str, str]]:
        """按当前 ctx 状态组装 messages（含已持久化的摘要），不触发压缩"""
        # 400 约束的单一口径（其它注入点引用这里，别再各写一份）：OpenAI/vLLM 要求
        # system 消息只能出现在开头，插到末尾即报 400。所以 driver.py / reins/guards.py
        # 里末尾注入的纠偏、引导、提示一律走 "user" 角色；而已经躺在 ctx.history /
        # ctx.messages 里的 system，由下面两处循环统一提到顶部。
        system_msgs: list[dict[str, str]] = []
        other_msgs: list[dict[str, str]] = []

        # 1. system prompt
        if ctx.system_prompt:
            system_msgs.append({"role": "system", "content": ctx.system_prompt})

        # 2. 记忆注入（作为 system 消息追加）
        if ctx.memories:
            memory_text = "\n".join(f"- {m}" for m in ctx.memories if m)
            if memory_text:
                system_msgs.append({
                    "role": "system",
                    "content": f"[相关记忆]\n{memory_text}",
                })

        # 3. 历史（始终前置、不参与压缩）
        # 修复：历史中可能含有之前 Agent 运行注入的 system 消息（纠偏提示等），
        # 无差别放入 other_msgs 会导致 system 消息夹在非 system 消息中间，
        # vLLM 返回 400 "System message must be at the beginning"。
        for msg in ctx.history:
            entry = {"role": msg.get("role", "user"), "content": msg.get("content", "")}
            if entry["role"] == "system":
                system_msgs.append(entry)
            else:
                other_msgs.append(entry)

        # 3.5 上下文摘要注入（压缩后保留的早期 run 对话摘要）
        # 置于 history 之后、近期 run 之前：摘要浓缩的是本轮早期 run 消息，
        # 时间上位于 history 之后，顺序符合时间线。角色取 user，见本方法顶部的 400 约束。
        summary = ctx.metadata.get("context_summary")
        if summary:
            other_msgs.append({"role": "user", "content": f"[上下文摘要]\n{summary}"})

        # 4. 当前运行累积的消息（含 task / assistant / tool / 拦截器注入的 system 纠偏消息）
        # 分离 system 消息到顶部，避免 vLLM 400 "System message must be at the beginning"
        for msg in ctx.messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        return system_msgs + other_msgs

    def _compress_run_into(self, ctx: RunContext, force: bool) -> None:
        """
        压缩本次 run 累积消息（ctx.messages），原地更新 ctx：

        - 早期 run 消息（user/assistant/tool）压缩为摘要（_SUMMARY_PREFIX 显式标记），
          存入 ctx.metadata["context_summary"]（滚动再摘要直接替换，尺寸有界）
        - ctx.messages 仅保留近期 run 消息（摘要消息不入 ctx.messages，
          由 assemble/_build 从 metadata 重新注入）
        - run 中的 system 消息（如拦截器 inject_messages 的纠偏提示）属于对话流，
          不并入摘要、不被丢弃，原样保留在 ctx.messages 末尾（紧邻下一轮 LLM 调用）

        history 由 assemble 始终前置，此处不参与压缩，因此不会被重复丢弃。
        """
        if not ctx.messages:
            return
        # 分离 run 中的 system 消息（纠偏提示），不参与压缩、不并入摘要
        run_system = [m for m in ctx.messages if m.get("role") == "system"]
        conv = [m for m in ctx.messages if m.get("role") != "system"]
        if not conv:
            return
        # 滚动再摘要：将既有摘要作为合成 user 消息前置参与本次压缩，使摘要随 run
        # 推进持续浓缩，而非逐轮无界追加（避免长 run 多轮压缩后反超上下文窗口）。
        prior = ctx.metadata.get("context_summary")
        fed_prior = bool(prior)
        if fed_prior:
            prior_text = prior
            if prior_text.startswith(_SUMMARY_PREFIX):
                prior_text = prior_text[len(_SUMMARY_PREFIX):]
            conv = [{"role": "user", "content": _PRIOR_SUMMARY_SENTINEL + prior_text}] + conv
        compressed = self.compress_messages(conv, force=force)
        # C-review M12：从压缩结果中取出"显式标记的摘要消息"（_SUMMARY_PREFIX 前缀，
        # compress_messages 生成的摘要消息 role=user），写入
        # ctx.metadata["context_summary"] — assemble 时经 _build 重新注入。
        # 原实现靠"压缩结果中存在 system 消息"才写元数据，但输入已剥离全部 system、
        # 摘要消息又是 user 角色 → 分支恒死，摘要滞留 ctx.messages 并在下一轮
        # 压缩被再截 200 字符嵌套（历史退化为嵌套切片）。
        recent: list[dict] = []
        summary_text: Optional[str] = None
        for m in compressed:
            c = m.get("content")
            if (m.get("role") != "system"
                    and isinstance(c, str)
                    and c.startswith(_SUMMARY_PREFIX)):
                summary_text = c[len(_SUMMARY_PREFIX):]
                continue
            recent.append(m)
        # 剔除合成前置摘要消息（仅用于本次压缩，不应进入 ctx.messages 或摘要）
        recent = [
            m for m in recent
            if not (isinstance(m.get("content"), str)
                    and m["content"].startswith(_PRIOR_SUMMARY_SENTINEL))
        ]
        if summary_text is not None:
            # 滚动再摘要结果直接替换旧摘要（尺寸有界）。spec §8：按 token 上限截，
            # 不再用 chars/token 反推字符数（旧 cap ≈ 整个窗口，一条摘要就能再撑爆）。
            ctx.metadata["context_summary"] = memory_budget.cap_text_tokens(
                summary_text, self.budget.summary)
        # 保留纠偏 system 消息（置于末尾），其余为近期 run 消息
        ctx.messages = recent + run_system

    async def compress(self, ctx: RunContext, force: bool = False) -> None:
        """
        压缩回调 — Stirrups 在 LLM 上下文超限时调用

        直接压缩本次 run 累积消息（history 由 assemble 重新前置，不参与压缩），
        避免旧实现中摘要被丢弃、近期消息被多截断的上下文丢失问题。
        """
        self._compress_run_into(ctx, force=force)

    def compress_messages(self, messages: list[dict], force: bool = False) -> list[dict]:
        """
        滑动窗口压缩 — 迁移自原 ContextManager.compress_if_needed

        保留 system + 最近 N 轮，早期消息替换为摘要

        这里**不得**剥 priority：本函数的返回值会被 `_compress_run_into` 赋回
        `ctx.messages`（内存态），而差异化截断/摘要正要读这个字段。剥字段的职责属于
        唯一的出网边界 `assemble()`（Stirrups 的压缩重试也经 set_messages_rebuilder
        =assemble 重取，见 harness.py 的装配处），原先在这里顺手剥掉等于把压缩器下一轮
        要用的输入改坏：第一次压缩后 high 结果被当 normal 砍到 1000、low 噪声被当
        normal 留 1000 而不再省略，优先级机制静默失效。
        """
        if not force and not self.should_compress(messages):
            return list(messages)

        logger.info(
            "上下文压缩: %d tokens > %d 阈值",
            estimate_messages_tokens(messages),
            self.compress_threshold_tokens,
        )

        system_msgs = [m for m in messages if m.get("role") == "system"]
        conv_msgs = [m for m in messages if m.get("role") != "system"]

        keep_count = self.config.keep_recent_turns * 3
        if not force and len(conv_msgs) <= keep_count:
            return list(messages)

        # force=True 时按优先级差异化截断超长 tool_result
        # high：保持完整(4000)，normal：1000，low：200 且超限直接省略
        if force and len(conv_msgs) <= keep_count:
            changed = False
            for i, msg in enumerate(conv_msgs):
                if msg.get("role") != "tool":
                    continue
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue
                priority = msg.get("priority", "normal")
                if priority == "high":
                    max_chars = self.config.tool_result_max_chars
                elif priority == "low":
                    max_chars = 200
                else:
                    max_chars = 1000
                if len(content) <= max_chars:
                    continue
                if priority == "low":
                    new_content = "[low-priority result omitted]"
                else:
                    new_content = self._truncate_head_tail(content, max_chars)
                conv_msgs[i] = {**msg, "content": new_content}
                changed = True
            if changed:
                return system_msgs + conv_msgs
            return list(messages)

        # 按"原子段"切分，保证 assistant(tool_calls) 与其 tool 结果不被窗口边界拆散
        segments = self._split_segments(conv_msgs)
        recent_msgs: list[dict] = []
        for seg in reversed(segments):
            recent_msgs = seg + recent_msgs
            if len(recent_msgs) >= keep_count:
                break
        old_count = len(conv_msgs) - len(recent_msgs)
        if old_count <= 0:
            # 没有足够旧消息可压缩（总消息数 <= keep_count），保持原样、不生成空摘要
            return system_msgs + conv_msgs
        old_msgs = conv_msgs[:old_count]

        # 早期消息压缩为一条摘要
        summary_parts = []
        prior_part: Optional[str] = None
        for msg in old_msgs:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                if content.startswith(_PRIOR_SUMMARY_SENTINEL):
                    # C-review M12：先前摘要按上限完整回喂（与 metadata 存储的 cap 一致），
                    # 滚动再摘要得以浓缩完整旧摘要；旧实现只留 200 字符，
                    # 每一代摘要都丢失上一代绝大部分信息（嵌套截断退化）
                    prior_capped = memory_budget.cap_text_tokens(
                        content[len(_PRIOR_SUMMARY_SENTINEL):], self.budget.summary)
                    prior_part = f"[user] {prior_capped}"
                    continue
                # tool 消息按 priority 差异化摘要长度
                if role == "tool":
                    priority = msg.get("priority", "normal")
                    if priority == "high":
                        summary_chars = 800
                    elif priority == "low":
                        summary_chars = 80
                    else:
                        summary_chars = 200
                    if priority == "low" and len(content) > 80:
                        content = "[low-priority result omitted]"
                    else:
                        content = content[:summary_chars]
                else:
                    content = content[:200]
                summary_parts.append(f"[{role}] {content}")
            elif msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    name = tc.get("function", {}).get("name", "?")
                    summary_parts.append(f"[assistant] 调用工具: {name}")
        # C-review M12：先前摘要置于 parts 末尾 — 超 cap 的尾部截断保住它
        # （及最近的旧消息），被裁掉的是最旧的 200 字符切片（相关性最低）
        if prior_part is not None:
            summary_parts.append(prior_part)

        # 摘要作为注入内容置于近期消息之前，但角色仍取 user（400 约束见 _build 顶部）
        summary_text = "以下是之前对话的摘要:\n" + "\n".join(summary_parts)
        summary_msg = {"role": "user", "content": summary_text}

        return system_msgs + [summary_msg] + recent_msgs

    def _split_segments(self, conv: list[dict]) -> list[list[dict]]:
        """
        将对话流切分为原子段，使 tool_calls 与其 tool 结果同段：

        - user / assistant(无 tool_calls) ：各自独立成段
        - assistant(tool_calls) + 紧随其后的所有 tool 结果 ：合并为一段
        - 孤立 tool 消息（无前置 assistant）：单独成段（防御性）

        滑动窗口只在段边界切分，避免产生孤儿 tool 消息（违反 OpenAI
        「tool 消息必须有前置 tool_call」约束，会触发 400 错误）。
        """
        segments: list[list[dict]] = []
        i = 0
        n = len(conv)
        while i < n:
            msg = conv[i]
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                seg = [msg]
                i += 1
                while i < n and conv[i].get("role") == "tool":
                    seg.append(conv[i])
                    i += 1
                segments.append(seg)
            else:
                segments.append([msg])
                i += 1
        return segments

    def _truncate_head_tail(self, content: str, max_chars: int) -> str:
        """头尾保留截断：头部 head_chars + 中间省略提示 + 尾部 tail_chars，结果不超 max_chars

        省略提示本身也占预算。原实现只保证 `head + tail ≤ max_chars`，拼上标记后结果
        比上限长（实测 max_chars=1000 给出 1024）—— 而调用方传进来的 max_chars 就是
        "这条工具结果最多占多少上下文"的预算，被静默击穿等于白设。
        标记里只有一个数字会变动，按其最长形态预留固定余量即可，不需要迭代收敛。
        """
        if len(content) <= max_chars:
            return content
        marker_reserve = 64
        room = max(1, max_chars - marker_reserve)
        head = min(self.config.head_chars, room * 2 // 3)
        tail = min(self.config.tail_chars, room - head)
        omitted = len(content) - head - tail
        return content[:head] + f"\n... [已截断，省略中间 {omitted} 字符]\n" + content[-tail:]

    def truncate_tool_result(self, result: str) -> str:
        """
        截断工具结果。

        - 未超阈值时原样返回
        - 超阈值时采用头尾保留：头部 head_chars + 中间省略提示 + 尾部 tail_chars
        """
        if len(result) <= self.config.tool_result_max_chars:
            return result
        head = self.config.head_chars
        tail = self.config.tail_chars
        omitted = f"\n... [已截断，原始 {len(result)} 字符，保留头部 {head} + 尾部 {tail}]\n"
        return result[:head] + omitted + result[-tail:]

    def build_tool_result_message(self, tool_call_id: str, result: str, priority: str = "normal") -> dict:
        """构建 OpenAI tool 角色消息"""
        summary = self.truncate_tool_result(result)
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": summary,
            "priority": priority,  # 供 compress_messages 读取
        }
