"""任务完成度裁决，作为 Driver 的可测试扩展。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .contracts import RunContext, TraceEvent

if TYPE_CHECKING:
    from .harness import Harness

logger = logging.getLogger(__name__)


class CompletionGuardMixin:
    async def _completion_guard(
        self,
        ctx: RunContext,
        harness: "Harness",
        final_text: str,
        iteration: int,
        next_round_budget: bool,
        intention_level: str,
    ) -> tuple[bool, list[TraceEvent]]:
        """
        假完成强校验 — 只覆盖无 workflow goal 的 run。

        有 goal 的 run 走上方 goal 自检锚（_goal_self_checked）。无 goal 时，
        模型宣告完成的唯一凭据是「它自己不调工具了」——答非所问也会被放行。
        这里用一次轻量 LLM 裁决把钉扎任务原文与最终答案对表：

        - FAIL → 注入纠偏 user 消息 + return True（调用方 continue 续跑）；
          每 run 至多一次（metadata 标记），第二次放行防死循环，且续跑天然受
          max_iterations / 成本熔断 / 墙钟三重兜底
        - PASS / 超时 / 异常 / 解析失败 → fail-open 放行（绝不阻断收尾，
          口径同生成式摘要静默降级）

        触发门槛：任务非空、意图等级 ≥ moderate（trivial/simple 不值得多花
        一次调用）、还有下一轮预算（最后一轮 continue 会落 loop-else 误判
        MAX_REACHED，同 goal 自检锚的教训）、最终答案非空。
        裁决期间临时摘掉 chunk 回调：PASS/FAIL 片段不得流进用户对话。
        """
        if not ctx.config.completion_guard_enabled:
            return False, []
        if not next_round_budget:
            return False, []
        if not final_text or not final_text.strip():
            return False, []
        if ctx.metadata.get("_completion_guarded"):
            return False, []
        task_text = (ctx.task or "").strip()
        if not task_text:
            return False, []
        if intention_level not in ("moderate", "complex"):
            return False, []
        try:
            # 置位在裁决前：异常/重入下宁可少校验一次，不可双发注入
            ctx.metadata["_completion_guarded"] = True
            return await self._completion_guard_judge(
                ctx, harness, task_text, final_text, iteration)
        except Exception as e:  # noqa: BLE001 — fail-open 铁律
            # 安全裁决异常时 fail-open 是既定口径，但必须 ERROR 留痕 + 前端可见事件，
            # 不能让用户以为完成度已被校验。控制流不变（仍放行）。
            logger.error("[SECURITY][fail-open] CompletionGuard 异常，fail-open 放行: %s",
                         e, exc_info=e)
            emit = (ctx.metadata or {}).get("emit_event")
            if emit is not None:
                try:
                    await emit(TraceEvent(
                        kind="warning", source="driver.completion_guard",
                        run_id=ctx.run_id,
                        payload={"code": "security_fail_open",
                                 "message": "安全裁决异常，已按保守策略放行（详见日志）"},
                    ))
                except Exception:  # noqa: BLE001 — 事件投递失败不能反过来打断放行
                    pass
            return False, []

    async def _completion_guard_judge(
        self,
        ctx: RunContext,
        harness: "Harness",
        task_text: str,
        final_text: str,
        iteration: int,
    ) -> tuple[bool, list[TraceEvent]]:
        """执行裁决调用并解析结果（PASS 放行 / FAIL 纠偏续跑 / 异常向上抛）"""
        client = harness.stirrups.llm_client
        prompt = (
            "你是任务完成度裁决器。判断下面的「最终答案」是否实质覆盖了「原始任务」"
            "的全部核心要求。宁可保守：只答了一部分、答非所问、把计划当结果交付，"
            "都算未完成。输出格式：第一行只写 PASS 或 FAIL；若 FAIL，"
            "第二行用一句话说明缺了什么。\n\n"
            f"[原始任务]\n{task_text[:2000]}\n\n"
            f"[最终答案]\n{final_text[:4000]}"
        )
        prev_cb = getattr(client, "_on_chunk", None)
        setattr(client, "_on_chunk", None)  # 裁决片段不得流入用户对话
        try:
            out = await asyncio.wait_for(
                client.call(
                    messages=[{"role": "user", "content": prompt}],
                    tools=[],
                    model=ctx.llm_model,
                    temperature=0.1,
                    max_tokens=64,
                    enable_thinking=False,
                ),
                timeout=ctx.config.completion_guard_timeout_s,
            )
        finally:
            setattr(client, "_on_chunk", prev_cb)
        raw = (getattr(out, "content", "") or "").strip()
        if not raw:
            return False, []
        lines = raw.splitlines()
        verdict = lines[0].strip().upper()
        if verdict.startswith("PASS"):
            return False, []
        if not verdict.startswith("FAIL"):
            # 输出不合约定 → fail-open（口径同 auto_approve 的 _parse_verdict）。
            # ERROR 留痕：安全裁决未按预期产出本身就是需要关注的异常信号。
            logger.error("[SECURITY][fail-open] CompletionGuard 输出不合约定，fail-open 放行: %r",
                         verdict)
            return False, []
        gap = lines[1].strip() if len(lines) > 1 else ""
        reason = gap or "最终答案未完整覆盖任务要求"
        ctx.messages.append({
            "role": "user",
            "content": (
                f"[系统拦截] 完成度校验未通过：{reason}。"
                "请补充缺失部分后重新给出完整结果；若确有客观限制无法完成，"
                "请在结果中明确说明已完成与未完成的边界。"
            ),
        })
        events = [TraceEvent(
            kind="warning",
            source="driver.completion_guard",
            run_id=ctx.run_id,
            iteration=max(0, iteration),
            payload={
                "message": f"完成度校验未通过，已要求 Agent 补充：{reason[:200]}",
            },
        )]
        logger.info(
            "CompletionGuard FAIL，注入纠偏续跑 run=%s iteration=%d",
            ctx.run_id, iteration,
        )
        return True, events

