"""
Safety — 命令安全检测层

借鉴 Hermes approval.py 思路，提供更鲁棒的命令风险检测：
- 命令归一化（反 ANSI/Unicode/转义/空字符串混淆）
- HARDLINE_PATTERNS：无条件阻断（12 条）
- DANGEROUS_PATTERNS：需审批（约 20 条）
- sudo -S stdin 密码猜测守卫

相比 reins.FilesystemGuard 仅用 7 个字符串子串匹配，本模块通过正则 +
归一化显著提升对 `r\\m -rf /`、`rm\\t-rf /`、Unicode 全角字符、ANSI 注入
等混淆攻击的防御能力。
"""

from __future__ import annotations

import os
import re
import unicodedata

__all__ = [
    "normalize_command_for_detection",
    "HARDLINE_PATTERNS",
    "DANGEROUS_PATTERNS",
    "check_sudo_stdin_guard",
    "classify_command",
    "classify_shell_args",
]


# ──────────────────────────────────────────────────────────────────────────────
# 命令归一化
# ──────────────────────────────────────────────────────────────────────────────

# ANSI 转义序列：ESC [ 参数... 字母
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# null 字节
_NULL_BYTE_RE = re.compile(r"\x00")
# 反斜杠转义：把 `r\m` 还原为 `rm`
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([^\n])")
# 空字符串：把 `r''m` 还原为 `rm`
_EMPTY_STRING_RE = re.compile(r"''|\"\"")


def normalize_command_for_detection(command: str) -> str:
    """命令归一化 — 抵御常见混淆攻击

    处理顺序：
    1. strip ANSI 转义序列
    2. 剥 null 字节
    3. Unicode NFKC 归一化（全角 → 半角）
    4. 折叠 home 路径（绝对路径折叠回 ~）
    5. 剥反斜杠转义
    6. 剥空字符串
    """
    # 1. strip ANSI 转义序列
    command = _ANSI_ESCAPE_RE.sub("", command)

    # 2. 剥 null 字节
    command = _NULL_BYTE_RE.sub("", command)

    # 3. Unicode NFKC 归一化（全角 → 半角）
    command = unicodedata.normalize("NFKC", command)

    # 4. 折叠 home 路径（绝对路径折叠回 ~）
    home = os.path.expanduser("~")
    if home and command.startswith(home):
        command = "~" + command[len(home):]

    # 5. 剥反斜杠转义：把 `r\m` 还原为 `rm`
    #    连续多个反斜杠（如 `r\\m`）需循环归约直至稳定
    while True:
        new_command = _BACKSLASH_ESCAPE_RE.sub(r"\1", command)
        if new_command == command:
            break
        command = new_command

    # 6. 剥空字符串：把 `r''m` 还原为 `rm`
    command = _EMPTY_STRING_RE.sub("", command)

    return command


# ──────────────────────────────────────────────────────────────────────────────
# HARDLINE_PATTERNS — 无条件阻断（12 条）
# ──────────────────────────────────────────────────────────────────────────────

HARDLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)"), "recursive delete of root filesystem"),
    (re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "format filesystem (mkfs)"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*"), "dd to raw block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "system shutdown"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "system reboot"),
    (re.compile(r"\bhalt\b", re.IGNORECASE), "system halt"),
    (re.compile(r"\bpoweroff\b", re.IGNORECASE), "system poweroff"),
    (re.compile(r"\binit\s+0\b"), "init 0 (halt)"),
    (re.compile(r"\bkillall5\b"), "kill all processes"),
    (re.compile(r"\bkill\s+-9\s+-1\b"), "kill all processes by PID -1"),
    (re.compile(r"\bgit\s+push\s+(-f|--force)\s+origin\s+(main|master)\b", re.IGNORECASE), "force push to main/master"),
]


