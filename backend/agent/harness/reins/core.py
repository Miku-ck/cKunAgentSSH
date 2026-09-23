"""Reins 容器与共享常量/助手 — 拆分自原 reins.py（纯搬家，无语义变更）。"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..contracts import Interceptor, ReinDecision, ReinPoint, RunContext

logger = logging.getLogger(__name__)

# 命令执行工具：审批必须绑定命令内容（审查 M-3），工具级授权不得覆盖命令级审批。
# job_run 同属命令入口（local=本地子进程 / remote=远程命令），其 `cmd` 参数
# 必须经 classify_command 精细判定，wrapper 不参与分类，防止内部 dangerous
# 命令借工具级 moderate 一刀切漏审。
# 本集合是「模型自由输入命令」的入口清单，不要往里加工具名来扩展内容级判定 —
# 自身拼命令的工具（file_write/file_edit/schedule_op）实现 BaseTool.bound_command
# 即可，由 extract_bound_command 统一取出，口径区分见该函数 docstring。
_COMMAND_EXEC_TOOLS = {"shell_exec", "job_run"}

# run 请求经 ctx.metadata 传入的权限模式键。harness 写入、plan_mode/auto_approve
# 读取，三处共用同一字面量——单一来源以防写侧与读侧漂移导致权限模式静默失效
# （harness 用 plan_mode 的引用写入，auto_approve 用自己的引用读取，历史上是两份
# 各自定义的 "permission_mode"，值一旦不一致 auto_approve 将永远匹配不上）。
MODE_KEY = "permission_mode"

# 审批窗口兜底时长（秒）。重构后审批不再设 120s 短超时 —— 未授权签名保持等待至
# grant/deny/会话结束。这里仅保留一个生产可配置的**兜底**（默认 600s），到期时
# 走 permission_expired（前端把卡片标记 timeout 并保留），但**不移出等待集**、
# 不按永久拒绝处理，兜底窗口内同命令重发复用待办、不重复建卡。
# 初值来自环境变量 CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S（config 已把
# .env 注入 os.environ）；设置面板 PUT /settings/permission-policy 经
# set_approval_fallback_seconds 即时改写。**消费方必须走 getter 调用** ——
# `from .core import 常量` 拿到的是导入期快照，PUT 后 guards/gate 看不见
# （跨层 key 漂移家族，钉在 test_gate_兜底时长过期判定走getter）。
# 0 = 禁用兜底（永久等待）。上限 86400s：再大的值与"永久"在使用上没区别。
APPROVAL_FALLBACK_MAX_S = 86400.0
_APPROVAL_FALLBACK_DEFAULT_S = 600.0
_approval_fallback_s = float(os.getenv(
    "CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S", "600.0"))


def approval_fallback_seconds() -> float:
    """审批兜底等待时长的当前生效值（秒）。0 = 永久等待。"""
    return _approval_fallback_s


def set_approval_fallback_seconds(value) -> float:
    """设置兜底时长：脏值回默认，越界夹紧。Returns: 实际生效值。"""
    global _approval_fallback_s
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = _APPROVAL_FALLBACK_DEFAULT_S
    _approval_fallback_s = max(0.0, min(v, APPROVAL_FALLBACK_MAX_S))
    return _approval_fallback_s


def _perm_pending(ctx: RunContext) -> dict:
    """C-review M7：「等待审批」签名表（ctx.metadata，per-run 隔离）

    key = tool_call_signature() 返回的 hashable 元组，
    value = 首次发出 permission_needed 的 time.monotonic() 时间戳。
    窗口内的重发豁免循环检测；抵达兜底（approval_fallback_seconds()）后的
    重发发 permission_expired（保留等待集，不 pop），但仍复用待办、不重复建卡。
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


