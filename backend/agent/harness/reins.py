"""
Reins — 缰绳

统一策略层 — 把原 hooks.py + permissions.py + consciousness.py 三套机制合并为
"策略点 (ReinPoint) + 拦截器 (Interceptor)"模型。

迁移自原三个模块：
- hooks.filesystem_guard      → FilesystemGuard
- hooks.loop_detection_guard  → LoopDetector
- consciousness.SelfMonitor   → ConfidenceMonitor
- permissions.PermissionManager → PermissionGate
- hooks.audit_logger          → AuditLogger
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .contracts import (
    Interceptor,
    ReinDecision,
    ReinPoint,
    RunContext,
    TraceEvent,
)

from .safety import classify_command, classify_shell_args

from agent.audit import append_record_async

logger = logging.getLogger(__name__)

# 命令执行工具：审批必须绑定命令内容（审查 M-3），工具级授权不得覆盖命令级审批。
# job_run 同属命令入口（local=本地子进程 / remote=远程命令），其 `cmd` 参数
# 必须经 classify_command 精细判定，wrapper 不参与分类，防止内部 dangerous
# 命令借工具级 moderate 一刀切漏审。
_COMMAND_EXEC_TOOLS = {"shell_exec", "job_run"}

# C-review M7：审批等待超时（秒）。危险命令/权限请求发出后，用户在此时限内
# 未响应 → 按拒绝处理：签名移出"等待审批"集合、不再重发 permission_needed
# （发 permission_expired 让前端标记卡片过期）、注入强拒绝消息让 LLM 换路。
_PERMISSION_APPROVAL_TIMEOUT_S = 120.0


def _perm_pending(ctx: RunContext) -> dict:
    """C-review M7：「等待审批」签名表（ctx.metadata，per-run 隔离）

    key = LoopDetector._signature() 返回的 hashable 元组，
    value = 首次发出 permission_needed 的 time.monotonic() 时间戳。
    窗口内的重发豁免循环检测；超时的重发按拒绝处理。
    """
    return ctx.metadata.setdefault("permission_pending", {})


def _audit_target_type(ctx: "RunContext") -> str:
    """根据运行上下文推断目标类型：仅远程（SSH 会话）；无会话时工具本身已 fail-closed 拒绝"""
    if getattr(ctx, "ssh_session_id", ""):
        return "ssh"
    return ""


def _audit_target_id(ctx: "RunContext") -> str:
    """目标标识：SSH 会话 id"""
    return getattr(ctx, "ssh_session_id", "") or ""


def _read_text_safe(path: str) -> str:
    """同步安全读文本（供 asyncio.to_thread 调用，审查修复：原实现直接在
    事件循环内 open().read() 大文件会阻塞事件循环）"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


class Reins:
    """
    缰绳 — 持有一组 Interceptor，按 point + priority 分组

    用法：
        reins = Reins()
        reins.register(FilesystemGuard())
        reins.register(PermissionGate(...))
        decision = await reins.check(ReinPoint.PRE_TOOL_CALL, ctx)
        if decision.intercepted:
            ...
    """

    def __init__(self) -> None:
        # 按 ReinPoint 分组的拦截器列表（每组内按 priority 升序）
        self._interceptors: dict[ReinPoint, list[Interceptor]] = {
            pt: [] for pt in ReinPoint
        }

    def register(self, interceptor: Interceptor) -> None:
        """注册拦截器 — 按 priority 升序插入"""
        bucket = self._interceptors[interceptor.point]
        bucket.append(interceptor)
        bucket.sort(key=lambda x: x.priority)

    def register_all(self, interceptors: list[Interceptor]) -> None:
        for it in interceptors:
            self.register(it)

    async def check(self, point: ReinPoint, ctx: RunContext) -> ReinDecision:
        """
        按优先级执行所有该点的拦截器

        任一拦截器返回 intercepted=True 则停止后续，Driver 会：
        1. 发出 decision.events
        2. 执行 decision.undo_actions 回滚
        3. 注入 decision.inject_messages 到下一轮 LLM

        审查修复：原实现在拦截器未拦截时直接丢弃其 events/inject_messages
        （循环后返回空决策）→ LoopDetector 第一级纠偏、ConfidenceMonitor
        单次纠偏、DynamicBudgetRein 的 budget_extended 事件全部丢失，
        LLM 收不到实时纠偏。现聚合未拦截
        决策的 events 与 inject_messages 一并返回（不拦截，仅透传反馈）。
        """
        events: list = []
        inject_messages: list = []
        for interceptor in self._interceptors[point]:
            # 审查 MINOR 修复：安全类拦截器（FilesystemGuard/PermissionGate）异常时
            # fail-closed（拦截），旧实现一律 continue = 跳过权限检查直接放行工具
            safety_critical = getattr(interceptor, "safety_critical", False)
            try:
                decision = await interceptor.check(ctx)
                if decision.intercepted:
                    logger.info(
                        "Reins 拦截 [%s] by %s: %s",
                        point.value, type(interceptor).__name__, decision.intercept_reason,
                    )
                    return decision
                # 未拦截：聚合纠偏事件与注入消息（见 docstring）
                if decision.events:
                    events.extend(decision.events)
                if decision.inject_messages:
                    inject_messages.extend(decision.inject_messages)
            except Exception as e:
                logger.warning(
                    "Reins 拦截器异常 [%s] %s: %s",
                    point.value, type(interceptor).__name__, e, exc_info=True,
                )
                if safety_critical:
                    return ReinDecision(
                        intercepted=True,
                        intercept_reason=f"安全拦截器异常（fail-closed）: {type(interceptor).__name__}: {e}",
                    )
                # 非安全拦截器异常不阻塞主流程，继续下一个
                continue
        return ReinDecision(intercepted=False, events=events, inject_messages=inject_messages)


