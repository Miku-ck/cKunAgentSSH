"""命令/文件守卫与审计落盘拦截器 — 拆分自原 reins.py。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.audit import append_record_async

from ..command_explain import explain_for_reason, maybe_explain_via_llm
from ..contracts import ReinDecision, ReinPoint, RunContext, TraceEvent
from ..safety import classify_command, classify_constructed_command
from .auto_approve import try_auto_approve
from .core import (
    _audit_environment,
    _audit_target_id,
    _audit_target_type,
    _perm_pending,
    _target_host,
    approval_fallback_seconds,
    command_from_args,
    extract_bound_command,
    permission_display_fields,
    resolve_registry,
    tool_call_signature,
)

logger = logging.getLogger(__name__)

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
    # 取 ToolRegistry 用：让实现 bound_command 的工具（file_write 等）也进内容级判定
    saddle: Optional[Any] = None

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not ctx.tool_calls_log:
            return ReinDecision()

        # 取最近一次工具调用（Driver 在 PRE_TOOL_CALL 时已记录）。
        # 并行段内 [-1] 已不是当前条目 → 统一走 ctx.active_tool_call
        # （未显式设置时回退 [-1]，串行语义逐字不变）。
        last = ctx.active_tool_call
        if not last:
            return ReinDecision()

        tool = last.get("tool", "")
        args = last.get("args", {})

        # 内容级判定 — 委托给 safety 模块分类：
        # 命令入口工具（shell_exec 的 command / job_run 的 cmd）取自由文本参数，
        # 自身拼命令的工具（file_write/file_edit/schedule_op）取 bound_command 报出的
        # 真实命令。原实现只认 _COMMAND_EXEC_TOOLS，于是写 authorized_keys 走
        # file_write 判 moderate、走 shell_exec 判 hardline —— 同一副作用两种结论。
        bound, is_free_form = extract_bound_command(
            resolve_registry(ctx, self.saddle), tool, args
        )
        if bound:
            level, reason = (classify_command if is_free_form
                             else classify_constructed_command)(bound)
            if level == "hardline":
                # 无条件阻断，不询问 PermissionGate；纠偏注入落在消息列表末尾，
                # 故角色取 user 而非 system（400 约束口径见 bridle._build）
                cmd = bound
                await append_record_async(
                    action=tool,
                    conclusion="blocked",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    target_type=_audit_target_type(ctx),
                    target_id=_audit_target_id(ctx),
                    environment=_audit_environment(ctx),
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
                cmd = bound
                if self.permission_gate is None:
                    await append_record_async(
                        action=tool, conclusion="denied",
                        run_id=ctx.run_id, iteration=ctx.iteration,
                        target_type=_audit_target_type(ctx),
                        target_id=_audit_target_id(ctx),
                        environment=_audit_environment(ctx),
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
                env = _audit_environment(ctx)
                if env == "prod":
                    if not self.permission_gate.is_permanently_allowed(
                            tool, args, bound_command=bound, host=_target_host(ctx)):
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
                    # 问题 1 改进：同时传入 run_id 以支持 once 作用域隔离
                    allowed = self.permission_gate.check_permission(
                        tool, args, risk="dangerous", conversation_id=ctx.conversation_id,
                        run_id=ctx.run_id, bound_command=bound,
                        host=_target_host(ctx))
                    if not allowed:
                        # 自动审批（permission_mode="auto"）：弹卡前先让模型裁决一次，
                        # ALLOW 放行 / 其余照旧弹卡（fail-closed，见 auto_approve 模块）。
                        # 位置约束：① 在 pending 记账之前 —— 放行不该留半张等待签名；
                        # ② 在 is_denied 兜底之前先做同一检查 —— 用户明确拒绝过的命令
                        # 不能被模型翻案，用户否决永远高于裁决。
                        if not self.permission_gate.denied_for_call(
                                tool, args, ctx.run_id,
                                str(cmd) if cmd else None):
                            decision = await try_auto_approve(
                                ctx, tool, args, reason or "",
                                bound_command=str(cmd or ""))
                            if decision is not None:
                                return decision
                        # C-review M7：审批等待三态处理（无审批超时、重发被循环检测
                        # 抢截、授权卡片孤儿的问题由此修复）
                        # 重构：审批不再设 120s 短超时 — 未授权签名保持等待至
                        # grant/deny/会话结束；仅兜底时长生 permission_expired 把卡
                        # 标记 timeout 并保留待办，不 pop、不按永久拒绝处理。
                        pending = _perm_pending(ctx)
                        sig = tool_call_signature(last)
                        first_ts = pending.get(sig)
                        # 时长现查 getter（设置面板可即时改），不吃导入期快照；
                        # >0 才判过期与 gate 审查R 同口径（0 = 永久等待，否则首次重发即过期）
                        fallback_s = approval_fallback_seconds()
                        if first_ts is not None and fallback_s > 0 and \
                                (time.monotonic() - first_ts) > fallback_s:
                            # 兜底到期 → 保留等待集（不 pop），发 permission_expired
                            # 让前端标记卡片 timeout 保留；注入提示引导用户确认后
                            # 重新发起。等待集未移除，兜底窗口内同命令重发仍被拦截、
                            # 复用待办而不重复建卡（也不会被循环检测抢截）。
                            await append_record_async(
                                action=tool, conclusion="denied",
                                run_id=ctx.run_id, iteration=ctx.iteration,
                                target_type=_audit_target_type(ctx),
                                target_id=_audit_target_id(ctx),
                                environment=env,
                                command_raw=str(cmd),
                                result=f"approval_expired: {reason}",
                            )
                            return ReinDecision(
                                intercepted=True,
                                intercept_reason=f"审批已过期（保留待办，等待确认）: {reason}",
                                events=[TraceEvent(
                                    kind="permission_expired",
                                    source="reins.filesystem_guard",
                                    run_id=ctx.run_id,
                                    iteration=ctx.iteration,
                                    payload={
                                        "tool": tool,
                                        "args": args,
                                        # 前端按「工具 + 命令」定位卡片（同一工具针对不同
                                        # 目标各一张卡），缺了它会把无关卡片一起标成过期
                                        "command": str(cmd),
                                        "message": (
                                            f"授权审批已过期（{int(fallback_s)}s 兜底未响应），"
                                            f"待办已保留，可重新确认后执行: {tool}"
                                        ),
                                    },
                                )],
                                inject_messages=[{
                                    "role": "user",
                                    "content": (
                                        f"[系统拦截] 上述命令的授权审批已过期"
                                        f"（{int(fallback_s)}s 兜底无用户响应），"
                                        "系统已保留待办、未按拒绝处理。请提示用户确认后重新发起"
                                        "完全相同的命令（授权后会被放行），或改用更安全的替代方案。"
                                    ),
                                }],
                            )
                        is_first = first_ts is None
                        # 审查R2修复：is_denied 兜底原先落在 pending[sig] 记账之后 —
                        # 被拒命令一旦走到这里，签名已永久驻留 permission_pending：
                        # 该命令从此豁免循环检测，模型可无限刷「已被拒绝」拦截直到
                        # max_iterations。拒绝判定前移，被拒即返回、不再记账。
                        # 问题 4：visible_terminal 拒绝过该命令 → 不再发卡片，直接拒绝
                        # （用户已明确点过"拒绝"，重复卡片是无意义的骚扰）
                        if self.permission_gate and self.permission_gate.is_denied(tool, str(cmd) if cmd else None, ctx.run_id):
                            pending.pop(sig, None)
                            return ReinDecision(
                                intercepted=True,
                                intercept_reason=f"命令曾被用户拒绝: {cmd}",
                                events=[TraceEvent(
                                    kind="warning",
                                    source="reins.filesystem_guard",
                                    run_id=ctx.run_id,
                                    iteration=ctx.iteration,
                                    payload={
                                        "tool": tool,
                                        "message": (
                                            f"命令 \"{cmd if cmd else tool}\" 在本次会话中曾被用户拒绝执行。"
                                            "系统已自动拦截。请改用更安全的替代方案，"
                                            "或提示用户确认后再重新发起。"
                                        ),
                                    },
                                )],
                                inject_messages=[{
                                    "role": "user",
                                    "content": (
                                        f"[系统拦截] 命令 \"{cmd if cmd else tool}\" "
                                        "在本次会话中曾被用户拒绝执行。系统已自动拦截该命令，"
                                        "不会再次请求审批。请：\n"
                                        "1. 不要再重试该命令；\n"
                                        "2. 改用更安全的替代方案；\n"
                                        "3. 如确需执行，提示用户手动在终端中运行。"
                                    ),
                                }],
                            )
                        # 审查 M-3 修复：原实现只发 "warning" 事件，前端没有可操作的授权卡片，
                        # 用户根本无法批准 dangerous 命令（授权闭环断裂）。
                        # 改发 permission_needed（payload 含 tool/args/command），前端
                        # AgentRunPanel 渲染授权卡片，用户选择 本次/会话/始终（绑定命令）。
                        # C-review M7：仅首次请求发卡片（前端本就按 工具+命令 去重）；
                        # 审批窗口内的重发只拦截 + 注入等待提示，不重复发事件。
                        if is_first:
                            pending[sig] = time.monotonic()
                        # 问题 2 改进：计算归一化展示命令，让用户看到实际授权的命令形态
                        from ..safety import get_normalized_for_display
                        display_cmd = get_normalized_for_display(str(cmd)) if cmd else ""
                        # 英文 reason 对小白不可读，附一条中文「后果说明」
                        # （纯展示，不参与放行判定与审计 result）
                        expl = explain_for_reason(reason or "")
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
                                "normalized_command": display_cmd,
                                # 卡片可读性字段：环境 / 用户原任务 / 目标会话 /
                                # 命令原文 + 中文后果说明（见 command_explain）
                                **permission_display_fields(ctx, str(cmd)),
                                "explain": expl.text,
                                "explain_source": expl.source,
                            },
                        )] if is_first else []
                        if is_first and expl.needs_llm:
                            # 未知命令族：卡片已带静态文案弹出，这里异步补一条
                            # 模型生成的中文解释（不等模型返回，不阻塞审批链）
                            await maybe_explain_via_llm(ctx, tool, str(cmd), display_cmd, env)
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
                            # 卡片已弹出 → 本次调用要等人。Driver 拿这把键登记等待项，
                            # grant / deny 端点算出同一把键（都是 _grant_key）把它唤醒。
                            # 重发（is_first=False，无新卡片）同样带上：同一张卡片的
                            # 等待项就是同一把键，重复唤醒无害。
                            approval_key=self.permission_gate.approval_key_for_call(
                                tool, args, bound),
                        )
            # safe → 放行

        return ReinDecision()


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
        last = ctx.active_tool_call
        if last is None:
            return ReinDecision()
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
            tool_name = str(last.get("tool", ""))
            try:
                # executed 记录得说得出「实际执行了什么」：file_write / service_manage /
                # schedule_op 这类工具自己拼命令，而 command_from_args 只认 command/shell/cmd
                # 三个槽位 ⇒ 恒为空串，审计链里那条 executed 点不出被改的路径、被停的服务，
                # 等保取证缺的正是在这一环。与 gate/guard 共用同一个提取器，三条路径一口径。
                bound, _is_free_form = extract_bound_command(
                    resolve_registry(ctx, None), tool_name, args
                )
            except Exception:
                # 提取失败只退回旧口径：审计宁可粗，不能因这里抛错而整条丢掉
                bound = None
            command = str(bound) if bound else str(command_from_args(args))
            await append_record_async(
                action=tool_name,
                conclusion="executed" if last.get("success") else "failed",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=_audit_environment(ctx),
                command_raw=command,
            )
        except Exception as e:
            logger.warning("审计 executed/failed 记录失败（不阻塞）: %s", e)
        return ReinDecision()


# ──────────────────────────────────────────────────────────────────────────────
