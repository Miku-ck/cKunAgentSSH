"""运行流控拦截器：循环检测 / 置信度 / 动态预算 / 成本熔断 — 拆分自原 reins.py。"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from agent.audit import append_record_async
from agent.remote_protocol import has_exit_sentinel

from ..contracts import (
    FILE_MUTATION_TOOLS,
    READONLY_TOOLS,
    ReinDecision,
    ReinPoint,
    RunContext,
    TraceEvent,
)
from .core import _audit_environment, tool_call_signature

logger = logging.getLogger(__name__)

@dataclass
class LoopDetector:
    """
    循环检测 — 多策略组合

    检测策略：
    1. 短窗口高频：3 次内同签名 → 警告
    2. 长窗口低频：20 次内同签名 ≥ 5 次 → 警告（只读工具放宽到 7 次）
    3. 模式重复：最近 6 次中后 3 == 前 3 → 拦截

    升级机制：
    - 第 1 次检测到循环：仅注入纠偏消息
    - 第 2 次检测到循环：拦截 + 注入消息
    - 第 3 次检测到循环：直接 FAILED（终止 run）

    状态隔离：检测窗口和计数器存储在 ctx.metadata，按 RunContext 隔离，
    避免拦截器实例被多 run 复用时跨 run 串扰。
    """
    point: ReinPoint = field(default=ReinPoint.PRE_TOOL_CALL, init=False)
    priority: int = field(default=5, init=False)

    # 只读/低风险工具：读取大文件、目录遍历、观测轮询是合理的高频重复调用，
    # 放宽长窗口阈值。注意：必须与 builtin 注册名一致（曾残留 work 模式旧名
    # read_file/list_dir 导致该放宽永不命中 — 2026-09 清理修复）。
    # 收敛到 contracts.READONLY_TOOLS 单一事实源（与并行化共用同一判定）。
    _READONLY_TOOLS: frozenset = field(
        default_factory=lambda: READONLY_TOOLS, init=False, repr=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        # 从 ctx.metadata 获取/初始化 per-run 状态
        window: deque = ctx.metadata.setdefault(
            "loop_window", deque(maxlen=20)
        )
        loop_count: int = ctx.metadata.setdefault("loop_count", 0)

        last = ctx.active_tool_call
        sig = tool_call_signature(last)
        # C-review M7：「等待审批」窗口内的签名豁免循环检测 — 审批期间的重发是
        # 合理等待，不是循环。旧实现连续 3 次重发即触发循环检测（本拦截器
        # priority=5 先于 FilesystemGuard=10 执行），第 4 次重发被抢截、
        # permission_needed 不再发出 → 授权卡片孤儿、命令永远执行不了。
        # 审批超时后由 FilesystemGuard/PermissionGate 移除标记，检测恢复正常
        # （LLM 若在拒绝后仍重试，循环检测会接管终止 run）。
        # 审查R2修复：同款豁免扩到澄清等待路径 — 原实现只覆盖审批，用户在
        # 向导期间不答、模型每轮重发同一命令，3 次检测即 terminate 把 run 打成
        # FAILED，之后的答案 POST 成孤儿（C-review M7 修审批时漏了澄清一侧）。
        if sig in ctx.metadata.get("permission_pending", {}):
            return ReinDecision()
        if sig in ctx.metadata.get("clarification_pending", ()):
            return ReinDecision()
        window.append(sig)

        # 实时统计当前窗口内该签名出现次数（避免持久计数失真）
        window_list = list(window)
        sig_count_in_window = window_list.count(sig)

        # 策略 1：短窗口高频（连续 3 次同签名）
        detected = False
        reason = ""
        if len(window) >= 3:
            recent_three = window_list[-3:]
            if recent_three[0] == recent_three[1] == recent_three[2]:
                detected = True
                reason = f"短窗口高频：连续 3 次相同调用 {sig[0]}({sig[1]})"

        # 策略 2：长窗口低频（当前窗口内同签名 ≥ 5 次；只读工具放宽到 7 次）
        # 只读工具多次读取同一文件是常见场景（如分析/修复大文件），
        # 适当放宽可减少误杀，同时仍能通过短窗口高频和模式重复策略拦截真死循环。
        long_window_threshold = 7 if sig[0] in self._READONLY_TOOLS else 5
        if not detected and sig_count_in_window >= long_window_threshold:
            detected = True
            reason = f"长窗口低频：{sig[0]}({sig[1]}) 在最近 {len(window)} 次中调用 {sig_count_in_window} 次"

        # 策略 3：模式重复（最近 6 次中后 3 == 前 3）
        if not detected and len(window) >= 6:
            recent = window_list
            if recent[-3:] == recent[-6:-3]:
                detected = True
                reason = "模式重复：最近 6 次中后 3 次 == 前 3 次"

        if not detected:
            return ReinDecision()

        # 升级机制
        ctx.metadata["loop_count"] = loop_count + 1
        current_loop_count = loop_count + 1

        if current_loop_count >= 3:
            # 第 3 次：直接 FAILED（C-review M6：terminate=True 让 driver 真正终止
            # run — 原实现只发 error 事件 + continue，run 继续烧满 max_iterations）
            return ReinDecision(
                intercepted=True,
                terminate=True,
                intercept_reason=f"循环检测第 {current_loop_count} 次触发，终止运行: {reason}",
                events=[TraceEvent(
                    kind="error",
                    source="reins.loop_detector",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={"message": f"循环检测第 {current_loop_count} 次触发，强制终止: {reason}"},
                )],
                inject_messages=[{
                    "role": "user",
                    "content": f"[系统拦截] 检测到工具调用循环（第 {current_loop_count} 次）：{reason}。已强制终止，请用户介入或换一种方法。",
                }],
            )
        elif current_loop_count == 2:
            # 第 2 次：拦截 + 注入
            return ReinDecision(
                intercepted=True,
                intercept_reason=f"循环检测第 {current_loop_count} 次触发: {reason}",
                events=[TraceEvent(
                    kind="warning",
                    source="reins.loop_detector",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={"message": f"循环检测第 {current_loop_count} 次触发，已拦截"},
                )],
                inject_messages=[{
                    "role": "user",
                    "content": f"[系统拦截] 检测到工具调用循环（第 {current_loop_count} 次）：{reason}。请立即换一种方法，否则下次将强制终止。",
                }],
            )
        else:
            # 第 1 次：仅注入纠偏消息（不拦截）
            return ReinDecision(
                intercepted=False,
                inject_messages=[{
                    "role": "user",
                    "content": f"[系统提示] 检测到可能的工具调用循环：{reason}。请换一种方法，避免重复相同的操作。",
                }],
            )


@dataclass
class ConfidenceMonitor:
    """
    置信度监控 — 迁移自 consciousness.SelfMonitor

    post_iteration 时检查置信度，过低则发 human_help_needed 事件。

    状态隔离：confidence_history 和 inject_count 存于 ctx.metadata，
    按 RunContext 隔离，避免拦截器实例被多 run 复用时跨 run 串扰
    （与 LoopDetector 同样的 per-run 隔离策略）。

    误报修复（2026-09-22）：
    - 探针类非零退出（输出含 [exit_code=N] 哨兵 = 命令确已执行）按 -0.05
      轻计，不按 -0.25 失败计 —— grep 无匹配/test 为假是运维日常而非困惑；
    - 同一最后 assistant 消息且无新工具调用时不重复计分 —— 原实现同一句
      带"不确定"的话会在连续多次 post_iteration 里重复扣分；
    - 求助阈值从"连续 3 次"提到"连续 5 次"，并始终累积 history
      （原实现纠偏注入会提前 return，help 分支永远看不到注入后迭代的分数）。
    """
    point: ReinPoint = field(default=ReinPoint.POST_ITERATION, init=False)
    priority: int = field(default=10, init=False)

    # 低置信度阈值 / 连续低分求助次数
    LOW_THRESHOLD: float = field(default=0.4, init=False)
    HELP_CONSECUTIVE: int = field(default=5, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        # per-run 隔离状态：从 ctx.metadata 取/初始化
        history: list[float] = ctx.metadata.setdefault("confidence_history", [])

        # 重复计分守卫：上下文（最后 assistant 消息 + 工具日志长度）未变则
        # 不追加 —— 本拦截器在 driver 主循环里每迭代只调一次，守卫只在
        # "无新消息无新调用"的空转迭代生效，不影响正常节奏。
        sig = self._context_signature(ctx)
        if sig is not None and sig == ctx.metadata.get("confidence_last_signature"):
            confidence = history[-1] if history else 0.8
        else:
            confidence = self._estimate_confidence(ctx)
            history.append(confidence)
            ctx.metadata["confidence_last_signature"] = sig

        # H14 修复：同一 run 只触发一次 human_help_needed，避免连续发多个相同求助卡片
        help_requested: bool = ctx.metadata.get("confidence_help_requested", False)

        decision = ReinDecision()

        # 连续 N 次低置信度 → 求助（仅一次）
        if not help_requested and len(history) >= self.HELP_CONSECUTIVE:
            recent = history[-self.HELP_CONSECUTIVE:]
            if all(c < self.LOW_THRESHOLD for c in recent):
                ctx.metadata["confidence_help_requested"] = True
                decision = ReinDecision(
                    intercepted=True,
                    intercept_reason=(
                        f"连续 {self.HELP_CONSECUTIVE} 次低置信度（{recent}），请求人工介入"
                    ),
                    events=[TraceEvent(
                        kind="human_help_needed",
                        source="reins.confidence_monitor",
                        run_id=ctx.run_id,
                        iteration=ctx.iteration,
                        payload={
                            "reason": "Agent 连续多次低置信度，建议人工介入",
                            "confidence": round(recent[-1], 3),
                        },
                    )],
                )

        # 单次低置信度 → 注入纠偏消息（最多 2 次）
        inject_count: int = ctx.metadata.get("confidence_inject_count", 0)
        if confidence < self.LOW_THRESHOLD and inject_count < 2:
            ctx.metadata["confidence_inject_count"] = inject_count + 1
            inject = [{
                "role": "user",
                "content": "[系统提示] 请重新审视任务，你当前的方案可能存在问题，尝试换一种思路。",
            }]
            decision.inject_messages = list(
                (decision.inject_messages or []) + inject
            )

        return decision

    @staticmethod
    def _context_signature(ctx: RunContext) -> str | None:
        """(最后一条 assistant 内容, 工具日志长度) 签名；无 assistant 消息返回 None"""
        recent = [m for m in ctx.messages if m.get("role") == "assistant"]
        if not recent:
            return None
        return f"{len(ctx.tool_calls_log)}::{recent[-1].get('content', '')}"

    @staticmethod
    def _estimate_confidence(ctx: RunContext) -> float:
        """估算置信度 — 迁移自 consciousness.SelfMonitor.record_llm_output

        2026-09-22 误报修复后的计分：
        - 真实失败（无 exit 哨兵）每个 -0.25，窗口内封顶 4 个；单个失败不触底；
        - 不确定标记词（"可能/不确定"等，含高频中文词）只在窗口内已有真实
          失败时才 -0.15 —— 自信陈述里出现"可能是配置问题"不再单独降权；
        - 已执行非零退出（探针答案，如 grep 无匹配）每个仅 -0.05。
        """
        base = 0.8
        # 取最近一条 assistant 消息
        recent_msgs = [m for m in ctx.messages if m.get("role") == "assistant"]
        last = recent_msgs[-1].get("content", "") if recent_msgs else ""

        # 工具失败 — 只看最近 5 次调用（窗口化），并区分两类：
        # - 真实失败：调用/命令未产出可判读结果
        # - 已执行非零退出（output 含 [exit_code=N] 哨兵）：grep 无匹配、
        #   test 为假等探针结果是有效诊断答案，不是失败信号
        recent_calls = ctx.tool_calls_log[-5:] if ctx.tool_calls_log else []

        def _exited(tc: dict) -> bool:
            # 命令确已执行（输出含 [exit_code=N]）时按有效诊断答案看待，不算失败
            return has_exit_sentinel(tc.get("output_tail") or tc.get("output") or "")

        failed_tools = sum(
            1 for tc in recent_calls
            if tc.get("success") is False and not _exited(tc)
        )
        exited_tools = sum(
            1 for tc in recent_calls
            if tc.get("success") is False and _exited(tc)
        )
        base -= 0.25 * min(failed_tools, 4)
        base -= 0.05 * exited_tools

        # 不确定标记 — 仅在有真实失败时叠加（有失败证据才值得再扣分）
        if failed_tools >= 1:
            uncertain_markers = ["不确定", "不清楚", "可能", "也许", "大概", "I'm not sure", "maybe"]
            if any(marker in last for marker in uncertain_markers):
                base -= 0.15

        return max(0.1, base)


@dataclass
class DynamicBudgetRein:
    """
    动态步数预算拦截器 — POST_ITERATION 时根据工作密度动态放宽步数上限

    策略（多维度综合计分）：
    - 文件编辑：每编辑 N 个不同文件追加 step_per_interval 步（N=file_mutation_interval）
    - 文件读取：每读取 M 个不同文件追加 step_per_interval 步（M=read_file_interval）
    - 工具调用密度：总调用数每达到 K 次追加 1 步（K=tool_call_density_unit）
    - 封顶：不超过 initial_max * max_multiplier（默认 2.0x）
    - 仅追加，不缩减（避免任务被意外提前终止）

    运行时修改 ctx.config.max_iterations 生效，因为 driver 主循环已改为 while。
    """
    point: ReinPoint = field(default=ReinPoint.POST_ITERATION, init=False)
    priority: int = field(default=3, init=False)

    # 文件变更工具集取自 contracts.FILE_MUTATION_TOOLS（唯一源，与 run 摘要共用）
    _FILE_MUTATION_TOOLS: frozenset = field(
        default_factory=lambda: FILE_MUTATION_TOOLS, init=False, repr=False)
    # 读文件工具集（判据：带 path 类参数、读取行为计入「不同文件读取数」）。
    # ⚠️ 与 contracts.READONLY_TOOLS（无副作用只读观测，含无路径参数的观测工具）
    # 不是同一判定：本集合的成员必须能从 args 提取出 path 才有计数意义，故意是
    # READONLY_TOOLS 的子集 —— 命名区分开，避免两份 _READONLY_TOOLS 字面量漂移
    # （2026-10 收敛前的真实状态：一处 10 个、一处 5 个，语义各异）。
    _FILE_READ_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "file_read", "file_glob", "file_grep", "read_image", "log_read",
    }), init=False, repr=False)
    # 每编辑 N 个不同文件追加预算
    file_mutation_interval: int = 3
    # 每读取 M 个不同文件追加预算
    read_file_interval: int = 5
    # 每次追加的步数
    step_per_interval: int = 2
    # 工具调用密度单位：每 K 次工具调用追加 1 步
    tool_call_density_unit: int = 10
    # 封顶倍数（相对 initial_max）
    max_multiplier: float = 2.0

    async def check(self, ctx: RunContext) -> ReinDecision:
        import os

        # 从 ctx.metadata 获取/初始化状态
        initial_max: int = ctx.metadata.setdefault(
            "dynamic_budget_initial_max", ctx.config.max_iterations
        )
        last_extended_count: int = ctx.metadata.setdefault(
            "dynamic_budget_last_extended", 0
        )

        # 统计已编辑的不同文件数（去重，按 normpath）
        touched_files: set = set()
        read_files: set = set()
        for entry in ctx.tool_calls_log:
            tool = entry.get("tool")
            args = entry.get("args") or {}
            path = args.get("path") or args.get("file_path") or args.get("destination") or ""
            if not path:
                continue
            try:
                norm = os.path.normpath(str(path))
            except Exception:
                norm = str(path)
            if tool in self._FILE_MUTATION_TOOLS:
                touched_files.add(norm)
            elif tool in self._FILE_READ_TOOLS:
                read_files.add(norm)

        file_count = len(touched_files)
        read_count = len(read_files)
        total_calls = len(ctx.tool_calls_log)

        # 综合计算应追加的预算
        intervals_from_mutations = file_count // self.file_mutation_interval
        intervals_from_reads = read_count // self.read_file_interval
        steps_from_density = total_calls // self.tool_call_density_unit

        target_budget = min(
            int(initial_max * self.max_multiplier),
            initial_max
            + intervals_from_mutations * self.step_per_interval
            + intervals_from_reads * self.step_per_interval
            + steps_from_density,
        )

        # 仅当目标预算 > 当前 max_iterations 且 > 上次追加值时才追加
        if target_budget <= ctx.config.max_iterations:
            return ReinDecision()

        if target_budget <= last_extended_count:
            return ReinDecision()

        added = target_budget - ctx.config.max_iterations
        ctx.config.max_iterations = target_budget
        ctx.metadata["dynamic_budget_last_extended"] = target_budget

        logger.info(
            "DynamicBudgetRein 追加预算: +%d (files_edited=%d, files_read=%d, total_calls=%d, "
            "initial=%d, extended_to=%d, cap=%d)",
            added, file_count, read_count, total_calls,
            initial_max, target_budget, int(initial_max * self.max_multiplier),
        )

        return ReinDecision(
            intercepted=False,
            events=[TraceEvent(
                kind="budget_extended",
                source="reins.dynamic_budget",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                payload={
                    "initial": initial_max,
                    "extended_to": target_budget,
                    "added": added,
                    "files_edited": file_count,
                    "files_read": read_count,
                    "total_tool_calls": total_calls,
                    "cap": int(initial_max * self.max_multiplier),
                },
            )],
        )


@dataclass
class CostBudgetRein:
    """
    C-review M10：成本/token 硬熔断 — POST_ITERATION

    背景：max_iterations 允许到 500，DynamicBudgetRein 还可再放宽 2x，
    最坏一次 run 约 1000 次 LLM 调用；CostTracker 只累计、无人切断，
    失控任务会持续烧资源直到步数耗尽。

    机制：
    - 每次 POST_ITERATION 从 ctx.metadata["cost_tracker"] 读实时累计
      成本/token（harness.assemble 注入的 per-run 引用）
    - 上限取 ctx.config.max_cost_usd / max_total_tokens
      （None → 内置默认；0 → 显式禁用；>0 → 自定义上限）
    - 超限 → intercepted + terminate：driver transition(FAILED) 并终止 run

    熔断器异常时 fail-open（跳过本次检查）——熔断自身故障不应阻断主流程。
    """
    point: ReinPoint = field(default=ReinPoint.POST_ITERATION, init=False)
    # priority=1：先于 DynamicBudgetRein(3)/ConfidenceMonitor 执行 —
    # Reins 在首个 intercepted 即短路，保证熔断触发后不会再"追加预算"
    priority: int = field(default=1, init=False)
    # 内置默认上限（ctx.config 为 None 时生效）。取值考量：本地 vLLM 按
    # fallback 计价时正常 100-200 步 run 远低于 $2；token 上限对大上下文
    # 循环是第一道闸。均可通过 AgentConfig 覆盖或置 0 禁用。
    default_max_cost_usd: float = 2.0
    default_max_total_tokens: int = 3_000_000

    async def check(self, ctx: RunContext) -> ReinDecision:
        tracker = (ctx.metadata or {}).get("cost_tracker")
        if tracker is None:
            return ReinDecision()
        max_cost = ctx.config.max_cost_usd
        if max_cost is None:
            max_cost = self.default_max_cost_usd
        max_tokens = ctx.config.max_total_tokens
        if max_tokens is None:
            max_tokens = self.default_max_total_tokens
        try:
            total_tokens = int(tracker.get_total_tokens())
            total_cost = float(tracker.get_total_cost())
        except Exception:
            logger.warning("CostBudgetRein 读取成本数据失败，跳过本次检查")
            return ReinDecision()

        cost_exceeded = max_cost > 0 and total_cost > max_cost
        tokens_exceeded = max_tokens > 0 and total_tokens > max_tokens
        if not (cost_exceeded or tokens_exceeded):
            return ReinDecision()

        reason = (
            f"成本/token 预算超限: cost=${total_cost:.4f}(上限 ${max_cost:g}), "
            f"tokens={total_tokens}(上限 {max_tokens})"
        )
        logger.warning("CostBudgetRein 熔断: %s (run_id=%s)", reason, ctx.run_id)
        # 落审计链（熔断也是可审计动作）；conclusion 取值受 audit 校验，用 blocked
        try:
            await append_record_async(
                action="cost_budget_breaker",
                conclusion="blocked",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                target_type="run",
                target_id=ctx.run_id,
                environment=_audit_environment(ctx),
                result=reason,
            )
        except Exception:
            logger.warning("CostBudgetRein 审计写入失败（不阻塞）")
        return ReinDecision(
            intercepted=True,
            terminate=True,
            intercept_reason=reason,
            events=[TraceEvent(
                kind="warning",
                source="reins.cost_budget",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                payload={
                    "message": f"硬熔断：累计成本/token 超预算，运行终止（{reason}）",
                    "total_tokens": total_tokens,
                    "total_cost_usd": round(total_cost, 6),
                    "max_cost_usd": max_cost,
                    "max_total_tokens": max_tokens,
                },
            )],
        )


@dataclass
class WallClockRein:
    """
    任务级墙钟 deadline — POST_ITERATION

    背景：单命令 60s 超时只兜住一条命令挂死；步数上限 + DynamicBudgetRein
    对"多轮短步数、每步都合法"的慢性失控也可能过宽。时间维度的最后防线：
    run 从启动到当前的墙钟时长超上限即熔断终止。

    机制（复刻 CostBudgetRein 口径）：
    - 打点：driver 主循环入口 ctx.metadata["run_started_mono"]（time.monotonic，
      不受系统时钟跳变影响）
    - 软阈值（elapsed ≥ 上限 80%）：一次性注入收尾劝导（metadata 标记防重发），
      走 POST_ITERATION 的 inject_messages 通道
    - 硬阈值（elapsed ≥ 上限）：intercepted + terminate + 落审计链
      （action="wall_clock_breaker"，conclusion="blocked"）
    - 上限取 ctx.config.max_wall_clock_s（0 → 显式禁用）；getattr 兜底：
      旧测试的 stub config 缺该属性时视为禁用，不引入回归
    - 拦截器内部异常 fail-open（跳过本次检查）——熔断自身故障不阻断主流程
    """
    point: ReinPoint = field(default=ReinPoint.POST_ITERATION, init=False)
    # priority=2：先于 DynamicBudgetRein(3)（熔断后不应再追加步数），
    # 后于 CostBudgetRein(1)（资源维度先判，短路语义下成本/token 优先熔断）
    priority: int = field(default=2, init=False)
    # 软阈值比例：elapsed 达上限 80% 时注入预警
    soft_ratio: float = 0.8

    async def check(self, ctx: RunContext) -> ReinDecision:
        max_seconds = getattr(ctx.config, "max_wall_clock_s", 0) or 0
        if max_seconds <= 0:
            return ReinDecision()
        started = (ctx.metadata or {}).get("run_started_mono")
        if started is None:
            return ReinDecision()
        try:
            elapsed = time.monotonic() - float(started)
        except (TypeError, ValueError):
            logger.warning("WallClockRein 读取打点失败，跳过本次检查")
            return ReinDecision()

        if elapsed >= max_seconds:
            reason = f"墙钟超限: elapsed={elapsed:.0f}s (上限 {max_seconds}s)"
            logger.warning("WallClockRein 熔断: %s (run_id=%s)", reason, ctx.run_id)
            # 落审计链（熔断也是可审计动作）；conclusion 取值受 audit 校验，用 blocked
            try:
                await append_record_async(
                    action="wall_clock_breaker",
                    conclusion="blocked",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    target_type="run",
                    target_id=ctx.run_id,
                    environment=_audit_environment(ctx),
                    result=reason,
                )
            except Exception:
                logger.warning("WallClockRein 审计写入失败（不阻塞）")
            return ReinDecision(
                intercepted=True,
                terminate=True,
                intercept_reason=reason,
                events=[TraceEvent(
                    kind="warning",
                    source="reins.wall_clock",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={
                        "message": f"墙钟超限：运行时长超预算，运行终止（{reason}）",
                        "elapsed_s": round(elapsed, 1),
                        "max_wall_clock_s": max_seconds,
                    },
                )],
            )

        # 软阈值预警（一次性）：elapsed 达上限 80% 时注入收尾劝导
        if not (ctx.metadata or {}).get("wall_clock_warned") \
                and elapsed >= max_seconds * self.soft_ratio:
            ctx.metadata["wall_clock_warned"] = True
            return ReinDecision(
                inject_messages=[{
                    "role": "user",
                    "content": (
                        f"[系统提示] 任务已运行 {elapsed:.0f}s"
                        f"（达到墙钟上限 {max_seconds}s 的 80%）。"
                        "请尽快收尾：完成当前关键步骤后直接输出结果总结，"
                        "不要再开启新的大任务。"
                    ),
                }],
            )
        return ReinDecision()