# ──────────────────────────────────────────────────────────────────────────────
# 内置拦截器
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FilesystemGuard:
    """
    文件系统守卫 — 迁移自 hooks.filesystem_guard

    pre_tool_call 时检查敏感 shell 命令（rm -rf / 等）
    """
    point: ReinPoint = field(default=ReinPoint.PRE_TOOL_CALL, init=False)
    priority: int = field(default=10, init=False)
    # 安全类拦截器：异常时 fail-closed（见 Reins.check）
    safety_critical: bool = field(default=True, init=False)
    # 关联的权限门 — 用于 dangerous 命令的审批决策
    permission_gate: Optional["PermissionGate"] = None

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        # 取最近一次工具调用（Driver 在 PRE_TOOL_CALL 时已记录）
        last = ctx.tool_calls_log[-1] if ctx.tool_calls_log else None
        if not last:
            return ReinDecision()

        tool = last.get("tool", "")
        args = last.get("args", {})

        # 敏感 shell 命令检查 — 委托给 safety 模块分类
        # （shell_exec / job_run 均按 command|shell|cmd 参数提取内部命令）
        if tool in _COMMAND_EXEC_TOOLS:
            level, reason = classify_shell_args(args)
            if level == "hardline":
                # 无条件阻断，不询问 PermissionGate
                # 修复：使用 "user" 角色而非 "system"，因为 inject_messages 会被添加到消息列表末尾
                # OpenAI API 要求 system 消息必须在开头，添加到末尾会触发 400 错误
                cmd = args.get("command") or args.get("shell") or args.get("cmd") or ""
                await append_record_async(
                    action=tool,
                    conclusion="blocked",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    target_type=_audit_target_type(ctx),
                    target_id=_audit_target_id(ctx),
                    environment=getattr(ctx, "environment", "test") or "test",
                    command_raw=str(cmd),
                    result=f"hardline: {reason}",
                )
                return ReinDecision(
                    intercepted=True,
                    intercept_reason=f"敏感命令: {reason}",
                    events=[TraceEvent(
                        kind="warning",
                        source="reins.filesystem_guard",
                        run_id=ctx.run_id,
                        iteration=ctx.iteration,
                        payload={"message": f"已拦截敏感命令: {reason}"},
                    )],
                    inject_messages=[{
                        "role": "user",
                        "content": f"[系统拦截] 已拦截敏感命令: {reason}。请使用更安全的替代方案。",
                    }],
                )
            elif level == "dangerous":
                # 交给 PermissionGate 决定是否需要审批
                # B-MINOR 修复：permission_gate 未配置时 fail-closed —
                # 原实现直接落到 "safe → 放行"，dangerous 命令在缺权限门时反而无审批
                cmd = args.get("command") or args.get("shell") or args.get("cmd") or ""
                if self.permission_gate is None:
                    await append_record_async(
                        action=tool, conclusion="denied",
                        run_id=ctx.run_id, iteration=ctx.iteration,
                        target_type=_audit_target_type(ctx),
                        target_id=_audit_target_id(ctx),
                        environment=getattr(ctx, "environment", "test") or "test",
                        command_raw=str(cmd),
                        result=f"permission_gate_missing: {reason}",
                    )
                    return ReinDecision(
                        intercepted=True,
                        intercept_reason="权限门未配置，拒绝执行危险命令",
                        events=[TraceEvent(
                            kind="warning",
                            source="reins.filesystem_guard",
                            run_id=ctx.run_id,
                            iteration=ctx.iteration,
                            payload={"message": f"权限门未配置，拒绝执行危险命令: {reason}"},
                        )],
                        inject_messages=[{
                            "role": "user",
                            "content": (
                                f"[系统拦截] 权限门未配置，拒绝执行危险命令（{reason}）。"
                                "请配置 PermissionGate 后重试，或改用更安全的替代方案。"
                            ),
                        }],
                    )
                # 生产环境强化（合规）：危险命令仅接受预授权 allowlist，禁止临时一次审批
                env = (getattr(ctx, "environment", "test") or "test")
                if env == "prod":
                    if not self.permission_gate.is_permanently_allowed(tool, args):
                        await append_record_async(
                            action=tool, conclusion="denied",
                            run_id=ctx.run_id, iteration=ctx.iteration,
                            target_type=_audit_target_type(ctx),
                            target_id=_audit_target_id(ctx),
                            environment=env,
                            command_raw=str(cmd),
                            result=f"prod_deny_no_allowlist: {reason}",
                        )
                        return ReinDecision(
                            intercepted=True,
                            intercept_reason=f"生产环境危险命令未预授权: {reason}",
                            events=[TraceEvent(
                                kind="warning",
                                source="reins.filesystem_guard",
                                run_id=ctx.run_id,
                                iteration=ctx.iteration,
                                payload={
                                    "message": (
                                        f"[生产强化] 危险命令在 prod 环境仅接受预授权 allowlist 放行。"
                                        f"拒绝执行: {reason}"
                                    ),
                                },
                            )],
                            inject_messages=[{
                                "role": "user",
                                "content": (
                                    f"[系统拦截] 当前目标为生产环境，危险命令（{reason}）需预先在"
                                    "权限 allowlist 中登记后方可执行，此处拒绝临时授权。请改用更安全的操作。"
                                ),
                            }],
                        )
                    # 预授权命中 → 放行
                else:
                    # C-review M11：session 授权按会话键控
                    allowed = self.permission_gate.check_permission(
                        tool, args, risk="dangerous", conversation_id=ctx.conversation_id)
                    if not allowed:
                        # C-review M7：审批等待三态处理（无审批超时、重发被循环检测
                        # 抢截、授权卡片孤儿的问题由此修复）
                        pending = _perm_pending(ctx)
                        sig = LoopDetector._signature(last)
                        first_ts = pending.get(sig)
                        if first_ts is not None and \
                                (time.monotonic() - first_ts) > _PERMISSION_APPROVAL_TIMEOUT_S:
                            # 超时 → 按拒绝：移除标记（循环检测恢复计数）、不再重发卡片
                            pending.pop(sig, None)
                            await append_record_async(
                                action=tool, conclusion="denied",
                                run_id=ctx.run_id, iteration=ctx.iteration,
                                target_type=_audit_target_type(ctx),
                                target_id=_audit_target_id(ctx),
                                environment=env,
                                command_raw=str(cmd),
                                result=f"approval_timeout: {reason}",
                            )
                            return ReinDecision(
                                intercepted=True,
                                intercept_reason=f"审批超时（按拒绝处理）: {reason}",
                                events=[TraceEvent(
                                    kind="permission_expired",
                                    source="reins.filesystem_guard",
                                    run_id=ctx.run_id,
                                    iteration=ctx.iteration,
                                    payload={
                                        "tool": tool,
                                        "args": args,
                                        "message": (
                                            f"授权审批超时（{int(_PERMISSION_APPROVAL_TIMEOUT_S)}s 未响应），"
                                            f"已按拒绝处理: {tool}"
                                        ),
                                    },
                                )],
                                inject_messages=[{
                                    "role": "user",
                                    "content": (
                                        f"[系统拦截] 上述命令的授权审批已超时"
                                        f"（{int(_PERMISSION_APPROVAL_TIMEOUT_S)}s 无用户响应），"
                                        "系统已按拒绝处理。请勿重试该命令；改用更安全的替代方案，"
                                        "或提示用户确认后再重新发起。"
                                    ),
                                }],
                            )
                        is_first = first_ts is None
                        if is_first:
                            pending[sig] = time.monotonic()
                        # 审查 M-3 修复：原实现只发 "warning" 事件，前端没有可操作的授权卡片，
                        # 用户根本无法批准 dangerous 命令（授权闭环断裂）。
                        # 改发 permission_needed（payload 含 tool/args/command），前端
                        # AgentRunPanel 渲染授权卡片，用户选择 本次/会话/始终（绑定命令）。
                        # C-review M7：仅首次请求发卡片（前端本就按 工具+命令 去重）；
                        # 审批窗口内的重发只拦截 + 注入等待提示，不重复发事件。
                        if is_first:
                            await append_record_async(
                                action=tool, conclusion="denied",
                                run_id=ctx.run_id, iteration=ctx.iteration,
                                target_type=_audit_target_type(ctx),
                                target_id=_audit_target_id(ctx),
                                environment=env,
                                command_raw=str(cmd),
                                result=f"approval_needed: {reason}",
                            )
                        events = [TraceEvent(
                            kind="permission_needed",
                            source="reins.filesystem_guard",
                            run_id=ctx.run_id,
                            iteration=ctx.iteration,
                            payload={
                                "tool": tool,
                                "args": args,
                                "risk": "dangerous",
                                "reversible": False,
                                "message": f"需要用户授权: {tool} — {reason}",
                            },
                        )] if is_first else []
                        return ReinDecision(
                            intercepted=True,
                            intercept_reason=f"需审批命令: {reason}",
                            events=events,
                            inject_messages=[{
                                "role": "user",
                                "content": (
                                    f"[系统拦截] 命令被判定为 dangerous（{reason}），已在界面上向用户请求授权。"
                                    "请重新发起完全相同的命令（授权后会被放行），或改用更安全的替代方案。"
                                ),
                            }],
                        )
            # safe → 放行

        return ReinDecision()


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
    # read_file/list_dir 导致该放宽永不命中 — 2026-09 清理修复）
    _READONLY_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "file_read", "file_glob", "file_grep", "read_image", "log_read",
        "sys_overview", "process_inspect", "network_probe",
        "job_list", "job_output",
    }), init=False, repr=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        # 从 ctx.metadata 获取/初始化 per-run 状态
        window: deque = ctx.metadata.setdefault(
            "loop_window", deque(maxlen=20)
        )
        loop_count: int = ctx.metadata.setdefault("loop_count", 0)

        last = ctx.tool_calls_log[-1]
        sig = self._signature(last)
        # C-review M7：「等待审批」窗口内的签名豁免循环检测 — 审批期间的重发是
        # 合理等待，不是循环。旧实现连续 3 次重发即触发循环检测（本拦截器
        # priority=5 先于 FilesystemGuard=10 执行），第 4 次重发被抢截、
        # permission_needed 不再发出 → 授权卡片孤儿、命令永远执行不了。
        # 审批超时后由 FilesystemGuard/PermissionGate 移除标记，检测恢复正常
        # （LLM 若在拒绝后仍重试，循环检测会接管终止 run）。
        if sig in ctx.metadata.get("permission_pending", {}):
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

    @staticmethod
    def _signature(call: dict[str, Any]) -> tuple:
        """生成规范化的工具调用签名

        规范化：
        - 路径：os.path.normpath 统一格式
        - 命令：shlex.split + 重组，消除空白差异
        - 其他：取 str()
        """
        import os
        import shlex

        tool = call.get("tool", "")
        args = call.get("args") or {}

        # 优先用路径类参数
        path = args.get("file_path") or args.get("path") or args.get("source") or args.get("destination")
        if path:
            try:
                path = os.path.normpath(str(path))
            except Exception:
                path = str(path)
            return (tool, ("path", path))

        # 命令类参数
        command = args.get("command")
        if command:
            try:
                # 规范化命令：shlex 分词后重组，消除空白差异
                parts = shlex.split(str(command))
                command = " ".join(parts)
            except Exception:
                command = str(command)
            return (tool, ("cmd", command))

        # URL / query 类参数
        url = args.get("url")
        if url:
            return (tool, ("url", str(url)))
        query = args.get("query")
        if query:
            return (tool, ("query", str(query)))

        # fallback：工具名 + 参数 frozenset（捕获所有参数）
        return (tool, ("args", frozenset(
            (k, str(v)) for k, v in args.items() if v is not None
        )))


