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

from agent.audit import append_record

logger = logging.getLogger(__name__)

# 命令执行工具：审批必须绑定命令内容（审查 M-3），工具级授权不得覆盖命令级审批
_COMMAND_EXEC_TOOLS = {"shell_exec", "local_exec"}


def _audit_target_type(ctx: "RunContext") -> str:
    """根据运行上下文推断目标类型（ssh / local / work）"""
    if getattr(ctx, "ssh_session_id", ""):
        return "ssh"
    if getattr(ctx, "workspace_path", ""):
        return "local"
    return ""


def _audit_target_id(ctx: "RunContext") -> str:
    """目标标识：SSH 会话 id 优先，其次为工作区路径"""
    return getattr(ctx, "ssh_session_id", "") or getattr(ctx, "workspace_path", "") or ""


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
        """
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
        return ReinDecision(intercepted=False)


# ──────────────────────────────────────────────────────────────────────────────
# 内置拦截器
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FilesystemGuard:
    """
    文件系统守卫 — 迁移自 hooks.filesystem_guard

    pre_tool_call 时检查：
    - 路径越界（workspace_path 之外）
    - 敏感 shell 命令（rm -rf / 等）
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

        # 路径越界检查（覆盖所有文件变更工具 + 含 path 参数的只读工具 + source/destination 字段）
        # grep/glob 含 path 参数，虽为只读但应防越界读取（与 Sandbox._check_path_safe 一致）
        _PATH_CHECK_TOOLS = {
            "write_file", "read_file", "list_dir", "patch_file",
            "edit_file", "create_file", "delete_file", "move_file", "rename_file",
            "grep", "glob",
        }
        if tool in _PATH_CHECK_TOOLS and ctx.workspace_path:
            # 收集所有可能的路径参数
            path_keys = ["file_path", "path", "source", "destination", "target"]
            paths_to_check = [args.get(k) for k in path_keys if args.get(k)]
            for path in paths_to_check:
                violation = self._check_path_boundary(path, ctx.workspace_path)
                if violation:
                    return violation

        # 敏感 shell 命令检查 — 委托给 safety 模块分类
        # H14 修复：同时覆盖 local_exec（IDE 模式命令执行工具），与 shell_exec 同源安全策略
        if tool in ("shell_exec", "local_exec"):
            level, reason = classify_shell_args(args)
            if level == "hardline":
                # 无条件阻断，不询问 PermissionGate
                # 修复：使用 "user" 角色而非 "system"，因为 inject_messages 会被添加到消息列表末尾
                # OpenAI API 要求 system 消息必须在开头，添加到末尾会触发 400 错误
                cmd = args.get("command") or args.get("shell") or args.get("cmd") or ""
                append_record(
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
                    append_record(
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
                        append_record(
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
                    allowed = self.permission_gate.check_permission(tool, args, risk="dangerous")
                    if not allowed:
                        # 审查 M-3 修复：原实现只发 "warning" 事件，前端没有可操作的授权卡片，
                        # 用户根本无法批准 dangerous 命令（授权闭环断裂）。
                        # 改发 permission_needed（payload 含 tool/args/command），前端
                        # AgentRunPanel 渲染授权卡片，用户选择 本次/会话/始终（绑定命令）。
                        append_record(
                            action=tool, conclusion="denied",
                            run_id=ctx.run_id, iteration=ctx.iteration,
                            target_type=_audit_target_type(ctx),
                            target_id=_audit_target_id(ctx),
                            environment=env,
                            command_raw=str(cmd),
                            result=f"approval_needed: {reason}",
                        )
                        return ReinDecision(
                            intercepted=True,
                            intercept_reason=f"需审批命令: {reason}",
                            events=[TraceEvent(
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
                            )],
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

    def _check_path_boundary(self, path: str, workspace: str) -> Optional[ReinDecision]:
        """路径越界检查 — 使用 realpath + commonpath 严格检查

        返回 ReinDecision 表示拦截，None 表示放行。
        失败安全：解析异常时拦截。
        """
        import os
        try:
            full_path = os.path.realpath(
                os.path.join(workspace, path) if not os.path.isabs(path) else path
            )
            real_workspace = os.path.realpath(workspace)
            try:
                common = os.path.commonpath([full_path, real_workspace])
            except ValueError:
                # 不同盘符等无法比较 → 拦截
                common = ""
            if common != real_workspace:
                # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
                return ReinDecision(
                    intercepted=True,
                    intercept_reason=f"路径越界: {path} 不在工作区 {workspace} 内",
                    events=[TraceEvent(
                        kind="warning",
                        source="reins.filesystem_guard",
                        run_id="",  # 由调用方补充
                        iteration=0,
                        payload={"message": f"已拦截路径越界操作: {path}"},
                    )],
                    inject_messages=[{
                        "role": "user",
                        "content": f"[系统拦截] 已拦截路径越界操作: {path}。请仅操作工作区 {workspace} 内的文件。",
                    }],
                )
        except Exception as e:
            # 失败安全：解析异常时拦截
            # 修复：使用 "user" 角色而非 "system"
            return ReinDecision(
                intercepted=True,
                intercept_reason=f"路径解析失败: {path} ({e})",
                events=[TraceEvent(
                    kind="warning",
                    source="reins.filesystem_guard",
                    run_id="",
                    iteration=0,
                    payload={"message": f"已拦截路径解析失败: {path}"},
                )],
                inject_messages=[{
                    "role": "user",
                    "content": f"[系统拦截] 路径解析失败: {path}。请使用工作区内的有效路径。",
                }],
            )
        return None


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

    # 只读/低风险工具：读取大文件或目录时多次调用是合理的，放宽长窗口阈值
    _READONLY_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "read_file", "list_dir", "grep", "glob",
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
            # 第 3 次：直接 FAILED
            return ReinDecision(
                intercepted=True,
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
    _confirmed_session: set[str] = field(default_factory=set)  # 会话级确认（整个会话有效）
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

        - 命令执行工具（shell_exec/local_exec）+ 具体命令 → `tool:<sha256(规范化命令)[:16]>`，
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

    def check_permission(self, tool_name: str, args: dict, scope: str = 'once',
                         risk: Optional[str] = None) -> bool:
        """检查权限，返回是否放行

        scope: 'once' (仅本次) | 'session' (会话级) | 'always' (永久)
        risk: 调用方已判定的风险等级；risk == "dangerous" 时忽略 session 级缓存
              （B-6：落实 "dangerous 需每次授权" 契约，只认 once 与 always/permanent 键）

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
        if risk != "dangerous" and key in self._confirmed_session:
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

    def grant(self, tool_name: str, scope: str = 'once', command: Optional[str] = None) -> str:
        """授予权限，返回实际写入的授权键

        scope: 'once' | 'session' | 'always'
        command: 命令执行工具时传入被批准的具体命令（审查 M-3 命令级绑定）
        """
        key = self._grant_key(tool_name, command)
        if scope == 'once':
            # 修复：使用计数器，支持多次授权累积
            self._confirmed_once[key] = self._confirmed_once.get(key, 0) + 1
        elif scope == 'session':
            self._confirmed_session.add(key)
        elif scope == 'always':
            self._confirmed_permanent.add(key)
            self._save_permanent_allowlist()
        return key

    def _revoke_key(self, key: str) -> None:
        self._confirmed_once.pop(key, None)  # Counter 使用 pop 替代 discard
        self._confirmed_session.discard(key)
        self._confirmed_permanent.discard(key)

    def revoke(self, tool_name: str, command: Optional[str] = None) -> None:
        """撤销权限

        - 提供 command：只撤销该条命令的绑定授权
        - 不提供 command：撤销该工具的全部授权（工具级 + 所有命令级绑定键）
        """
        if command and tool_name in _COMMAND_EXEC_TOOLS:
            self._revoke_key(self._grant_key(tool_name, command))
        else:
            self._revoke_key(tool_name)
            prefix = tool_name + ":"
            for k in [k for k in self._confirmed_permanent if k.startswith(prefix)]:
                self._revoke_key(k)
            for k in [k for k in self._confirmed_session if k.startswith(prefix)]:
                self._revoke_key(k)
            for k in [k for k in self._confirmed_once if k.startswith(prefix)]:
                self._revoke_key(k)
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

        # H14 修复：命令执行工具（shell_exec / local_exec）绕过 tool-level 风险检查
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
            if self.check_permission(tool, args, scope='once', risk='dangerous'):
                return ReinDecision()
        else:
            # moderate 不可逆 → session 级授权可接受（风险有限）
            if self.check_permission(tool, args, scope='session'):
                return ReinDecision()

        # 未授权 → 发 permission_needed 事件 + 拦截
        return ReinDecision(
            intercepted=True,
            intercept_reason=f"工具 {tool} 风险等级 {risk}，需要用户授权",
            events=[TraceEvent(
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
            )],
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


async def get_global_permission_gate(saddle: Optional[Any] = None) -> "PermissionGate":
    """
    获取进程级 PermissionGate 单例（异步版本，供 routes 层 await 调用）

    与 _get_global_permission_gate_sync 共享同一单例。
    保留 async 签名是为了符合 FastAPI 异步路由的调用约定。
    """
    return _get_global_permission_gate_sync(saddle=saddle)


def reset_global_permission_gate() -> None:
    """重置单例（仅用于测试隔离）"""
    global _global_permission_gate
    _global_permission_gate = None


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
class SyntaxVerifier:
    """
    语法验证拦截器 — POST_TOOL_CALL 时对文件变更工具做语法检查

    触发条件：工具调用成功 + 工具属于 _FILE_MUTATION_TOOLS + 文件类型支持检查
    检查策略：
    - .py → ast.parse（内存检查，无需落盘也可检查 content）
    - .ts/.tsx/.js/.jsx/.vue → 尝试 tsc --noEmit（若环境有 tsc）
    - .json → json.loads
    - 其他 → 跳过

    检查失败时：
    - 不拦截工具调用（文件已写入，无法回滚语法错误）
    - 注入错误信息到下一轮 LLM，驱动 Agent 修复
    - 记录到 ctx.metadata['syntax_errors'] 供完成前校验
    """
    point: ReinPoint = field(default=ReinPoint.POST_TOOL_CALL, init=False)
    priority: int = field(default=5, init=False)

    # 文件变更工具集（与 driver._FILE_MUTATION_TOOLS 对齐）
    # 注：delete_file 删除后无内容可检，在 check() 内早期跳过；
    # move_file/rename_file 后目标路径为新内容，必须读磁盘验证；
    # create_file/write_file/patch_file 同理。
    _FILE_MUTATION_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "write_file", "patch_file", "edit_file", "create_file",
        "delete_file", "move_file", "rename_file",
    }))

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        last = ctx.tool_calls_log[-1]
        tool = last.get("tool", "")
        success = last.get("success")

        # 只检查成功的文件变更工具调用
        if tool not in self._FILE_MUTATION_TOOLS or success is not True:
            return ReinDecision()

        # delete_file 删除后无内容可检，直接跳过
        if tool == "delete_file":
            return ReinDecision()

        args = last.get("args") or {}
        # path 优先用目标路径（move/rename 后的真实路径），否则用源路径
        path = (
            args.get("destination")
            or args.get("path")
            or args.get("file_path")
            or ""
        )
        if not path:
            return ReinDecision()

        # 获取文件内容：
        # - patch_file: new_string 是片段，必须读磁盘完整文件检查
        # - write_file/create_file: 用 args.content（完整文件内容）
        # - move_file/rename_file: 无 args.content，必须读磁盘目标文件
        # - 其他: 尝试 args.content，再回退读磁盘
        if tool == "patch_file":
            # patch_file 的 new_string 是替换片段，不能直接做语法检查
            # 必须读取磁盘上的完整文件
            if not ctx.workspace_path:
                return ReinDecision()
            import os
            full_path = os.path.join(ctx.workspace_path, path) if not os.path.isabs(path) else path
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                return ReinDecision()
        else:
            content = args.get("content") or ""
            if not content and ctx.workspace_path:
                import os
                full_path = os.path.join(ctx.workspace_path, path) if not os.path.isabs(path) else path
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    return ReinDecision()

        if not content:
            return ReinDecision()

        # 按扩展名做语法检查
        error_msg = await self._check_syntax(path, content)
        if not error_msg:
            # 验证通过：移除该文件的语法错误记录
            errors: list = ctx.metadata.get("syntax_errors") or []
            ctx.metadata["syntax_errors"] = [e for e in errors if e.get("path") != path]
            return ReinDecision()

        # 记录语法错误（按文件去重，更新最新错误）
        errors: list = ctx.metadata.setdefault("syntax_errors", [])
        # 移除该文件的旧错误，追加新错误
        errors[:] = [e for e in errors if e.get("path") != path]
        errors.append({"path": path, "error": error_msg, "iteration": ctx.iteration})

        # 注入错误信息，驱动 Agent 修复
        # 修复：使用 "user" 角色而非 "system"，避免添加到消息末尾时触发 400 错误
        return ReinDecision(
            intercepted=False,
            inject_messages=[{
                "role": "user",
                "content": (
                    f"[语法检查失败] {path} 存在语法错误：\n```\n{error_msg}\n```\n"
                    f"请立即修复此语法错误，确保代码可运行后再继续任务。"
                ),
            }],
        )

    async def _check_syntax(self, path: str, content: str) -> str:
        """按文件扩展名做语法检查，返回错误信息（空字符串表示通过）"""
        import os
        ext = os.path.splitext(path)[1].lower()

        if ext == ".py":
            return self._check_python(content)
        elif ext == ".json":
            return self._check_json(content)
        elif ext in (".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".cjs"):
            # H13 修复 B6：TS/JS 文件用 tsc --noEmit 检查
            # .vue 文件含 SFC 结构，tsc 无法直接检查，跳过
            if ext == ".vue":
                return ""
            return await self._check_tsc(path)
        else:
            return ""

    @staticmethod
    async def _check_tsc(path: str) -> str:
        """用 tsc --noEmit 检查 TS/JS 文件语法（H13 修复 B6）

        策略：
        - 用 shutil.which 探测 tsc 是否可用（一次探测后缓存到类属性）
        - 不可用 → 跳过（返回空字符串）
        - 可用 → 在线程池中调用 tsc --noEmit <path>（asyncio.to_thread，
          避免同步 subprocess 阻塞 event loop），捕获输出
        - 失败 → 返回错误信息
        - 成功 → 返回空字符串

        注意：
        - tsc 检查的是磁盘文件（不是 content 参数），调用前需确保文件已落盘
        - 用 asyncio.to_thread 包装同步 subprocess.run，防止阻塞 event loop
        - tsc 不可用时静默跳过，不阻塞 Agent
        """
        import os
        import shutil
        import subprocess

        # 缓存 tsc 可用性（类属性，进程级缓存）
        if not hasattr(SyntaxVerifier, "_tsc_available"):
            SyntaxVerifier._tsc_available = shutil.which("tsc") is not None
            if SyntaxVerifier._tsc_available:
                logger.info("SyntaxVerifier: 检测到 tsc，TS/JS 文件将做语法检查")
            else:
                logger.debug("SyntaxVerifier: 未检测到 tsc，TS/JS 文件跳过语法检查")

        if not SyntaxVerifier._tsc_available:
            return ""

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["tsc", "--noEmit", "--pretty", "false", path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                # tsc 输出格式：path(line,col): error TSxxxx: message
                return result.stdout or result.stderr or "tsc 检查失败"
            return ""
        except subprocess.TimeoutExpired:
            return "tsc 检查超时（10s）"
        except Exception as e:
            # tsc 调用失败 → 静默跳过，不阻塞 Agent
            logger.debug("tsc 检查异常 %s: %s", path, e)
            return ""

    @staticmethod
    def _check_python(content: str) -> str:
        try:
            import ast
            ast.parse(content)
            return ""
        except SyntaxError as e:
            return f"SyntaxError (line {e.lineno}): {e.msg}\n{e.text or ''}"

    @staticmethod
    def _check_json(content: str) -> str:
        try:
            import json
            json.loads(content)
            return ""
        except json.JSONDecodeError as e:
            return f"JSONDecodeError (line {e.lineno}, col {e.colno}): {e.msg}"


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

    # 文件变更工具集（与 driver._FILE_MUTATION_TOOLS 对齐）
    _FILE_MUTATION_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "write_file", "patch_file", "edit_file", "create_file",
        "delete_file", "move_file", "rename_file",
    }), init=False, repr=False)
    # 只读工具集：多次读取不同文件是合理的工作密度，应给予预算
    _READONLY_TOOLS: frozenset = field(default_factory=lambda: frozenset({
        "read_file", "list_dir", "grep", "glob",
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
            explicit_ids = set(ctx.skill_ids or [])
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

    # 语法验证（POST_TOOL_CALL）
    if config is None or config.enable_syntax_verify:
        interceptors.append(SyntaxVerifier())

    # 动态步数预算（POST_ITERATION）
    if config is None or config.enable_dynamic_budget:
        budget_rein = DynamicBudgetRein()
        if config is not None:
            budget_rein.file_mutation_interval = config.dynamic_budget_interval
            budget_rein.step_per_interval = config.dynamic_budget_step
            budget_rein.max_multiplier = config.dynamic_budget_cap
        interceptors.append(budget_rein)

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
