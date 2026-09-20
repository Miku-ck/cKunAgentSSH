"""PermissionGate — 三态授权门与进程级单例。拆分自原 reins.py。"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.audit import append_record_async
from config import ALLOWLIST_PATH

from ..contracts import ReinDecision, ReinPoint, RunContext, TraceEvent
from .core import (
    _COMMAND_EXEC_TOOLS,
    _PERMISSION_APPROVAL_TIMEOUT_S,
    _audit_target_id,
    _audit_target_type,
    _perm_pending,
    permission_display_fields,
    tool_call_signature,
)

logger = logging.getLogger(__name__)

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
    # once 授权严格按 run_id 键控（grant 对空 run_id 直接拒绝，无全局池）。
    # _confirmed_once[run_id][grant_key] = count（某 run 授权了 N 次同一 grant_key）
    _confirmed_once: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    # C-review M11：会话级确认按 conversation_id 键控（原全局 set — 会话 A 的
    # session 授权会泄漏到会话 B 的 run 中免审）。进程级单例共享不变，
    # 但"会话"边界 = conversation_id
    _confirmed_session: dict[str, set[str]] = field(default_factory=dict)  # conversation_id → 确认键
    _confirmed_permanent: set[str] = field(default_factory=set)  # 永久确认（持久化到 config）
    # 用户覆盖（从 DB 加载）
    _overrides: dict[str, str] = field(default_factory=dict)
    # 问题 4：visible_terminal 拒绝命令 — 按 run_id 键控，防止 LLM 重发时再次弹卡片。
    # _denied_commands[run_id] = set(grant_key)，拒绝后 LLM 重试不会再次触发审批，
    # 而是直接拦截并注入"已被用户拒绝"提示（让 LLM 换路）。
    # 运行终止时由 ReflectionRein 自动清理（与 cleanup_run_once 同源）。
    _denied_commands: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def __post_init__(self) -> None:
        self._confirmed_permanent = self._load_permanent_allowlist()

    @staticmethod
    def _validate_allowlist_entry(line: str, lineno: int) -> tuple[bool, str]:
        """校验单条 allowlist 条目格式。

        合法格式：
          1. 纯工具名：^[A-Za-z0-9_.-]+$
          2. 工具名 + 命令哈希：^[A-Za-z0-9_.-]+:[0-9a-f]{16}$

        Returns: (valid, reason_if_invalid)
        """
        import re
        _TOOL_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+$')
        _GRANT_KEY_RE = re.compile(r'^[A-Za-z0-9_.-]+:[0-9a-f]{16}$')
        if _GRANT_KEY_RE.match(line):
            return True, ""
        if _TOOL_NAME_RE.match(line):
            return True, ""
        return False, (
            f"allowlist 第 {lineno} 行格式无效: {line!r}（"
            f"期望工具名如 'my_tool' 或 工具:16位hex哈希 如 'shell_exec:a1b2c3d4e5f67890'）"
        )

    def _load_permanent_allowlist(self) -> set[str]:
        """从 config 加载永久 allowlist（~/.ckunagentic/allowlist.txt）。

        逐行校验格式，不合法行记录 warning 日志后跳过。
        空行和 '#' 注释行被忽略。
        """
        import os
        path = ALLOWLIST_PATH
        if not os.path.exists(path):
            return set()
        result: set[str] = set()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for lineno, raw_line in enumerate(f, start=1):
                    line = raw_line.strip()
                    if not line or line.startswith('#'):
                        continue
                    valid, reason = self._validate_allowlist_entry(line, lineno)
                    if not valid:
                        logger.warning(reason)
                        continue
                    result.add(line)
        except Exception as e:
            logger.warning("读取 allowlist 失败: %s: %s", path, e)
        return result

    def _save_permanent_allowlist(self) -> None:
        """保存永久 allowlist"""
        import os
        path = ALLOWLIST_PATH
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
            from ..safety import normalize_command_for_detection
            digest = hashlib.sha256(
                normalize_command_for_detection(str(command)).encode("utf-8")
            ).hexdigest()[:16]
            return f"{tool_name}:{digest}"
        return tool_name

    def check_permission(self, tool_name: str, args: dict,
                         risk: Optional[str] = None,
                         conversation_id: str = "",
                         run_id: str = "") -> bool:
        """检查权限，返回是否放行

        risk: 调用方已判定的风险等级；risk == "dangerous" 时忽略 session 级缓存
              （B-6：落实 "dangerous 需每次授权" 契约，只认 once 与 always/permanent 键）

        检查顺序：permanent allowlist → session（非 dangerous，按会话键控）
        → once 计数器（优先 run_id 作用域，fallback 全局池）。原 scope 参数已移除
        （C-review NIT：死参数，行为实际由 risk + 已存在的授权决定，与传入的 scope 无关）。

        run_id: once 授权严格按 run_id 作用域匹配（问题 1 改进的收尾）。
               原实现保留一个空 run_id 的"全局池" fallback，兼容前端不传
               run_id 的旧调用模型 — 代价是 A run 未消费的 once 授权会被
               B run 消费（跨 run 竞态），且全局池不参与 run 终态清理。
               现 grant/deny 端点强制要求 run_id（缺失 400），此 fallback 已删除。

        无交互式 UI 场景：默认拦截 dangerous 命令，除非已在 allowlist 中。
        命令执行工具按"工具+命令哈希"键检查（审查 M-3）。
        """
        command = None
        if tool_name in _COMMAND_EXEC_TOOLS and isinstance(args, dict):
            command = args.get("command") or args.get("shell") or args.get("cmd")
        key = self._grant_key(tool_name, command)
        # 问题 4：visible_terminal 拒绝标记检查 — 用户明确拒绝的命令不再免审
        # （即使永久 allowlist 中有该命令，当前 run 的拒绝状态也优先）
        if self.is_denied(tool_name, command, run_id):
            return False
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
        # once 授权只认当前 run_id 作用域池（空 run_id 全局池 fallback 已删除）
        run_pool = self._confirmed_once.get(run_id)
        if run_pool and run_pool.get(key, 0) > 0:
            run_pool[key] -= 1
            if run_pool[key] <= 0:
                del run_pool[key]
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
              conversation_id: str = "", run_id: str = "") -> str:
        """授予权限，返回实际写入的授权键

        scope: 'once' | 'session' | 'always'
        command: 命令执行工具时传入被批准的具体命令（审查 M-3 命令级绑定）
        conversation_id: C-review M11 — session 授权绑定到发起授权的会话
        run_id: 问题 1 改进 — once 授权绑定到发起授权的 run_id（消除跨 run 竞态）
        """
        key = self._grant_key(tool_name, command)
        if scope == 'once':
            # once 授权必须绑定 run_id：无归属的授权无法被 check 消费、
            # 也无法被 run 终态清理，直接拒绝而非落入无人认领的池
            if not run_id:
                raise ValueError("scope='once' 授权必须提供 run_id")
            self._confirmed_once[run_id][key] += 1
        elif scope == 'session':
            self._confirmed_session.setdefault(conversation_id, set()).add(key)
        elif scope == 'always':
            self._confirmed_permanent.add(key)
            self._save_permanent_allowlist()
        return key

    def _revoke_key(self, key: str, conversation_id: Optional[str] = None) -> None:
        # 问题 1 改进：遍历所有 run_id 的 once 池，移除该 key
        for run_pool in list(self._confirmed_once.values()):
            run_pool.pop(key, None)
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

    def deny_command(self, tool_name: str, command: Optional[str], run_id: str) -> str:
        """记录某 run 下某命令被用户拒绝（visible_terminal 拒绝时调用）。

        后续该 run 再次请求此命令时 check_permission 直接返回 False，
        LLM 不会收到新的 permission_needed 卡片。

        Returns:
            实际标记的 grant_key
        """
        if not run_id:
            raise ValueError("deny_command 必须提供 run_id（拒绝记录按 run 隔离并随 run 终态清理）")
        key = self._grant_key(tool_name, command)
        self._denied_commands[run_id].add(key)
        return key

    def is_denied(self, tool_name: str, command: Optional[str], run_id: str) -> bool:
        """检查某 run 下某命令是否已被用户拒绝（严格 run_id 作用域，不查全局池）。"""
        key = self._grant_key(tool_name, command)
        return key in self._denied_commands.get(run_id, set())

    def cleanup_run_once(self, run_id: str) -> int:
        """释放指定 run_id 的全部 once 授权。

        在 run 终止（completed/failed/max_reached）时由 ReflectionRein 调用，
        防止已授权的 yet-unused once 卡子被 run 退出后的新 run 消费。

        Returns:
            释放的授权条目数量
        """
        run_pool = self._confirmed_once.pop(run_id, None)
        if not run_pool:
            return 0
        return sum(run_pool.values())

    def cleanup_run_denied(self, run_id: str) -> int:
        """释放指定 run_id 的全部拒绝命令记录。

        在 run 终止（completed/failed/max_reached）时由 ReflectionRein 调用，
        防止跨 run 的拒绝卡子残留。

        Returns:
            释放的拒绝条目数量
        """
        denied_set = self._denied_commands.pop(run_id, None)
        if not denied_set:
            return 0
        return len(denied_set)

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

    def load_overrides(self, overrides: dict[str, str]) -> None:
        """从 DB 加载用户自定义权限覆盖"""
        self._overrides.update(overrides)

    def _get_effective_risk(self, tool_name: str, meta: Any, args: dict,
                            environment: str) -> str:
        """计算工具在特定参数和环境下的有效风险等级（问题 5）。

        规则：
        - base risk = meta.risk_level
        - 当 environment == "prod" 且 meta.destructive_actions 非空：
          若 args["action"] 命中 destructive_actions 集合，
          将风险升级为 "dangerous"（即使 meta.risk_level == "moderate"）。
        - 其他情况返回 meta.risk_level。

        设计目标：service_manage 等 moderate 工具的 stop/restart 操作在
        生产环境不会因 session 级授权而免审，每次都需要显式批准。
        """
        base = meta.risk_level
        if not args or not isinstance(args, dict):
            return base
        destructive = getattr(meta, "destructive_actions", None)
        if not destructive:
            return base
        if environment != "prod":
            return base
        action = args.get("action")
        if action and action in destructive:
            return "dangerous"
        return base

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

        # 问题 5：prod 环境下，对声明了 destructive_actions 的工具，
        # 若当前调用的 action 命中该集合，风险等级升级为 dangerous
        # （确保 stop/restart 等操作不走 session 级缓存，每次都要审批）
        environment = getattr(ctx, "environment", "") or ""
        risk = self._get_effective_risk(tool, meta, args, environment)
        reversible = meta.reversible

        # safe → 自主放行
        if risk == "safe":
            return ReinDecision()

        # moderate + 可逆 → 自动放行（可逆操作无需审批）
        if risk == "moderate" and reversible:
            return ReinDecision()

        # dangerous → 需每次授权（不可 session 级缓存，防止首次授权后 Agent 执行任意高危命令）
        if risk == "dangerous":
            # prod 强化（合规）：与 FilesystemGuard 的 shell 命令对齐 — 生产环境
            # 危险操作仅接受【预授权 allowlist】，禁止临时一次审批（once/session）。
            # 原实现 prod 下 non-shell 危险工具（如 service_manage stop/restart）
            # 走临时审批，用户点"始终允许"即写入永久 allowlist 后免审，
            # 与 shell 命令"prod 必须预登记"的合规姿态不一致（第 8 轮审查修复）。
            if environment == "prod" and not self.is_permanently_allowed(tool, args):
                await append_record_async(
                    action=tool, conclusion="denied",
                    run_id=ctx.run_id, iteration=ctx.iteration,
                    target_type=_audit_target_type(ctx),
                    target_id=_audit_target_id(ctx),
                    environment=environment,
                    command_raw=str(args.get("action") if isinstance(args, dict) else ""),
                    result=f"prod_deny_no_allowlist: {tool} 未在 allowlist 预授权",
                )
                return ReinDecision(
                    intercepted=True,
                    intercept_reason=f"生产环境危险工具未预授权: {tool}",
                    events=[TraceEvent(
                        kind="warning",
                        source="reins.permission_gate",
                        run_id=ctx.run_id,
                        iteration=ctx.iteration,
                        payload={
                            "tool": tool,
                            "message": (
                                f"[生产强化] 生产环境危险工具 {tool} 仅接受预授权 allowlist 放行。"
                                "拒绝临时审批。请预先在权限 allowlist 中登记后重试。"
                            ),
                        },
                    )],
                    inject_messages=[{
                        "role": "user",
                        "content": (
                            f"[系统拦截] 当前目标为生产环境，危险工具 {tool} 需预先在"
                            "权限 allowlist 中登记后方可执行，此处拒绝临时授权。"
                            "请改用更安全的操作，或提示用户预先登记 allowlist。"
                        ),
                    }],
                )
            # C-review NIT：原 scope='once' 为死参数 — dangerous 每次授权的
            # 语义由 risk 分支保证（忽略 session 缓存，只认 once/permanent 键）
            if self.check_permission(tool, args, risk='dangerous',
                                      conversation_id=ctx.conversation_id,
                                      run_id=ctx.run_id):
                return ReinDecision()
        else:
            # moderate 不可逆 → session 级授权可接受（风险有限）
            if self.check_permission(tool, args,
                                     conversation_id=ctx.conversation_id,
                                     run_id=ctx.run_id):
                return ReinDecision()

        # 未授权 → 发 permission_needed 事件 + 拦截
        # C-review M7：与 FilesystemGuard 相同的审批三态处理（首次发卡片 /
        # 窗口内重发豁免循环检测 / 超时按拒绝）
        pending = _perm_pending(ctx)
        sig = tool_call_signature(last)
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
                # 卡片可读性字段（与 FilesystemGuard 的卡片信息保持一致）：
                # 本路径只命中非命令工具（无 command 参数），故 command 留空，
                # 前端 isCommandTool 判断下「授权命令」行自然隐藏。
                # 工具名 → 中文说明的后果表尚未收录（工具级风险等级已由
                # ToolMeta 声明），本期只补上下文不补 explain。
                **permission_display_fields(ctx),
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


# ──────────────────────────────────────────────────────────────────────────────
