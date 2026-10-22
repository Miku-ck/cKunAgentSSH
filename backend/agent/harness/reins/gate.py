"""PermissionGate — 三态授权门与进程级单例。拆分自原 reins.py。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.audit import append_record_async
from config import ALLOWLIST_PATH

from ..contracts import (
    POLICY_DENY_MARKER,
    ReinDecision,
    ReinPoint,
    RunContext,
    TraceEvent,
)
from ..safety import classify_command, classify_constructed_command
from .auto_approve import try_auto_approve
from .core import (
    _COMMAND_EXEC_TOOLS,
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

# allowlist 文件的条目格式（一行一条，工具名或 工具名:16位hex）。读侧校验与写侧
# 校验必须共用这一份定义 —— 写侧不校验时，一条带空格/换行的工具名照样落盘，
# 面板显示「已授权」，重启后读侧按本表判非法直接丢弃，事故当场才发现预授权早没了。
# 锚位用 \Z 而不是 $：Python 的 $ 会放过「尾随换行」，带一个行尾换行的工具名照样算合法，
# 而 allowlist.txt 是一行一条 —— 放过尾随换行等于多写一个空行（读侧按空行丢弃，
# 面板却显示「已授权」）。这条格式规则同时管住读侧与写侧，所以必须严格到串尾。
_ALLOWLIST_TOOL_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+\Z')
_ALLOWLIST_GRANT_KEY_RE = re.compile(r'^[A-Za-z0-9_.-]+:[0-9a-f]{16}\Z')

# ── 主机限域条目 ──────────────────────────────────────────────────────────
# 授权键本身只有「工具 + 命令哈希」两个维度，是**全局**的：在测试机上批准过
# `systemctl restart nginx`，生产机上同一条命令同样免审 —— 而 prod 强化的说辞
# 是「危险命令必须预登记」，「在任意一台机器上登记过」并不等于「为这台机器
# 登记过」。所以 allowlist 的行格式增加主机段：
#
#   @prod-db-01|shell_exec:a1b2c3d4e5f67890   # 只对该主机生效
#   shell_exec:a1b2c3d4e5f67890               # 旧条目 = 全局（行为不变）
#
# 分隔符选 `|` 与 `@`：`|` 不在工具名/哈希/host 的任何合法字符集里，解析不会
# 歧义；`@` 前缀让「全局」和「主机限域」一眼可分，手工编辑时不会误读。
# 主机段允许 `:`（IPv6）与 `.`/`-`/`_`（域名），但不允许空白与换行 —— 一行一条
# 的格式前提比 host 字符集完整性更重要。
_SCOPED_PREFIX = "@"
_SCOPED_SEP = "|"
_ALLOWLIST_HOST_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,255}\Z')


def normalize_host(host: str) -> str:
    """主机标识归一化：去空白 + 小写。空串 = 未指定主机（全局）。

    只做大小写与首尾空白，不做端口剥离或 IPv6 缩写展开：`db-01` 与 `DB-01`
    是同一台，而 `db-01:2222` 与 `db-01:22` 在本项目里由连接配置区分，合并
    它们反而会让「给 2222 端口那台登记」变成给整台机器登记。
    """
    return (host or "").strip().lower()


def scoped_key(host: str, key: str) -> str:
    """把裸授权键包成主机限域形式；host 为空时原样返回（全局条目）。"""
    h = normalize_host(host)
    return f"{_SCOPED_PREFIX}{h}{_SCOPED_SEP}{key}" if h else key


def split_scoped_key(entry: str) -> tuple[str, str]:
    """拆出 (host, 裸键)。host 为空串表示全局条目。

    只认开头的 `@host|`；不带前缀的一律原样当裸键（旧文件无需迁移）。
    格式非法（如只有 `@|`）时不在这里报错：校验属于 _validate_entry，
    解析层只负责「拆得开就拆」。
    """
    if entry.startswith(_SCOPED_PREFIX):
        host, sep, rest = entry[1:].partition(_SCOPED_SEP)
        if sep:
            return normalize_host(host), rest
    return "", entry


def _validate_entry(line: str, lineno: int) -> tuple[bool, str]:
    """单条目格式校验（读侧与写侧的唯一判据）。"""
    host, key = split_scoped_key(line)
    if line.startswith(_SCOPED_PREFIX):
        if not host:
            return False, (
                f"allowlist 第 {lineno} 行主机段为空: {line!r}"
                "（主机限域条目格式为 @主机|工具名[:16位hex哈希]）")
        if not _ALLOWLIST_HOST_RE.match(host):
            return False, (
                f"allowlist 第 {lineno} 行主机段格式无效: {host!r}"
                "（expect [A-Za-z0-9_.:-]，不接受空白、换行或斜杠）")
    if _ALLOWLIST_GRANT_KEY_RE.match(key):
        return True, ""
    if _ALLOWLIST_TOOL_NAME_RE.match(key):
        return True, ""
    return False, (
        f"allowlist 第 {lineno} 行格式无效: {line!r}（"
        f"期望工具名如 'my_tool' 或 工具:16位hex哈希 如 'shell_exec:a1b2c3d4e5f67890'，"
        f"可加主机前缀如 '@db-01|shell_exec:a1b2c3d4e5f67890'）"
    )


def is_valid_allowlist_entry(line: str) -> bool:
    """单条目是否落在允许格式内（写侧入口用，与 _validate_allowlist_entry 同口径）"""
    return _validate_entry(line, 1)[0]

# E3：永久授权保留时长（小时）。0 = 永不过期，即 allowlist 引入 TTL 之前的行为。
# 上限与 routes/settings.py 的 Field(le=) 同口径，读取侧脏值不至于是无限大。
ALLOWLIST_TTL_MAX_HOURS = 8760

# 模块级 TTL：PermissionGate 是进程级单例，且多半在设置接口被调用之前就已建好，
# 所以 TTL 既要在单例字段上生效，也要留在模块级供之后创建的实例读到（__post_init__）。
_ALLOWLIST_TTL_HOURS = 0


def set_allowlist_ttl_hours(hours: int) -> int:
    """设置 allowlist TTL，并对已存在的单例即时生效（含清理已过期键）。

    Returns: 夹紧后的实际值。
    """
    global _ALLOWLIST_TTL_HOURS
    try:
        value = int(hours)
    except (TypeError, ValueError):
        value = 0
    _ALLOWLIST_TTL_HOURS = max(0, min(value, ALLOWLIST_TTL_MAX_HOURS))
    if _global_permission_gate is not None:
        _global_permission_gate.allowlist_ttl_hours = _ALLOWLIST_TTL_HOURS
        _global_permission_gate.prune_expired_allowlist()
    return _ALLOWLIST_TTL_HOURS


def get_allowlist_ttl_hours() -> int:
    return _ALLOWLIST_TTL_HOURS


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
    # E3：永久授权键的写入时间（epoch 秒）。allowlist.txt 里只有键、没有时间概念，
    # 时间戳落在同目录的 allowlist.meta.json —— 键文件保持一行一键的原格式，
    # 手工编辑的条目和旧版本写下的文件都不会因为多一列而解析失败。
    _permanent_granted_at: dict[str, int] = field(default_factory=dict)
    # 0 = 永不过期（默认，与引入 TTL 之前完全一致）
    allowlist_ttl_hours: int = 0
    # 问题 4：visible_terminal 拒绝命令 — 按 run_id 键控，防止 LLM 重发时再次弹卡片。
    # _denied_commands[run_id] = set(grant_key)，拒绝后 LLM 重试不会再次触发审批，
    # 而是直接拦截并注入"已被用户拒绝"提示（让 LLM 换路）。
    # 运行终止时由 ReflectionRein 自动清理（与 cleanup_run_once 同源）。
    _denied_commands: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def __post_init__(self) -> None:
        # 显式构造参数优先（测试可直接 PermissionGate(allowlist_ttl_hours=24)），
        # 未表态时才继承模块级值 —— 进程启动时从 DB 载入的那份。
        if not self.allowlist_ttl_hours:
            self.allowlist_ttl_hours = _ALLOWLIST_TTL_HOURS
        self._confirmed_permanent = self._load_permanent_allowlist()
        self._permanent_granted_at = self._load_permanent_granted_at()
        self.prune_expired_allowlist()

    @staticmethod
    def _validate_allowlist_entry(line: str, lineno: int) -> tuple[bool, str]:
        """校验单条 allowlist 条目格式（委托模块级 _validate_entry，读写同源）。

        合法格式：
          1. 纯工具名：^[A-Za-z0-9_.-]+$
          2. 工具名 + 命令哈希：^[A-Za-z0-9_.-]+:[0-9a-f]{16}$
          以上两种均可加主机前缀 `@主机|`，限定该条只对这台主机生效。

        Returns: (valid, reason_if_invalid)
        """
        return _validate_entry(line, lineno)

    def _load_permanent_allowlist(self) -> set[str]:
        """从 config 加载永久 allowlist（~/.ckunagentic/allowlist.txt）。

        逐行校验格式，不合法行记录 warning 日志后跳过。
        空行和 '#' 注释行被忽略。
        """
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

    @staticmethod
    def _allowlist_meta_path() -> str:
        """时间戳 sidecar 路径：与 allowlist.txt 同目录。

        从 ALLOWLIST_PATH 推导而不是另设 config 常量，是为了让测试只 patch 一个
        名字（ALLOWLIST_PATH）就能把两份文件一起挪进 tmp 目录。
        """
        return os.path.join(os.path.dirname(ALLOWLIST_PATH) or ".", "allowlist.meta.json")

    def _load_permanent_granted_at(self) -> dict[str, int]:
        """读取「授权键 → 写入时间」映射（allowlist.meta.json）。

        没有记录的键（升级前写入的旧行、手工登记、sidecar 写失败）用 allowlist.txt
        的 mtime 兜底：宁可按"文件最后修改时间"算一个保守的授予时间，也不要因为
        缺时间戳就把已有预授权判成"无限旧"直接撤销 —— 那会让 prod 预登记条目
        在升级后当场失效，运维在故障现场发现命令突然要审批。
        """
        mapping: dict[str, int] = {}
        path = self._allowlist_meta_path()
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    try:
                        mapping[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("读取 allowlist 时间戳失败: %s: %s", path, e)
        try:
            fallback = int(os.path.getmtime(ALLOWLIST_PATH))
        except OSError:
            fallback = int(time.time())
        for key in self._confirmed_permanent:
            mapping.setdefault(key, fallback)
        return mapping

    def _ttl_seconds(self) -> int:
        return max(0, int(self.allowlist_ttl_hours)) * 3600

    def _is_expired_permanent(self, key: str) -> bool:
        ttl = self._ttl_seconds()
        if not ttl:
            return False
        granted = self._permanent_granted_at.get(key)
        return granted is not None and granted < int(time.time()) - ttl

    def _permanent_active(self, key: str, host: str = "") -> bool:
        """永久授权命中判定，顺带就地失效到期条目。

        只查两个候选：`@host|key`（主机限域）与 `key`（全局）。全局条目是有意的
        显式授权（旧文件里的条目全部是全局，行为不变），主机条目只对那台机器
        生效 —— 这正是「测试机上批准不等于生产机免审」的落点。

        清理不止发生在加载和 TTL 变更时：进程可以长期不重启，期间把 TTL 调小
        也不该让旧键继续免审 —— 查表时兜一次，代价是一次 dict 读。
        """
        candidates = [key]
        h = normalize_host(host)
        if h:
            candidates.insert(0, scoped_key(h, key))
        for cand in candidates:
            if cand not in self._confirmed_permanent:
                continue
            if self._is_expired_permanent(cand):
                logger.info("永久授权已过期（TTL %dh），撤销: %s",
                            max(0, int(self.allowlist_ttl_hours)), cand)
                self._confirmed_permanent.discard(cand)
                self._permanent_granted_at.pop(cand, None)
                continue
            return True
        return False

    def prune_expired_allowlist(self) -> int:
        """撤销全部已过期的永久授权并落盘，返回清理条数。

        没有清理掉任何东西时不写文件：构造期/测试环境下不该凭空创建 allowlist。
        """
        expired = [k for k in list(self._confirmed_permanent) if self._is_expired_permanent(k)]
        for key in expired:
            self._confirmed_permanent.discard(key)
            self._permanent_granted_at.pop(key, None)
        if expired:
            logger.info("allowlist 过期清理：%s 条（TTL %dh）",
                        len(expired), max(0, int(self.allowlist_ttl_hours)))
            try:
                self._save_permanent_allowlist()
            except Exception as e:
                logger.warning("allowlist 过期清理落盘失败（内存已生效）: %s", e)
        return len(expired)

    def permanent_entries(self) -> list[dict]:
        """已生效的永久授权条目（含主机、授予/到期时间），供管理接口展示。

        key 是裸授权键（不带 `@host|` 前缀），host 单独给 —— 前端要按主机分组、
        也要能显示「全部主机」，把主机段埋在 key 里让两边各自拆字符串迟早错位。
        expires_at 为 None = 永不过期（TTL=0，或该键没有可信的授予时间）。
        """
        ttl = self._ttl_seconds()
        out: list[dict] = []
        for entry in sorted(self._confirmed_permanent):
            host, key = split_scoped_key(entry)
            granted = self._permanent_granted_at.get(entry)
            out.append({
                "key": key,
                "host": host,
                "entry": entry,
                "granted_at": granted,
                "expires_at": granted + ttl if (ttl and granted is not None) else None,
            })
        return out

    def _save_permanent_allowlist(self) -> None:
        """保存永久 allowlist（键文件 + 同目录时间戳 sidecar）

        写前逐条校验、全部合法才打开文件：任何一条不合法都抛错且不落半截文件
        —— 这个 set 的成员通常来自 grant（已校验），但主机段是新增维度，
        多一道「写进去的必须读得回来」的守卫比依赖上游每个入口都自觉更可靠。
        """
        path = ALLOWLIST_PATH
        for entry in self._confirmed_permanent:
            valid, reason = _validate_entry(entry, 1)
            if not valid:
                raise RuntimeError(reason)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = int(time.time())
        for key in self._confirmed_permanent:
            self._permanent_granted_at.setdefault(key, now)
        # sidecar 只留仍生效的键，撤销/过期的条目不该继续累积
        self._permanent_granted_at = {
            k: v for k, v in self._permanent_granted_at.items() if k in self._confirmed_permanent
        }
        with open(path, 'w', encoding='utf-8') as f:
            for name in sorted(self._confirmed_permanent):
                f.write(name + '\n')
        try:
            with open(self._allowlist_meta_path(), 'w', encoding='utf-8') as f:
                json.dump(self._permanent_granted_at, f, sort_keys=True)
        except Exception as e:
            # 时间戳写失败不能阻塞授权本身：缺记录的键之后按 allowlist.txt 的 mtime
            # 兜底，方向是"少过期一点"，不是把已登记的预授权判死。
            logger.warning("写入 allowlist 时间戳失败: %s", e)

    @staticmethod
    def _grant_key(tool_name: str, command: Optional[str] = None) -> str:
        """计算授权键（审查 M-3 核心修复）。

        - 提供了命令 → `tool:<sha256(规范化命令)[:16]>`，授权只绑定被批准的那条
          命令，防止一次"始终允许 shell_exec"后任意 dangerous 命令免审批
          （如 `rm -rf ~/backups`）。
        - 未提供命令 → 工具名本身（保持原工具级授权语义）。

        命令有两个来源，二者共用同一键空间：
          1. 命令入口工具（_COMMAND_EXEC_TOOLS）的自由文本参数，由调用方取出传入；
          2. 实现 BaseTool.bound_command 的工具（file_write/file_edit/schedule_op）
             报出的"本次真实执行命令"。没有这一路，"始终允许 file_write"写进的是
             裸工具名，等于允许写任意路径（含 /root/.ssh/authorized_keys）。

        兼容性：旧 allowlist.txt 中裸 `shell_exec` 条目对新逻辑不再放行命令级审批
        （fail-safe 方向收紧），用户需重新针对具体命令授权。
        """
        if command and str(command).strip():
            from ..safety import normalize_command_for_detection
            digest = hashlib.sha256(
                normalize_command_for_detection(str(command)).encode("utf-8")
            ).hexdigest()[:16]
            return f"{tool_name}:{digest}"
        return tool_name

    def check_permission(self, tool_name: str, args: dict,
                         risk: Optional[str] = None,
                         conversation_id: str = "",
                         run_id: str = "",
                         bound_command: Optional[str] = None,
                         host: str = "") -> bool:
        """检查权限，返回是否放行

        risk: 调用方已判定的风险等级；risk == "dangerous" 时忽略 session 级缓存
              （B-6：落实 "dangerous 需每次授权" 契约，只认 once 与 always/permanent 键）

        bound_command: 调用方（FilesystemGuard）已算出的"本次真实执行命令"。非命令
              入口工具（file_write 等）靠它把授权键从裸工具名收紧到目标粒度 —
              否则一次"始终允许 file_write"等于允许写任意路径。

        host: 目标主机（服务端从连接配置反查）。永久授权命中时先查
              `@host|key` 再查全局 `key` —— 在测试机上登记过的命令不会自动
              在生产机上免审，除非那条登记本身就是全局的（旧条目语义）。

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
        command = self.command_for_call(tool_name, args, bound_command)
        key = self._grant_key(tool_name, command)
        # 问题 4：visible_terminal 拒绝标记检查 — 用户明确拒绝的命令不再免审
        # （即使永久 allowlist 中有该命令，当前 run 的拒绝状态也优先）
        if self.is_denied(tool_name, command, run_id):
            return False
        # 检查 permanent allowlist（E3：到期即失效，不再永久免审）
        if self._permanent_active(key, host):
            return True
        # 检查 session allowlist
        # B-6 修复：dangerous 需每次授权 — 忽略 session 级缓存
        # （原实现固定按 permanent→session→once 检查，首次 session 授权后
        #   同命令后续执行全部免审批，与 check() 中 L748 注释契约矛盾）
        # C-review M11：只查本会话（conversation_id）的 session 授权，
        # 跨会话不再免审（原全局 set 会把会话 A 的授权泄漏到会话 B）
        # 主机维度与永久池同构：`@host|key` 只对该主机命中，裸键（无主机授权）
        # 对所有主机命中 —— 否则在测试机上点「本会话允许」，同一个对话切到
        # 生产机后同一条命令直接免审（会话是跨主机的，主机不是）。
        if risk != "dangerous":
            session_keys = self._confirmed_session.get(conversation_id, frozenset())
            h = normalize_host(host)
            if key in session_keys or (h and scoped_key(h, key) in session_keys):
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

    def is_permanently_allowed(self, tool_name: str, args: dict,
                               bound_command: Optional[str] = None,
                               host: str = "") -> bool:
        """是否已被永久授权（生产环境危险命令的预授权 allowlist 判断）。

        仅认 `_confirmed_permanent`（持久化 allowlist），忽略 once/session 一次性授权，
        保证 prod 环境危险命令必须"预先登记"而非临时批准（合规要求）。
        有命令可绑定时按"工具+命令哈希"键匹配（审查 M-3），否则按工具名匹配。
        E3：过了 TTL 的预登记条目同样不算预授权 —— 到期后 prod 危险命令会重新
        走"未预授权"拒绝路径，而不是悄悄按放行处理。
        host：主机限域条目只对该主机算预授权；`@other|key` 不会放行本机命令。

        bound_command 由 FilesystemGuard 传入（它已算过一遍，不在此重算）：
        prod 下 file_write 写敏感路径预登记的键是 `file_write:<该路径模板哈希>`，
        漏传会退化成查裸工具名 → 批准过 /etc/nginx 就等于批准写任意路径。
        """
        command = self.command_for_call(tool_name, args, bound_command)
        key = self._grant_key(tool_name, command)
        return self._permanent_active(key, host)

    def grant_key_for(self, tool_name: str, command: Optional[str] = None,
                      scope: str = 'once', run_id: str = "") -> str:
        """只算授权键 + 校验入参，**不改任何授权状态**。

        为什么需要它：授权端点的写前审计（audit.append_record(critical=True)）
        要先把 target_id 写进审计链、再让授权生效 —— 顺序反过来就会留下
        「授权已生效但没记录」的缺口，而那正是审计链最怕的形态。
        校验与 grant 共用同一套规则，否则会出现「审计已经写了、grant 才抛
        ValueError」的半条记录。
        """
        self._validate_grant_args(tool_name, scope=scope, run_id=run_id)
        return self._grant_key(tool_name, command)

    @staticmethod
    def _validate_grant_args(tool_name: str, *, scope: str, run_id: str) -> None:
        if not _ALLOWLIST_TOOL_NAME_RE.match(tool_name or ""):
            raise ValueError(
                f"工具名 {tool_name!r} 不在授权清单允许的格式内"
                "（期望 ^[A-Za-z0-9_.-]+$，不接受空白、换行或斜杠）")
        if scope == 'once' and not run_id:
            # once 授权必须绑定 run_id：无归属的授权无法被 check 消费、
            # 也无法被 run 终态清理，直接拒绝而非落入无人认领的池
            raise ValueError("scope='once' 授权必须提供 run_id")

    def grant(self, tool_name: str, scope: str = 'once', command: Optional[str] = None,
              conversation_id: str = "", run_id: str = "", host: str = "") -> str:
        """授予权限，返回实际写入的授权键（裸键，不含主机前缀）

        scope: 'once' | 'session' | 'always'
        command: 命令执行工具时传入被批准的具体命令（审查 M-3 命令级绑定）
        conversation_id: C-review M11 — session 授权绑定到发起授权的会话
        run_id: 问题 1 改进 — once 授权绑定到发起授权的 run_id（消除跨 run 竞态）
        host: scope='always' 时限定到该主机（`@host|key`）；空串 = 全局条目。
              服务端能反查到目标主机时一律传，否则用户在测试机上点的
              「始终允许」会变成生产机上的免审通行证。

        tool_name 必须在 allowlist 的条目格式内：scope=always 的授权要落
        `allowlist.txt` 一行一条，一个含空白/换行/非 ASCII 的工具名照样写得进去，
        于是两种坏结果同时发生 —— 带尾空格的名字永远匹配不上 check_permission
        （面板显示「已授权」，事故时发现预授权是空的），而带 `\\n` 的名字会被写成
        **两行**，等于凭空多出一条没人批准过的预授权记录。读侧的
        `_validate_allowlist_entry` 一直会丢掉这类行，所以这不是新增限制，
        只是把「写进去的东西必须读得回来」这条隐含前提摆到写侧。
        """
        if not _ALLOWLIST_TOOL_NAME_RE.match(tool_name or ""):
            raise ValueError(
                f"工具名 {tool_name!r} 不在授权清单允许的格式内"
                "（期望 ^[A-Za-z0-9_.-]+$，不接受空白、换行或斜杠）")
        if host and not _ALLOWLIST_HOST_RE.match(normalize_host(host)):
            raise ValueError(
                f"主机标识 {host!r} 不在授权清单允许的格式内"
                "（期望 [A-Za-z0-9_.:-]，不接受空白、换行或斜杠）")
        key = self._grant_key(tool_name, command)
        if scope == 'once':
            # once 授权必须绑定 run_id：无归属的授权无法被 check 消费、
            # 也无法被 run 终态清理，直接拒绝而非落入无人认领的池
            if not run_id:
                raise ValueError("scope='once' 授权必须提供 run_id")
            # 用户在「拒绝」后又点了「允许本次」→ 显式授权覆盖该 key 的拒绝标记。
            # 否则对无命令可绑定的工具（多数 ops_* 走裸工具名键），一次拒绝即整 run
            # 禁用：check_permission 先查 is_denied 直接返回 False，之后的每次授权
            # 只写进无人消费的池子，卡片反复弹出直到审批超时，而审计却记为 approved。
            # 仅 once 生效：session/always 授权仍让位于本 run 的拒绝（原契约不变）。
            self._denied_commands.get(run_id, set()).discard(key)
            self._confirmed_once[run_id][key] += 1
        elif scope == 'session':
            # 与永久池同构：能反查到目标主机时写 `@host|键`（host 为空 = 裸键，
            # 对所有主机的旧语义），留着裸键是因为本地无 SSH 的 run 没有主机维度。
            self._confirmed_session.setdefault(conversation_id, set()).add(
                scoped_key(host, key))
        elif scope == 'always':
            # 主机限域：host 为空时 scoped_key 原样返回裸键（全局，旧行为）
            self._confirmed_permanent.add(scoped_key(host, key))
            self._save_permanent_allowlist()
        return key

    def _revoke_key(self, key: str, conversation_id: Optional[str] = None,
                    host: Optional[str] = None) -> None:
        """撤掉一个裸授权键（host 同时决定永久池与 session 池的作用域）。

        host=None（旧调用）：全局条目 + 所有 `@*|key` 一并撤 —— 兼容原来的
        「删一行就断」语义，也符合撤销总是收紧的方向；host="" 只撤全局；
        host="h" 只撤该主机。session 池与永久池同一口径：`@h|key` 只对那台
        机器免审，点一下别的机器的撤销不该挟带它。
        """
        def _matches(entry: str) -> bool:
            if split_scoped_key(entry)[1] != key:
                return False
            if host is None:
                return True
            if host == "":
                return not entry.startswith(_SCOPED_PREFIX)
            return entry == scoped_key(host, key)

        # 问题 1 改进：遍历所有 run_id 的 once 池，移除该 key
        for run_pool in list(self._confirmed_once.values()):
            run_pool.pop(key, None)
        # C-review M11：conversation_id 指定 → 只撤该会话；None → 撤所有会话
        # 先按 host 过滤再删除：scoped 条目的前缀与裸键不同，不能直接 discard(key)
        if conversation_id is not None:
            s = self._confirmed_session.get(conversation_id)
            if s is not None:
                s.difference_update([e for e in s if _matches(e)])
                if not s:
                    self._confirmed_session.pop(conversation_id, None)
        else:
            for s in self._confirmed_session.values():
                s.difference_update([e for e in s if _matches(e)])
            for cid in [c for c, s in self._confirmed_session.items() if not s]:
                self._confirmed_session.pop(cid, None)
        for entry in [e for e in self._confirmed_permanent if _matches(e)]:
            self._confirmed_permanent.discard(entry)
            self._permanent_granted_at.pop(entry, None)

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
        """检查某 run 下某命令是否已被用户拒绝（严格 run_id 作用域，不查全局池）。

        拒绝按「或」匹配，与授权的方向刻意相反：授权要逐条绑定（见 _grant_key），
        否决则要整体生效 —— 工具级拒绝记下的是裸工具名键，而 bound_command 让
        service_manage 这类工具改用命令级键；只比命令键的话，用户点「拒绝」留下的
        工具级记录就不再命中，S2-01 的「用户否决高于模型翻案」被推翻。
        """
        denied = self._denied_commands.get(run_id)
        if not denied:
            return False
        return (
            self._grant_key(tool_name, None) in denied
            or self._grant_key(tool_name, command) in denied
        )

    def command_for_call(self, tool_name: str, args: dict,
                         bound_command: Optional[str] = None) -> Optional[str]:
        """本次调用的授权键命令位 —— 与 check_permission 的推导同源（单一数据源）。

        bound_command 由调用方（FilesystemGuard / PermissionGate.check）按工具实际
        执行的那条命令传入；命令执行工具未传时回退 command_from_args。两处曾各抄
        一遍这段推导，抄漏的一方裁决标的与授权键绑到不同东西上。
        """
        if bound_command is not None:
            return bound_command
        if tool_name in _COMMAND_EXEC_TOOLS:
            return command_from_args(args)
        return None

    def denied_for_call(self, tool_name: str, args: dict, run_id: str,
                        bound_command: Optional[str] = None) -> bool:
        """本次调用是否被用户显式拒绝过。

        LLM 裁决器（auto_approve.try_auto_approve）调用前必须先过这道门：
        auto_approve.py 头部把「用户明确拒绝过的命令」列为刻意不经过裁决器的路径，
        并写明调用方负责保持这个顺序 —— 用户否决永远高于模型翻案。
        """
        return self.is_denied(
            tool_name, self.command_for_call(tool_name, args, bound_command), run_id)

    def approval_key_for_call(self, tool_name: str, args: dict,
                              bound_command: Optional[str] = None) -> str:
        """审批等待闸的钥匙：本次调用实际绑定的授权键（裸键，与 grant 端点同源）。

        Driver 拿它登记等待项，grant / deny 端点用 grant_key_for 算出同一把键
        唤醒。两边必须是同一份推导：键一旦漂移，用户点了允许、卡片也消失，run 却
        一直挂着等下去 —— 这种「授权了但没生效」比直接报错难查得多。
        """
        return self._grant_key(
            tool_name, self.command_for_call(tool_name, args, bound_command))

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
               conversation_id: Optional[str] = None,
               host: Optional[str] = None) -> None:
        """撤销权限

        - 提供 command：只撤销该条命令的绑定授权
        - 不提供 command：撤销该工具的全部授权（工具级 + 所有命令级绑定键）
        - conversation_id：C-review M11 — 只撤指定会话的 session 授权；
          None（默认）保持旧语义：撤所有会话
        - host：None（默认）= 全局与所有主机限域条目全撤；"" = 只撤全局；
          具体主机 = 只撤该主机的条目

        command 不限 _COMMAND_EXEC_TOOLS：bound_command 类工具（file_write 等）的
        授权键同样按命令哈希绑定，撤销口径必须与 grant 对称，否则撤销会要么过宽
        （抹掉该工具所有路径）要么失效（撤不掉那一条键）。
        """
        if command:
            self._revoke_key(self._grant_key(tool_name, command), conversation_id, host)
        else:
            self._revoke_key(tool_name, conversation_id, host)
            prefix = tool_name + ":"
            # 主机限域条目在集合里是 `@host|tool:hash`，直接 startswith(prefix)
            # 永远匹配不到 —— 必须按拆出的裸键比。漏了这一层的后果是「点了全部
            # 撤销，文件里的 @host|tool:hash 原封不动」。
            for k in [k for k in self._confirmed_permanent
                      if split_scoped_key(k)[1].startswith(prefix)]:
                # 传裸键：_revoke_key 的 host 参数负责决定撤哪个作用域，
                # 直接把 `@host|...` 当键传进去会撤一条不存在的条目。
                self._revoke_key(split_scoped_key(k)[1], conversation_id, host)
            cids = [conversation_id] if conversation_id is not None \
                else list(self._confirmed_session.keys())
            for cid in cids:
                s = self._confirmed_session.get(cid)
                if not s:
                    continue
                # 与永久池同一坑：scoped 条目是 `@host|tool:hash`，
                # startswith(prefix) 永远匹配不上 —— 必须按拆出的裸键比。
                for k in [k for k in s if split_scoped_key(k)[1].startswith(prefix)]:
                    self._revoke_key(split_scoped_key(k)[1], cid, host)
            # once 池按 run_id 分桶（_confirmed_once[run_id][grant_key]），顶层键是
            # run_id 而非授权键，直接 for k in self._confirmed_once 永远匹配不到 prefix。
            # 需下钻各 run 池收集匹配该工具的授权键，再逐个撤销（与 _revoke_key 同构）。
            once_keys = {
                k for pool in self._confirmed_once.values() for k in pool
                if k.startswith(prefix)
            }
            for k in once_keys:
                self._revoke_key(k, conversation_id, host)
        self._save_permanent_allowlist()

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

        本函数只看 args["action"] 这一类"工具参数即语义"的升级，不做命令内容判定 —
        内容级升级（写敏感路径 / 定时任务里塞 rm）由 bound_command + FilesystemGuard
        承担（见 check 的 A5 分支），两条路各自单一职责，避免第二份风险口径表。
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

    def _tool_description(self, tool_name: str, ctx: Optional[RunContext] = None) -> str:
        """工具级审批卡的中文说明 —— 直接取 BaseTool.description（模型看到的同一份文案）。

        与 _lookup_meta 分开查，是因为这里要的是工具对象、不是 ToolMeta。
        注册表取不到时回空串：卡片宁可少一行说明，也不能为展示层把审批链炸掉。
        """
        registry = resolve_registry(ctx, self.saddle)
        if not registry:
            return ""
        try:
            tool = registry.get(tool_name)
            return str(getattr(tool, "description", "") or "") if tool else ""
        except Exception:
            return ""

    async def check(self, ctx: RunContext) -> ReinDecision:
        last = ctx.active_tool_call
        if last is None:
            return ReinDecision()
        tool = last.get("tool", "")
        args = last.get("args", {})

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

        # A5：自身拼命令的工具（file_write/file_edit/schedule_op）与 shell_exec 对齐 —
        # 内容比 ToolMeta 更危险时让位给 FilesystemGuard（priority 8 在本拦截器、
        # 10 在守卫，守卫后跑）。守卫已实现 dangerous 审批三态/超时/拒绝记忆/prod
        # 预授权闸门，此处不复制那五套分支，否则同一判定有两处实现必然漂移。
        # 内容 safe（如写 /tmp）仍走下面的 ToolMeta 流程，但授权键绑定该命令。
        bound, is_free_form = extract_bound_command(
            resolve_registry(ctx, self.saddle), tool, args
        )
        if bound:
            content_level, _reason = (classify_command if is_free_form
                                      else classify_constructed_command)(bound)
            if content_level != "safe":
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
            if environment == "prod" and not self.is_permanently_allowed(
                    tool, args, bound_command=bound, host=_target_host(ctx)):
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
                    intercept_reason=(
                        f"生产环境危险工具{POLICY_DENY_MARKER}: {tool}"),
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
                            "换用其他工具或参数变体同样会被拒绝，不要再尝试替代写法。"
                            "请停止尝试，直接告知用户：需在权限 allowlist 中预先登记后重试。"
                        ),
                    }],
                )
            # C-review NIT：原 scope='once' 为死参数 — dangerous 每次授权的
            # 语义由 risk 分支保证（忽略 session 缓存，只认 once/permanent 键）
            if self.check_permission(tool, args, risk='dangerous',
                                      conversation_id=ctx.conversation_id,
                                      run_id=ctx.run_id,
                                      bound_command=bound,
                                      host=_target_host(ctx)):
                return ReinDecision()
        else:
            # moderate 不可逆 → session 级授权可接受（风险有限）
            if self.check_permission(tool, args,
                                     conversation_id=ctx.conversation_id,
                                     run_id=ctx.run_id,
                                     bound_command=bound,
                                     host=_target_host(ctx)):
                return ReinDecision()

        # 自动审批（permission_mode="auto"）：工具级 dangerous / moderate 不可逆
        # 未授权时，弹卡前先让模型裁决一次（fail-closed，见 auto_approve 模块）。
        # prod 强化分支已在上方 return —— 合规授权必须出自用户预登记，不问模型。
        # bound_command 与上方 check_permission 同源：裁决标的必须等于授权键绑定的
        # 那条命令，否则模型批的是文件正文、键却绑在写模板路径上（两处不同物）。
        # S2-01 修复：check_permission 返回 False 无法区分「还没批过」与「用户拒绝
        # 过」，原实现把两者一视同仁送进裁决器 ⇒ 自动审批可推翻用户的显式拒绝
        # （FilesystemGuard 有这道门、这里漏了 —— 同一族防护写一处漏另一处）。
        if not self.denied_for_call(tool, args, ctx.run_id, bound):
            decision = await try_auto_approve(
                ctx, tool, args, f"工具级风险 {risk} 需授权",
                bound_command=str(bound or ""))
            if decision is not None:
                return decision

        # 未授权 → 发 permission_needed 事件 + 拦截
        # C-review M7：与 FilesystemGuard 相同的审批三态处理（首次发卡片 /
        # 窗口内重发豁免循环检测 / 兜底到期发 permission_expired 但保留等待集）
        pending = _perm_pending(ctx)
        sig = tool_call_signature(last)
        first_ts = pending.get(sig)
        # 审查R：兜底窗口 >0 才启用到期判定（core.py 注释声明"设为 0 表示禁用兜底/永久等待"，
        # 但旧判据 elapsed > 0.0 对任何已记录时间戳恒真 → 设 0 反而首次重发即过期，语义反转）。
        # 时长现查 getter（可被设置面板 PUT 即时改写），不吃导入期快照。
        fallback_s = approval_fallback_seconds()
        if first_ts is not None and fallback_s > 0 and \
                (time.monotonic() - first_ts) > fallback_s:
            # 兜底到期 → 保留等待集（不 pop），发 permission_expired 把卡标记
            # timeout 保留；注入提示引导用户确认后重新发起。等待集未移除，兜底
            # 窗口内同命令重发仍被拦截、复用待办而不重复建卡。
            # 审计：与 FilesystemGuard 的同名分支同一口径 —— 这条判定必须进链。
            # 原先只有守卫那半边记 denied，工具级授权的过期尝试只发事件不落链，
            # 而「AI 试了什么没成功同样可追」（security-boundary §6）正靠这些记录兑现。
            await append_record_async(
                action=tool, conclusion="denied",
                run_id=ctx.run_id, iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=environment,
                command_raw=str(bound or ""),
                result=f"approval_expired: {tool}",
            )
            return ReinDecision(
                intercepted=True,
                intercept_reason=f"审批已过期（保留待办，等待确认）: {tool}",
                events=[TraceEvent(
                    kind="permission_expired",
                    source="reins.permission_gate",
                    run_id=ctx.run_id,
                    iteration=ctx.iteration,
                    payload={
                        "tool": tool,
                        "args": args,
                        # 与 permission_needed 同源：前端按「工具 + 命令」定位卡片
                        "command": str(bound or ""),
                        "message": (
                            f"授权审批已过期（{int(fallback_s)}s 兜底未响应），"
                            f"待办已保留，可重新确认后执行: {tool}"
                        ),
                    },
                )],
                inject_messages=[{
                    "role": "user",
                    "content": (
                        f"[系统拦截] 工具 {tool} 的授权审批已过期"
                        f"（{int(fallback_s)}s 兜底无用户响应），"
                        "系统已保留待办、未按拒绝处理。请提示用户确认后重新发起，"
                        "或换用更安全的工具。"
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
                # command 传 bound（内容 safe 的写入/定时任务工具，如写 /tmp）或空串
                # （纯工具级授权，如 service_manage 无命令参数 → 前端 isCommandTool
                # 判断下「授权命令」行自然隐藏）。展示的是授权键实际绑定的那条命令，
                # 用户批准什么就看到什么。
                # explain 取工具自己声明的 description：工具级卡片此前只有英文
                # message，同一张卡族里它是唯一没有中文后果说明的那一个。
                "explain": self._tool_description(tool, ctx),
                "explain_source": "tool",
                **permission_display_fields(ctx, str(bound or "")),
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
            # 卡片已弹出 → 本次调用要等人。Driver 拿这把键登记等待项，
            # grant / deny 端点算出同一把键（都是 _grant_key）把它唤醒。
            approval_key=self.approval_key_for_call(tool, args, bound),
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