@dataclass
class ConfidenceMonitor:
    """
    置信度监控 — 迁移自 consciousness.SelfMonitor

    post_iteration 时检查置信度，过低则发 human_help_needed 事件。

    状态隔离：confidence_history 和 inject_count 存于 ctx.metadata，
    按 RunContext 隔离，避免拦截器实例被多 run 复用时跨 run 串扰
    （与 LoopDetector 同样的 per-run 隔离策略）。
    """
    point: ReinPoint = field(default=ReinPoint.POST_ITERATION, init=False)
    priority: int = field(default=10, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        confidence = self._estimate_confidence(ctx)

        # per-run 隔离状态：从 ctx.metadata 取/初始化
        history: list[float] = ctx.metadata.setdefault("confidence_history", [])
        history.append(confidence)

        # H14 修复：同一 run 只触发一次 human_help_needed，避免连续发多个相同求助卡片
        help_requested: bool = ctx.metadata.get("confidence_help_requested", False)

        # 连续 3 次低置信度 → 求助（仅一次）
        if not help_requested and len(history) >= 3:
            recent = history[-3:]
            if all(c < 0.4 for c in recent):
                ctx.metadata["confidence_help_requested"] = True
                return ReinDecision(
                    intercepted=True,
                    intercept_reason=f"连续 3 次低置信度（{recent}），请求人工介入",
                    events=[TraceEvent(
                        kind="human_help_needed",
                        source="reins.confidence_monitor",
                        run_id=ctx.run_id,
                        iteration=ctx.iteration,
                        payload={"reason": "Agent 连续多次低置信度，建议人工介入"},
                    )],
                )

        # 单次低置信度 → 注入纠偏消息（最多 2 次）
        inject_count: int = ctx.metadata.get("confidence_inject_count", 0)
        if confidence < 0.4 and inject_count < 2:
            ctx.metadata["confidence_inject_count"] = inject_count + 1
            return ReinDecision(
                intercepted=False,
                inject_messages=[{
                    "role": "user",
                    "content": "[系统提示] 请重新审视任务，你当前的方案可能存在问题，尝试换一种思路。",
                }],
            )

        return ReinDecision()

    @staticmethod
    def _estimate_confidence(ctx: RunContext) -> float:
        """估算置信度 — 迁移自 consciousness.SelfMonitor.record_llm_output"""
        base = 0.8
        # 取最近一条 assistant 消息
        recent_msgs = [m for m in ctx.messages if m.get("role") == "assistant"]
        if not recent_msgs:
            return base
        last = recent_msgs[-1].get("content", "")

        # 不确定标记
        uncertain_markers = ["不确定", "不清楚", "可能", "也许", "大概", "I'm not sure", "maybe"]
        for marker in uncertain_markers:
            if marker in last:
                base -= 0.15
                break

        # 工具失败 — 审查 MINOR 修复：只看最近 5 次调用（窗口化），
        # 原实现累计整个 run 的所有失败，长 run 里一次早期失败会永久压低置信度，
        # 触发误判求助/纠偏注入
        recent_calls = ctx.tool_calls_log[-5:] if ctx.tool_calls_log else []
        failed_tools = sum(1 for tc in recent_calls if tc.get("success") is False)
        base -= 0.2 * failed_tools

        return max(0.1, base)


@dataclass
class PermissionGate:
    """
    权限门 — 从 Saddle 实时读取 ToolMeta 判定风险等级

    单一数据源：风险等级由工具的 ToolMeta 声明（tools/__init__.py），
    不在此处硬编码 _risk_table，避免与工具声明脱同步。

    判定策略：
    - safe → 自主放行
    - moderate + 可逆 → 自动放行（可逆操作无需审批）
    - moderate 不可逆 / dangerous → 需用户授权，未授权则发 permission_needed 事件 + 拦截
    - 工具未注册 → 拦截并明确报错（区分"未注册" vs "确实危险"）
    """
    point: ReinPoint = field(default=ReinPoint.PRE_TOOL_CALL, init=False)
    priority: int = field(default=8, init=False)
    # 安全类拦截器：异常时 fail-closed（见 Reins.check）
    safety_critical: bool = field(default=True, init=False)
    # Saddle 引用（用于从 ToolRegistry 读取 ToolMeta，单一数据源）
    # 审查 MINOR 修复：本实例是进程级单例跨 run 共享，并发 run 下 `saddle` 字段会被
    # 后启动的 run 覆盖（查错 ToolRegistry → 误判"工具未注册"）。因此 check 优先使用
    # ctx.metadata["saddle"]（per-run），self.saddle 仅作无 ctx 调用（如 grant 端点）的兜底。
    saddle: Optional[Any] = None
    # 三范围审批状态
    # 修复：使用 Counter 替代 set，避免跨 run 竞态（Run A 的授权被 Run B 消费）
    _confirmed_once: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # 本次确认（计数器）
    # C-review M11：会话级确认按 conversation_id 键控（原全局 set — 会话 A 的
    # session 授权会泄漏到会话 B 的 run 中免审）。进程级单例共享不变，
    # 但"会话"边界 = conversation_id
    _confirmed_session: dict[str, set[str]] = field(default_factory=dict)  # conversation_id → 确认键
    _confirmed_permanent: set[str] = field(default_factory=set)  # 永久确认（持久化到 config）
    # 用户覆盖（从 DB 加载）
    _overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._confirmed_permanent = self._load_permanent_allowlist()

    def _load_permanent_allowlist(self) -> set[str]:
        """从 config 加载永久 allowlist（简化：从 ~/.ckunagentic/allowlist.txt 读取，每行一个工具名）"""
        import os
        path = os.path.expanduser('~/.ckunagentic/allowlist.txt')
        if not os.path.exists(path):
            return set()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return {line.strip() for line in f if line.strip() and not line.startswith('#')}
        except Exception:
            return set()

    def _save_permanent_allowlist(self) -> None:
        """保存永久 allowlist"""
        import os
        path = os.path.expanduser('~/.ckunagentic/allowlist.txt')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for name in sorted(self._confirmed_permanent):
                f.write(name + '\n')

    @staticmethod
    def _grant_key(tool_name: str, command: Optional[str] = None) -> str:
        """计算授权键（审查 M-3 核心修复）。

        - 命令执行工具（shell_exec）+ 具体命令 → `tool:<sha256(规范化命令)[:16]>`，
          授权只绑定被批准的那条命令，防止一次"始终允许 shell_exec"后任意
          dangerous 命令免审批（如 `rm -rf ~/backups`）。
        - 其他工具 / 未提供命令 → 工具名本身（保持原工具级授权语义）。

        兼容性：旧 allowlist.txt 中裸 `shell_exec` 条目对新逻辑不再放行命令级审批
        （fail-safe 方向收紧），用户需重新针对具体命令授权。
        """
        if tool_name in _COMMAND_EXEC_TOOLS and command and str(command).strip():
            from .safety import normalize_command_for_detection
            digest = hashlib.sha256(
                normalize_command_for_detection(str(command)).encode("utf-8")
            ).hexdigest()[:16]
            return f"{tool_name}:{digest}"
        return tool_name

    def check_permission(self, tool_name: str, args: dict,
                         risk: Optional[str] = None,
                         conversation_id: str = "") -> bool:
        """检查权限，返回是否放行

        risk: 调用方已判定的风险等级；risk == "dangerous" 时忽略 session 级缓存
              （B-6：落实 "dangerous 需每次授权" 契约，只认 once 与 always/permanent 键）

        检查顺序：permanent allowlist → session（非 dangerous，按会话键控）
        → once 计数器。原 scope 参数已移除（C-review NIT：死参数，行为实际由
        risk + 已存在的授权决定，与传入的 scope 无关）。

        无交互式 UI 场景：默认拦截 dangerous 命令，除非已在 allowlist 中。
        命令执行工具按"工具+命令哈希"键检查（审查 M-3）。
        """
        command = None
        if tool_name in _COMMAND_EXEC_TOOLS and isinstance(args, dict):
            command = args.get("command") or args.get("shell") or args.get("cmd")
        key = self._grant_key(tool_name, command)
        # 检查 permanent allowlist
        if key in self._confirmed_permanent:
            return True
        # 检查 session allowlist
        # B-6 修复：dangerous 需每次授权 — 忽略 session 级缓存
        # （原实现固定按 permanent→session→once 检查，首次 session 授权后
        #   同命令后续执行全部免审批，与 check() 中 L748 注释契约矛盾）
        # C-review M11：只查本会话（conversation_id）的 session 授权，
        # 跨会话不再免审（原全局 set 会把会话 A 的授权泄漏到会话 B）
        if risk != "dangerous" and key in self._confirmed_session.get(conversation_id, frozenset()):
            return True
        # 修复：检查 once allowlist（使用计数器，避免跨 run 竞态）
        if self._confirmed_once.get(key, 0) > 0:
            self._confirmed_once[key] -= 1
            if self._confirmed_once[key] <= 0:
                del self._confirmed_once[key]
            return True
        # 默认拦截 dangerous 命令（无交互式 UI，不能自动放行）
        return False

    def is_permanently_allowed(self, tool_name: str, args: dict) -> bool:
        """是否已被永久授权（生产环境危险命令的预授权 allowlist 判断）。

        仅认 `_confirmed_permanent`（持久化 allowlist），忽略 once/session 一次性授权，
        保证 prod 环境危险命令必须"预先登记"而非临时批准（合规要求）。
        命令执行工具按"工具+命令哈希"键匹配（审查 M-3），其余工具按工具名匹配。
        """
        command = None
        if tool_name in _COMMAND_EXEC_TOOLS and isinstance(args, dict):
            command = args.get("command") or args.get("shell") or args.get("cmd")
        key = self._grant_key(tool_name, command)
        return key in self._confirmed_permanent

    def grant(self, tool_name: str, scope: str = 'once', command: Optional[str] = None,
              conversation_id: str = "") -> str:
        """授予权限，返回实际写入的授权键

        scope: 'once' | 'session' | 'always'
        command: 命令执行工具时传入被批准的具体命令（审查 M-3 命令级绑定）
        conversation_id: C-review M11 — session 授权绑定到发起授权的会话
        """
        key = self._grant_key(tool_name, command)
        if scope == 'once':
            # 修复：使用计数器，支持多次授权累积
            self._confirmed_once[key] = self._confirmed_once.get(key, 0) + 1
        elif scope == 'session':
            self._confirmed_session.setdefault(conversation_id, set()).add(key)
        elif scope == 'always':
            self._confirmed_permanent.add(key)
            self._save_permanent_allowlist()
        return key

    def _revoke_key(self, key: str, conversation_id: Optional[str] = None) -> None:
        self._confirmed_once.pop(key, None)  # Counter 使用 pop 替代 discard
        # C-review M11：conversation_id 指定 → 只撤该会话；None → 撤所有会话
        if conversation_id is not None:
            s = self._confirmed_session.get(conversation_id)
            if s is not None:
                s.discard(key)
                if not s:
                    self._confirmed_session.pop(conversation_id, None)
        else:
            for s in self._confirmed_session.values():
                s.discard(key)
            for cid in [c for c, s in self._confirmed_session.items() if not s]:
                self._confirmed_session.pop(cid, None)
        self._confirmed_permanent.discard(key)

    def revoke(self, tool_name: str, command: Optional[str] = None,
               conversation_id: Optional[str] = None) -> None:
        """撤销权限

        - 提供 command：只撤销该条命令的绑定授权
        - 不提供 command：撤销该工具的全部授权（工具级 + 所有命令级绑定键）
        - conversation_id：C-review M11 — 只撤指定会话的 session 授权；
          None（默认）保持旧语义：撤所有会话
        """
        if command and tool_name in _COMMAND_EXEC_TOOLS:
            self._revoke_key(self._grant_key(tool_name, command), conversation_id)
        else:
            self._revoke_key(tool_name, conversation_id)
            prefix = tool_name + ":"
            for k in [k for k in self._confirmed_permanent if k.startswith(prefix)]:
                self._revoke_key(k, conversation_id)
            cids = [conversation_id] if conversation_id is not None \
                else list(self._confirmed_session.keys())
            for cid in cids:
                s = self._confirmed_session.get(cid)
                if not s:
                    continue
                for k in [k for k in s if k.startswith(prefix)]:
                    self._revoke_key(k, cid)
            for k in [k for k in self._confirmed_once if k.startswith(prefix)]:
                self._revoke_key(k, conversation_id)
        self._save_permanent_allowlist()

    def clear_once(self) -> None:
        """清空 once 范围（每次工具调用后调用）"""
        self._confirmed_once.clear()

    def load_overrides(self, overrides: dict[str, str]) -> None:
        """从 DB 加载用户自定义权限覆盖"""
        self._overrides.update(overrides)

    def _lookup_meta(self, tool_name: str, ctx: Optional[RunContext] = None) -> Optional[Any]:
        """从 Saddle 的 ToolRegistry 读取工具的 ToolMeta（单一数据源）

        返回 None 表示工具未注册。
        审查 MINOR：优先使用 per-run saddle（ctx.metadata["saddle"]），
        避免进程级单例的 saddle 字段被并发 run 覆盖后查错注册表。
        """
        saddle = None
        if ctx is not None:
            saddle = (ctx.metadata or {}).get("saddle")
        saddle = saddle or self.saddle
        if not saddle:
            return None
        try:
            tool = saddle.registry.get(tool_name)
            if tool is None:
                return None
            return tool.meta
        except Exception:
            return None

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        last = ctx.tool_calls_log[-1]
        tool = last.get("tool", "")
        args = last.get("args", {})

        # 用户覆盖优先
        if tool in self._overrides:
            level = self._overrides[tool]
            if level == "autonomous":
                return ReinDecision()
            # 其他级别走标准流程

        # H14 修复：命令执行工具（shell_exec）绕过 tool-level 风险检查
        # 原因：这些工具自身是命令入口，命令级安全由 FilesystemGuard + Sandbox 的
        # safety.classify_command 精细判定（hardline/dangerous/safe），
        # 不应被 PermissionGate 基于工具级 risk_level 一刀切拦截。
        # 若在此拦截，FilesystemGuard 的精细分类永远跑不到（priority 8 < 10）。
        # 审查 M-3：命令级审批绑定在 FilesystemGuard → PermissionGate.check_permission
        # 的 `tool:cmdhash` 授权键上，不在此处用工具级 risk 一刀切。
        if tool in _COMMAND_EXEC_TOOLS:
            # 跳过 tool-level 风险检查，由 FilesystemGuard 按命令内容判定
            return ReinDecision()

        # 从 ToolMeta 读取风险等级（单一数据源，避免硬编码脱同步）
        # 审查 MINOR：传入 ctx 使用 per-run saddle（单例 saddle 字段会被并发 run 覆盖）
        meta = self._lookup_meta(tool, ctx)
        if meta is None:
            # 工具未注册 — 拦截并明确报错（区分"未注册" vs "确实危险"）
            return ReinDecision(
                intercepted=True,
                intercept_reason=f"工具未注册: {tool}",
                events=[TraceEvent(
                    kind="error",
                    source="reins.permission_gate",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={"tool": tool, "message": f"工具未注册: {tool}"},
                )],
                inject_messages=[{
                    "role": "user",
                    "content": f"[系统拦截] 工具 {tool} 未注册，无法执行。请使用已注册的工具。",
                }],
            )

        risk = meta.risk_level
        reversible = meta.reversible

        # safe → 自主放行
        if risk == "safe":
            return ReinDecision()

        # moderate + 可逆 → 自动放行（可逆操作无需审批）
        if risk == "moderate" and reversible:
            return ReinDecision()

        # dangerous → 需每次授权（不可 session 级缓存，防止首次授权后 Agent 执行任意高危命令）
        if risk == "dangerous":
            # C-review NIT：原 scope='once' 为死参数 — dangerous 每次授权的
            # 语义由 risk 分支保证（忽略 session 缓存，只认 once/permanent 键）
            if self.check_permission(tool, args, risk='dangerous',
                                      conversation_id=ctx.conversation_id):
                return ReinDecision()
        else:
            # moderate 不可逆 → session 级授权可接受（风险有限）
            if self.check_permission(tool, args,
                                     conversation_id=ctx.conversation_id):
                return ReinDecision()

        # 未授权 → 发 permission_needed 事件 + 拦截
        # C-review M7：与 FilesystemGuard 相同的审批三态处理（首次发卡片 /
        # 窗口内重发豁免循环检测 / 超时按拒绝）
        pending = _perm_pending(ctx)
        sig = LoopDetector._signature(last)
        first_ts = pending.get(sig)
        if first_ts is not None and \
                (time.monotonic() - first_ts) > _PERMISSION_APPROVAL_TIMEOUT_S:
            pending.pop(sig, None)
            return ReinDecision(
                intercepted=True,
                intercept_reason=f"审批超时（按拒绝处理）: {tool}",
                events=[TraceEvent(
                    kind="permission_expired",
                    source="reins.permission_gate",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={
                        "tool": tool,
                        "args": args,
                        "message": (
                            f"授权审批超时（{int(_PERMISSION_APPROVAL_TIMEOUT_S)}s 未响应），"
                            f"已按拒绝处理: {tool}"
                        ),
                    },
                )],
                inject_messages=[{
                    "role": "user",
                    "content": (
                        f"[系统拦截] 工具 {tool} 的授权审批已超时"
                        f"（{int(_PERMISSION_APPROVAL_TIMEOUT_S)}s 无用户响应），系统已按拒绝处理。"
                        "请勿重试该工具；换用更安全的工具，或提示用户确认后再重新发起。"
                    ),
                }],
            )
        is_first = first_ts is None
        if is_first:
            pending[sig] = time.monotonic()
        events = [TraceEvent(
            kind="permission_needed",
            source="reins.permission_gate",
            run_id=ctx.run_id,
            iteration=ctx.iteration,
            payload={
                "tool": tool,
                "args": args,
                "risk": risk,
                "reversible": reversible,
                "message": f"需要用户授权: {tool} (风险: {risk})",
            },
        )] if is_first else []
        return ReinDecision(
            intercepted=True,
            intercept_reason=f"工具 {tool} 风险等级 {risk}，需要用户授权",
            events=events,
            inject_messages=[{
                "role": "user",
                "content": f"[系统拦截] 工具 {tool} 风险等级为 {risk}，需要用户授权才能执行。请换用更安全的工具，或在弹出的授权卡片中允许此工具。",
            }],
        )


@dataclass
class AuditLogger:
    """
    审计日志 — 迁移自 hooks.audit_logger

    post_tool_call 和 agent_failed 时记录日志。
    不拦截，只记录。
    """
    point: ReinPoint = field(default=ReinPoint.POST_TOOL_CALL, init=False)
    priority: int = field(default=10, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()
        last = ctx.tool_calls_log[-1]
        logger.info(
            "AUDIT [%s] iter=%d tool=%s success=%s",
            ctx.run_id, ctx.iteration, last.get("tool", ""), last.get("success"),
        )
        # C-review C2 修复：原实现只有 FilesystemGuard 的 blocked/denied 分支落
        # 审计链，"谁批准了什么、实际执行了什么"全部缺失，等保审计目标落空。
        # 这里为每个真实执行过的工具调用补 executed/failed 记录（append_record
        # 内部静默失败，不阻塞 run）。被 PRE 拦截的调用不会走到 POST_TOOL_CALL，
        # 与 blocked/denied 记录不重复。
        try:
            args = last.get("args") or {}
            command = ""
            if isinstance(args, dict):
                command = str(args.get("command") or args.get("shell") or args.get("cmd") or "")
            await append_record_async(
                action=str(last.get("tool", "")),
                conclusion="executed" if last.get("success") else "failed",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=getattr(ctx, "environment", "test") or "test",
                command_raw=command,
            )
        except Exception as e:
            logger.warning("审计 executed/failed 记录失败（不阻塞）: %s", e)
        return ReinDecision()


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例 — 跨 run 共享 session scope 授权（H13 修复 A6）
# ──────────────────────────────────────────────────────────────────────────────

# 模块级单例引用（首次调用 _get_global_permission_gate_sync 时初始化）
_global_permission_gate: Optional["PermissionGate"] = None


def _get_global_permission_gate_sync(saddle: Optional[Any] = None) -> "PermissionGate":
    """
    获取进程级 PermissionGate 单例（同步版本）

    - 首次调用时创建实例并加载永久 allowlist
    - saddle 参数仅在首次创建时生效（后续调用忽略，保持单例）
    - session scope 授权在进程内跨 run 共享，进程重启后失效（session 语义）

    设计选择：同步而非 async，因为 PermissionGate 初始化是纯同步操作（加载文件 + 创建 set），
    asyncio 单线程无需 asyncio.Lock 保护。这样 create_default_reins 无需改为 async，
    避免牵连 harness.py 两处调用方。
    """
    global _global_permission_gate
    if _global_permission_gate is None:
        _global_permission_gate = PermissionGate(saddle=saddle)
    return _global_permission_gate


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
    """
    point: ReinPoint = field(default=ReinPoint.AGENT_COMPLETE, init=False)
    priority: int = field(default=5, init=False)

    async def check(self, ctx: RunContext) -> ReinDecision:
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
            "mode": ctx.config.mode if hasattr(ctx.config, "mode") else "ssh",
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

    # 文件变更工具集（与 driver._FILE_MUTATION_TOOLS 对齐；必须用 builtin 实际
    # 注册名 —— work 模式旧名 write_file/edit_file 等已随全库清理移除）
    _FILE_MUTATION_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "file_write", "file_edit",
    }), init=False, repr=False)
    # 只读工具集：多次读取不同文件是合理的工作密度，应给予预算（旧名残留修复）
    _READONLY_TOOLS: frozenset = field(default_factory=lambda: frozenset({
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
            elif tool in self._READONLY_TOOLS:
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
                environment=getattr(ctx, "environment", "test") or "test",
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
                    registry.match_skills, ctx.task, ctx.mode, max_results=3
                )
                auto_ids = {s['id'] for s in auto_matched}

            all_ids = list(explicit_ids | auto_ids)
            if not all_ids:
                return ReinDecision()

            parts = []
            catalog = await asyncio.to_thread(registry.build_skill_catalog, all_ids, ctx.mode)
            if catalog:
                parts.append(catalog)

            if explicit_ids:
                preload = await asyncio.to_thread(
                    registry.build_skill_preload, list(explicit_ids), ctx.mode
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


def create_default_reins(
    permission_overrides: Optional[dict[str, str]] = None,
    config: Optional[Any] = None,
    saddle: Optional[Any] = None,
) -> Reins:
    """创建默认 Reins — 注册所有内置拦截器

    Args:
        config: AgentConfig，用于控制各拦截器的启停与参数。
                None 时使用默认配置（全部启用）。
        saddle: Saddle 引用，PermissionGate 从 ToolRegistry 读取 ToolMeta（单一数据源）。
                None 时 PermissionGate 无法判定风险等级，所有工具都会被拦截。

    H13 修复 A6：PermissionGate 使用进程级单例，跨 run 共享 session scope 授权
    """
    # H13 修复 A6：使用进程级单例 PermissionGate
    permission_gate = _get_global_permission_gate_sync(saddle=saddle)
    # H13 修复 A6 bug：每次 run 都更新 saddle 引用
    # 原实现 `if saddle is not None and permission_gate.saddle is None` 只在首次注入 saddle，
    # 但每个 Harness 实例都会创建新的 Saddle（新 ToolRegistry），
    # 导致第二次 run 时单例仍持有旧 Saddle，查询工具会失败（"工具未注册"）
    if saddle is not None:
        permission_gate.saddle = saddle
    if permission_overrides:
        permission_gate.load_overrides(permission_overrides)

    reins = Reins()

    interceptors: list[Interceptor] = [
        SkillRein(),  # AGENT_START: 技能注入
        FilesystemGuard(permission_gate=permission_gate),
        LoopDetector(),
        permission_gate,
        AuditLogger(),
    ]

    # 动态步数预算（POST_ITERATION）
    if config is None or config.enable_dynamic_budget:
        budget_rein = DynamicBudgetRein()
        if config is not None:
            budget_rein.file_mutation_interval = config.dynamic_budget_interval
            budget_rein.step_per_interval = config.dynamic_budget_step
            budget_rein.max_multiplier = config.dynamic_budget_cap
        interceptors.append(budget_rein)

    # C-review M10：成本/token 硬熔断（POST_ITERATION，priority=1 先于动态预算，
    # 触发后短路终止 — 最坏 1000 次 LLM 调用无人切断的问题由此兜底）
    interceptors.append(CostBudgetRein())

    interceptors.append(ConfidenceMonitor())

    # 反思触发（AGENT_COMPLETE + AGENT_FAILED）
    # 注册两个实例，分别绑定到不同 ReinPoint，确保成功/失败/超上限都会推送 reflection_request
    # 原实现 AGENT_FAILED 没有拦截器，导致失败运行错过反思，演化数据偏斜
    interceptors.append(ReflectionRein())  # AGENT_COMPLETE
    failed_reflection = ReflectionRein()
    failed_reflection.point = ReinPoint.AGENT_FAILED
    interceptors.append(failed_reflection)

    reins.register_all(interceptors)
    return reins
