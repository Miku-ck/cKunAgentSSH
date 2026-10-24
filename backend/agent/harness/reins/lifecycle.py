"""Agent 生命周期拦截器：技能注入与反思触发/终态回收 — 拆分自原 reins.py。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from ..contracts import ReinDecision, ReinPoint, RunContext, TraceEvent
from .clarification import release_clarification_run

if TYPE_CHECKING:
    from .gate import PermissionGate
from prompts import SSH_MODE

logger = logging.getLogger(__name__)

@dataclass
class SkillRein:
    """
    技能注入拦截器 — 在 AGENT_START 时注入技能上下文到 system_prompt

    运行时机：AGENT_START（在 driver 主循环之前，确保首轮 assemble() 即包含技能）

    流程：
    1. 取 ctx.skill_ids（前端显式选择）+ 按 ctx.task 触发词自动匹配的技能
    2. 通过 SkillRegistry.build_skill_catalog() 生成可用技能目录索引（不含正文）
    3. 对显式选择的技能追加 build_skill_preload() 简短提示（描述 + 正文开头片段）
    4. 追加到 ctx.system_prompt（in-place 修改）
    5. LLM 可通过 load_skill 工具按需加载完整技能内容

    选择 AGENT_START 而非 PRE_LLM_CALL 的原因：
    - AGENT_START 在 assemble() 之前运行，首轮 LLM 调用即可包含技能
    - PRE_LLM_CALL 在 assemble() 之后运行，首轮会缺失技能上下文
    - AGENT_START 仅运行一次，无需 _injected 标志防重复
    """
    point: ReinPoint = field(default=ReinPoint.AGENT_START, init=False)
    priority: int = field(default=5, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        try:
            from skill_registry import get_skill_registry
            registry = get_skill_registry()

            # 显式选择的技能 + 自动匹配的技能（去重）
            # auto_skill_match=False（前端"自动技能匹配"关闭）时跳过触发词匹配，
            # 仅注入用户显式选择的技能
            explicit_ids = set(ctx.skill_ids or [])
            auto_ids: set[str] = set()
            if ctx.auto_skill_match:
                # D 修复（事件循环阻塞）：registry 内部含同步 db 查询，放线程池执行
                auto_matched = await asyncio.to_thread(
                    registry.match_skills, ctx.task, SSH_MODE, max_results=3
                )
                auto_ids = {s['id'] for s in auto_matched}

            all_ids = list(explicit_ids | auto_ids)
            if not all_ids:
                return ReinDecision()

            parts = []
            catalog = await asyncio.to_thread(registry.build_skill_catalog, all_ids, SSH_MODE)
            if catalog:
                parts.append(catalog)

            if explicit_ids:
                preload = await asyncio.to_thread(
                    registry.build_skill_preload, list(explicit_ids), SSH_MODE
                )
                if preload:
                    parts.append(preload)

            if parts:
                ctx.system_prompt = (ctx.system_prompt or "") + "\n\n" + "\n\n".join(parts)
                logger.info(
                    "SkillRein 注入技能目录 (total=%d, explicit=%d, auto=%d)",
                    len(all_ids), len(explicit_ids), len(auto_ids),
                )
        except Exception as e:
            logger.warning("技能注入失败: %s", e, exc_info=True)

        return ReinDecision()


@dataclass
class ReflectionRein:
    """
    反思触发拦截器 — AGENT_COMPLETE / AGENT_FAILED 时推送 reflection_request trace 事件

    设计目的：
    - 原实现中 routes/agent.py 在 done 事件中异步触发 _run_reflection_and_evolution，
      但该触发点是 SSE 流处理层，与 Harness 内部状态耦合较松，且 AGENT_FAILED 时
      不一定走 done 分支（可能是 error 事件终止），导致失败运行经常错过反思。
    - 本拦截器在 Harness 内部的 AGENT_COMPLETE / AGENT_FAILED ReinPoint 直接触发，
      保证无论成功/失败/超上限都会推送 reflection_request 事件，路由层只需监听该事件
      即可触发异步反思任务。

    注意：
    - 拦截器本身不执行反思（避免阻塞 Harness 主循环），只发事件。
    - 实际反思逻辑仍由 routes/agent.py 的 _run_reflection_and_evolution 异步执行。
    - 事件 payload 含 run_id / mode / final_state / iterations / tool_calls_summary，
      供路由层直接使用，无需重新从 ctx 提取。
    - 运行终止时自动清理该 run_id 绑定的 once 授权池（P1 竞态修复）。
    """
    point: ReinPoint = field(default=ReinPoint.AGENT_COMPLETE)
    priority: int = field(default=5, init=False)
    permission_gate: Optional["PermissionGate"] = field(default=None, repr=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        # P1 竞态修复：运行终止时清理该 run 绑定的 once 授权池
        #（AGENT_COMPLETE / AGENT_FAILED 均为终态，once 授权不应跨 run 残留）
        if self.permission_gate is not None:
            try:
                released = self.permission_gate.cleanup_run_once(ctx.run_id)
                if released:
                    logger.debug("ReflectionRein: 清理 run %s 的 %d 条 once 授权",
                                 ctx.run_id, released)
            except Exception:
                logger.warning("ReflectionRein: once 清理异常（不阻塞反思）", exc_info=True)
            # 问题 4：同步清理拒绝命令记录
            try:
                denied_count = self.permission_gate.cleanup_run_denied(ctx.run_id)
                if denied_count:
                    logger.debug("ReflectionRein: 清理 run %s 的 %d 条拒绝记录",
                                 ctx.run_id, denied_count)
            except Exception:
                logger.warning("ReflectionRein: denied 清理异常（不阻塞反思）", exc_info=True)

        # P1-6: run 终态回收澄清状态 — 未答完的题随 run 作废，不留到下个 run
        try:
            if release_clarification_run(ctx.run_id):
                logger.debug("ReflectionRein: 回收 run %s 的澄清状态", ctx.run_id)
        except Exception:
            logger.warning("ReflectionRein: 澄清回收异常（不阻塞反思）", exc_info=True)

        # 统计工具调用摘要（避免传完整 tool_calls_log 进 payload，太重）
        tool_calls_log = ctx.tool_calls_log or []
        tool_names = [tc.get("tool", "?") for tc in tool_calls_log]
        failed_tools = [
            tc.get("tool", "?") for tc in tool_calls_log if not tc.get("success", True)
        ]
        # 成本信息（如有 CostTracker 写入 metadata）
        cost_info = ctx.metadata.get("cost_snapshot") if ctx.metadata else None

        payload = {
            "run_id": ctx.run_id,
            "mode": SSH_MODE,
            "final_state": ctx.state.value if ctx.state else "unknown",
            "iterations": ctx.iteration,
            "max_iterations": ctx.config.max_iterations,
            "tool_calls_count": len(tool_calls_log),
            "unique_tools": list(set(tool_names)),
            "failed_tools": list(set(failed_tools)),
            "has_warnings": bool(ctx.metadata.get("warnings")) if ctx.metadata else False,
            "has_cost": cost_info is not None,
        }
        return ReinDecision(
            events=[TraceEvent(
                kind="reflection_request",
                source="reflection_rein",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                payload=payload,
            )],
        )


# ── 事后复盘（方案 Phase 2：认知层闭环飞轮）──────────────────────────────────
# 复盘材料全部取自 ctx（task / state_history / tool_calls_log / errors / warnings /
# 最终答复），零额外采集；产物写入 memories 表 kind='postmortem'，不建独立页面。

# 复盘记忆的 kind 值。刻意用常量传参：harness 下的 kind="…" 字面量会被
# test_trace_projection_keys 当成 Wire 事件类型扫描（记忆 kind 不是事件）。
POSTMORTEM_MEMORY_KIND = "postmortem"

_POSTMORTEM_TIMEOUT_S = 15.0   # 与 bridle 生成式摘要同一超时口径
_POSTMORTEM_MAX_TOKENS = 700
_POSTMORTEM_TASKS: set = set()  # 后台任务强引用：asyncio 对无引用任务的 GC 不可预期

_POSTMORTEM_PROMPT = (
    "以下是一次 AI 运维智能体执行任务的完整记录。请生成一份简明的中文事后复盘，"
    "按以下结构输出：\n"
    "任务：一句话概括做了什么\n"
    "执行过程：关键步骤与观测发现\n"
    "关键决策：为什么这样做\n"
    "结果：最终状态与遗留问题\n"
    "经验教训：可复用的经验或下次要避免的坑\n"
    "只输出复盘正文，不要任何开场白或解释。\n\n"
    "[执行记录]\n{material}"
)


def _postmortem_material(ctx: RunContext) -> str:
    """拼装复盘材料。长 run 的工具序列/答复/错误各段截断，防 prompt 失控。"""
    calls = []
    for tc in (ctx.tool_calls_log or [])[:60]:
        try:
            args_str = json.dumps(tc.get("args") or {}, ensure_ascii=False)[:120]
        except Exception:
            args_str = str(tc.get("args"))[:120]
        succ = tc.get("success")
        verdict = "成功" if succ else ("失败" if succ is False else "未记录")
        calls.append(f"- {tc.get('tool', '?')} {args_str} -> {verdict}")
    states = " → ".join(
        str(h.get("state", "?")) for h in (ctx.state_history or [])[-20:]
    )
    final_content = ""
    for msg in reversed(ctx.messages or []):
        if (msg.get("role") == "assistant"
                and isinstance(msg.get("content"), str)
                and msg["content"].strip()):
            final_content = msg["content"][:800]
            break
    parts = [f"[任务]\n{ctx.task[:500]}"]
    if states:
        parts.append(f"[状态时间线]\n{states}")
    if calls:
        parts.append("[工具调用序列]\n" + "\n".join(calls))
    if final_content:
        parts.append(f"[最终答复]\n{final_content}")
    if ctx.errors:
        parts.append("[错误]\n" + "\n".join(str(e)[:200] for e in ctx.errors[-10:]))
    if ctx.warnings:
        parts.append("[警告]\n" + "\n".join(str(w)[:200] for w in ctx.warnings[-10:]))
    return "\n\n".join(parts)


async def _generate_postmortem(ctx: RunContext) -> None:
    """后台生成复盘并写入 memories（kind='postmortem'）。

    降级铁律（与 bridle._generate_summary 同款）：整体 try/except 全包，LLM 失败/
    超时/DB 异常一律 warning 后返回 —— run 已经结束，复盘失败不能向任何存活
    调用方抛出异常。
    """
    try:
        from agent.harness.llm_client import StreamLLMClient

        llm = StreamLLMClient(base_url=ctx.llm_base_url, api_key=ctx.llm_api_key)
        out = await asyncio.wait_for(
            llm.call(
                messages=[{
                    "role": "user",
                    "content": _POSTMORTEM_PROMPT.format(material=_postmortem_material(ctx)),
                }],
                tools=[],
                model=ctx.llm_model,
                temperature=0.3,
                max_tokens=_POSTMORTEM_MAX_TOKENS,
                enable_thinking=False,
            ),
            timeout=_POSTMORTEM_TIMEOUT_S,
        )
        text = (getattr(out, "content", "") or "").strip()
        if not text:
            return
        # 作用域固定 global：会话 ID（ssh-N）跨进程回收且复用，host:{sid} 对持久
        # 知识不可达、跨重启会串机；复盘正文里本就带任务与命令细节
        from agent.memory import mstore

        await asyncio.to_thread(
            mstore.add_memory,
            kind=POSTMORTEM_MEMORY_KIND,
            content=text,
            scope="global",
            title=f"复盘：{ctx.task[:50]}",
            source="agent",
            importance=0.6,
            agent_run_id=ctx.run_id,
        )
        logger.info("事后复盘已沉淀: run=%s", ctx.run_id)
    except Exception as e:  # noqa: BLE001 — 降级铁律，见 docstring
        logger.warning("事后复盘生成失败（静默跳过）: %s", e)


@dataclass
class PostmortemRein:
    """
    事后复盘拦截器 — AGENT_COMPLETE / AGENT_FAILED 时后台生成复盘写入记忆库

    触发条件（三者同时满足）：
    - ctx.config.postmortem_enabled（AgentConfig 默认 False：复盘是额外 LLM 调用，
      成本敏感，用户显式开启）
    - ctx.tool_calls_log 非空（纯聊天 run 没有可复盘的执行过程）
    - ctx 带 LLM 配置（base_url / model，Bridle 组装期注入）

    生成走 asyncio.create_task 后台执行：本拦截器若同步 await 15s LLM 调用，
    AGENT_COMPLETE / AGENT_FAILED 注入点会把 run 终态与 SSE 收尾整体卡住 15 秒。
    任务以模块级集合持有强引用，防止被事件循环提前 GC。
    """
    point: ReinPoint = field(default=ReinPoint.AGENT_COMPLETE)
    priority: int = field(default=20, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not getattr(ctx.config, "postmortem_enabled", False):
            return ReinDecision()
        if not (ctx.tool_calls_log or []):
            return ReinDecision()
        if not (ctx.llm_base_url and ctx.llm_model):
            return ReinDecision()
        t = asyncio.create_task(_generate_postmortem(ctx))
        _POSTMORTEM_TASKS.add(t)
        t.add_done_callback(_POSTMORTEM_TASKS.discard)
        return ReinDecision()