def permission_display_fields(ctx: "RunContext", command: str = "") -> dict[str, Any]:
    """审批卡片的「用户在看什么」上下文：环境 / 用户原任务 / 目标会话 / 命令原文。

    纯展示字段，不参与任何授权判定（grant_key 仍按 工具+命令哈希 绑定）。
    给 FilesystemGuard 与 PermissionGate 两条审批路径共用，保证两侧卡片信息一致。
    task 截断 200 字符：卡片引用块显示得下即可，同时避免超长任务撑大事件 payload。
    """
    return {
        "environment": getattr(ctx, "environment", "") or "test",
        "task": str(getattr(ctx, "task", "") or "")[:200],
        "ssh_session_id": getattr(ctx, "ssh_session_id", "") or "",
        "command": command,
    }


def command_from_args(args: Any) -> str:
    """从工具参数里取 shell 命令串（shell_exec 用 command、job_run 用 cmd）。"""
    if not isinstance(args, dict):
        return ""
    return args.get("command") or args.get("shell") or args.get("cmd") or ""


def extract_bound_command(registry: Any, tool: str, args: Any) -> tuple[str | None, bool]:
    """取「本次调用将要真实执行的命令」+ 该命令是否为模型自由输入。

    为什么需要它：safety.classify_command 原先只挂在 shell_exec/job_run 一条路上
    （_COMMAND_EXEC_TOOLS）。但 file_write 会自己拼出
    `printf '%s' '<b64>' | base64 -d > /root/.ssh/authorized_keys` 落地 —— 同一条
    命令手敲是 hardline 无条件阻断，走工具却只是 moderate（会话授权即可，"始终允许"
    后含 prod 永久免审）。让工具自己报出真实命令，两条路径自此共用同一判定。

    Returns:
        (命令, 是否自由输入)；(None, False) 表示无命令可判 — 调用方退回工具级
        ToolMeta.risk_level 口径。自由输入走 classify_command（含「未入只读白名单
        即审批」的默认策略），客户端自造模板走 classify_constructed_command。

    registry 为 None（如未注入 saddle 的单测环境）时，仅 _COMMAND_EXEC_TOOLS 生效，
    与修复前行为一致。bound_command 抛异常不做兜底：FilesystemGuard 是
    safety_critical 拦截器，异常按 fail-closed 拦截，此处吞掉反而变成放行。
    """
    if tool in _COMMAND_EXEC_TOOLS:
        cmd = command_from_args(args)
        return (cmd or None), True
    get = getattr(registry, "get", None)
    impl = get(tool) if callable(get) else None
    bound_command = getattr(impl, "bound_command", None)
    if not callable(bound_command):
        return None, False
    bound = bound_command(args if isinstance(args, dict) else {})
    if not isinstance(bound, str) or not bound.strip():
        return None, False
    return bound, bool(getattr(impl, "bound_command_is_free_form", False))


def resolve_registry(ctx: RunContext, saddle: Any) -> Any:
    """取 per-run ToolRegistry（extract_bound_command 的第一个参数来源）。

    优先 ctx.metadata["saddle"]：PermissionGate 是进程级单例，其 saddle 字段会被
    并发 run 互相覆盖（见 gate._lookup_meta 的同款处理），拿错注册表会把 A run 的
    工具元数据用在 B run 上。取不到返回 None（内容级判定自动退场，不报错）。
    """
    resolved = (getattr(ctx, "metadata", None) or {}).get("saddle") or saddle
    return getattr(resolved, "registry", None)


def tool_call_signature(call: dict[str, Any]) -> tuple:
    """生成规范化的工具调用签名（原 LoopDetector._signature 实现体，拆至 core 供 guards/gate/flow 共用）

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
                    # 审查R2修复：此前所有未拦截拦截器已聚合的 events/
                    # inject_messages 在早退分支被整体丢弃 — 澄清答案注入
                    # （priority 7 放行轮）随后被 FilesystemGuard 拦截时蒸发，
                    # LoopDetector 纠偏同灭。合并进返回决策再退出。
                    if events or inject_messages:
                        decision.events = events + list(decision.events)
                        decision.inject_messages = inject_messages + list(decision.inject_messages)
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