# ──────────────────────────────────────────────────────────────────────────────
# DANGEROUS_PATTERNS — 需审批（约 20 条）
# ──────────────────────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[^\s]*\s+)*-r[^\s]*\s+/", re.IGNORECASE), "recursive delete from root"),
    (re.compile(r"\brm\s+(-[^\s]*\s+)*-r[^\s]*\s+\*", re.IGNORECASE), "recursive delete with wildcard"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\b", re.IGNORECASE), "chmod 777 (world writable)"),
    (re.compile(r"\bchown\s+-R\b", re.IGNORECASE), "recursive chown"),
    (re.compile(r"\bsudo\b", re.IGNORECASE), "sudo command"),
    (re.compile(r"\bsu\s", re.IGNORECASE), "switch user"),
    (re.compile(r"\bmv\s+.+\s+/dev/null\b"), "move to /dev/null (data loss)"),
    (re.compile(r"\b>\s*/dev/(sd|nvme|hd|mmcblk)"), "redirect to raw block device"),
    (re.compile(r"\bcurl\s+.+\s*\|\s*sh\b"), "curl pipe shell (remote execution)"),
    (re.compile(r"\bwget\s+.+\s*\|\s*sh\b"), "wget pipe shell (remote execution)"),
    (re.compile(r"\bgit\s+push\s+(-f|--force)\b", re.IGNORECASE), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git reset hard"),
    (re.compile(r"\bgit\s+clean\s+-fdx\b", re.IGNORECASE), "git clean force"),
    (re.compile(r"\bdocker\s+(rm|prune|rmi)\b", re.IGNORECASE), "docker destructive"),
    (re.compile(r"\bnpm\s+uninstall\b", re.IGNORECASE), "npm uninstall"),
    (re.compile(r"\bpip\s+uninstall\b", re.IGNORECASE), "pip uninstall"),
    (re.compile(r"\bcrontab\s+-r\b", re.IGNORECASE), "remove crontab"),
    (re.compile(r"\biptables\s+-F\b", re.IGNORECASE), "flush iptables"),
    (re.compile(r"\bufw\s+disable\b", re.IGNORECASE), "disable firewall"),
    (re.compile(r":\(\)\s*\{"), "fork bomb pattern (partial)"),
]


# ──────────────────────────────────────────────────────────────────────────────
# sudo stdin 守卫
# ──────────────────────────────────────────────────────────────────────────────

# 匹配命令分隔符后紧跟的 `sudo -S`（stdin 密码读取）
_SUDO_STDIN_RE = re.compile(r"(?:^|[;&|`\n]|&&|\|\|)\s*sudo\s+-S\b", re.IGNORECASE)


def check_sudo_stdin_guard(command: str) -> tuple[bool, str | None]:
    """检测 sudo -S（stdin 密码）但 SUDO_PASSWORD 未配置 → 阻断

    Returns:
        (blocked, reason) - blocked=True 表示应阻断
    """
    if "SUDO_PASSWORD" in os.environ:
        return (False, None)
    normalized = normalize_command_for_detection(command).lower()
    if _SUDO_STDIN_RE.search(normalized):
        return (True, "sudo password guessing via stdin (sudo -S)")
    return (False, None)


# ──────────────────────────────────────────────────────────────────────────────
# 主检测函数
# ──────────────────────────────────────────────────────────────────────────────

def classify_command(command: str) -> tuple[str, str | None]:
    """分类命令风险等级

    Returns:
        (level, reason)
        level: "hardline" (无条件阻断) | "dangerous" (需审批) | "safe" (放行)
        reason: 阻断/需审批的原因（level=safe 时为 None）
    """
    normalized = normalize_command_for_detection(command)

    # 1. sudo stdin guard（先于其他检查）
    blocked, reason = check_sudo_stdin_guard(command)
    if blocked:
        return ("hardline", reason)

    # 2. HARDLINE 检查
    for pattern, reason in HARDLINE_PATTERNS:
        if pattern.search(normalized):
            return ("hardline", reason)

    # 3. DANGEROUS 检查
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(normalized):
            return ("dangerous", reason)

    return ("safe", None)


# ──────────────────────────────────────────────────────────────────────────────
# 批量检测：从工具参数中提取 shell 命令
# ──────────────────────────────────────────────────────────────────────────────

def classify_shell_args(args: dict) -> tuple[str, str | None]:
    """从工具参数中提取 shell 命令并分类

    支持 args.command / args.shell / args.cmd 字段
    """
    cmd = args.get("command") or args.get("shell") or args.get("cmd") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return ("safe", None)
    return classify_command(cmd)
