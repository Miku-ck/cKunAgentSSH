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
import posixpath
import re
import shlex
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
    # 尾部边界含闭括号：`os.system('rm -rf /')` 等解释器包裹的字面根删除同样 hardline
    (re.compile(r"\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(?=[\s)\]}]|$)"), "recursive delete of root filesystem"),
    (re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "format filesystem (mkfs)"),
    # B-4 修复：of= 目标为任意 /dev/ 路径，仅排除安全设备前缀
    # （zero/null/random/urandom/tty/pts/shm/console/fd），写裸设备一律 hardline
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(?!zero\b|urandom\b|random\b|null\b|tty|pts|shm|console|fd)\S+"), "dd to raw block device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "system shutdown"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "system reboot"),
    (re.compile(r"\bhalt\b", re.IGNORECASE), "system halt"),
    (re.compile(r"\bpoweroff\b", re.IGNORECASE), "system poweroff"),
    (re.compile(r"\binit\s+0\b"), "init 0 (halt)"),
    # B-4 修复：systemctl 电源动作 + telinit 0/6（与 shutdown 同级，无条件阻断）
    (re.compile(r"\bsystemctl\s+(poweroff|reboot|halt|kexec|suspend)\b", re.IGNORECASE), "systemctl power action"),
    (re.compile(r"\btelinit\s+(0|6)\b"), "telinit 0/6 (halt/reboot)"),
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
    # 编码管道执行：base64 -d | sh（必须全串检测——管道符是分段符，per-segment 永远匹配不到）
    (re.compile(r"\bbase64\b[^\n|]*\|\s*(?:sh|bash|zsh|dash|ksh|python[0-9.]*|perl|ruby|node)\b"),
     "base64 decode piped to interpreter (encoded remote execution)"),
    # 反弹 shell：nc/ncat/netcat -e
    (re.compile(r"\b(?:nc|ncat|netcat)\b[^\n]*\s-e\s+"), "netcat -e (reverse shell)"),
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
# 加固层（在 normalize + 正则表之上）
# ──────────────────────────────────────────────────────────────────────────────

# 严重度排序
_SEVERITY = {"safe": 0, "dangerous": 1, "hardline": 2}

# 检测副本：额外剥掉所有单双引号字符（仅用于模式匹配）
_QUOTE_STRIP_RE = re.compile(r"['\"]")

# 命令链切分：;  &&  ||  |  换行（在剥引号后的副本上切分，引号无法隐藏分隔符）
_COMMAND_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")

# 间接展开标记：$( ... ) / 反引号 / ${ ... }
_INDIRECT_EXPANSION_RE = re.compile(r"\$\(|`|\$\{")

# 破坏性动词（间接展开规则 + token 分析共用），含 `>` 重定向到设备
_DESTRUCTIVE_VERB_RE = re.compile(
    r"\b(?:rm|rmdir|find|shred|truncate|dd|mkfs[a-z0-9.]*)\b|\>\s*/dev/"
)

# 敏感系统目录（前缀匹配：/etc/xxx 也算）
_SENSITIVE_DIRS = (
    "/etc", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/dev", "/home", "/root", "/opt", "/srv", "/proc", "/sys",
    "/System", "/Applications",
)

# 持久化/植入类写入目标（重定向 > / >> 或 tee 命中即 hardline）：
# 篡改认证/权限/启动加载机制，属于植入后门行为
_PERSISTENCE_PREFIXES = (
    "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/etc/gshadow",
    "/etc/ld.so.preload",
)

# 敏感读取目标（cat/less/head/tail/cp/dd 等命中即 dangerous）：
# 认证凭据与权限文件，正常运维不应由 Agent 读取
_SECRET_FILE_RE = re.compile(
    r"(/etc/(?:passwd|shadow|sudoers|gshadow|master\.passwd)|"
    r"(?:/root|~|\$HOME)/?\.ssh/"
    r"|/\.ssh/(?:id_|authorized_keys)"
    r"|/id_(?:rsa|dsa|ecdsa|ed25519)\b)"
)

# 解释器/脚本执行器（作为命令首 token 出现 → 至少 dangerous，需审批）
# 字符串参数内容对 token 分析不可见，解释器调用是间接执行的主要载体
_INTERPRETERS = {
    "python", "python2", "python3", "perl", "perl5", "ruby", "irb", "php",
    "node", "nodejs", "deno", "bun", "lua", "busybox", "sh", "bash", "zsh",
    "dash", "ksh", "tcsh", "csh", "eval", "source", "powershell", "pwsh",
    "cmd", "rubyw", "perl5.30",
    # B-1a 修复：包管理器/脚本执行器同样可间接执行任意代码，首 token 出现即需审批
    "npx", "npm", "pip", "pip3", "yarn", "pnpm", "bunx", "uv",
}
# 带版本号的 python3.x 形式在首 token 判断时按前缀匹配
_INTERPRETER_PREFIXES = ("python3.", "python2.", "perl5.")

# 可剥离的命令包装器/环境赋值前缀（检测真实首 token 用）
_WRAPPERS = {"env", "nohup", "time", "sudo", "doas", "nice", "stdbuf", "xargs", "command"}

# 重定向扫描：> 或 >> 后跟路径（排除 << / <<< / <> 噪声；2>&1 的目标 "1" 无害）
# 检测副本已剥引号，无需引号组
_REDIRECT_RE = re.compile(r"(?<!<)>{1,2}\s*([^\s;|&<>]+)")


def _detection_copy(command: str) -> str:
    """生成「检测副本」：归一化后额外剥掉所有单双引号字符（仅用于模式匹配）

    `normalize_command_for_detection` 的返回值语义保持不变；
    本副本只在 classify 内部使用，用于识破 `rm -rf "/"` 等引号包裹绕过。
    """
    return _QUOTE_STRIP_RE.sub("", normalize_command_for_detection(command))


def _split_command_chain(text: str) -> list[str]:
    """按 ; / && / || / | / 换行 切分命令链，返回非空段列表"""
    return [seg.strip() for seg in _COMMAND_CHAIN_SPLIT_RE.split(text) if seg.strip()]


def _normalize_target(path: str) -> str:
    """规范化目标路径：strip + 折叠连续的 / + 解析 ./.. 点分段

    B-7 配套修复：原实现只折叠连续 /，`rm -rf /../` 归一后仍是 "/../"，
    绕过根目录/敏感目录前缀匹配。改用 posixpath.normpath 解析点分段
    （normpath("/../") → "/"），保持原函数契约（strip + 折叠连续 /，
    空串返回 ""，~/$HOME 等非常规前缀不受影响）。
    """
    p = path.strip()
    if not p:
        return ""
    norm = posixpath.normpath(p)
    # posixpath.normpath 对恰好两个前导 / 有 POSIX 特殊语义（保留 "//x"），
    # 原契约是折叠为单个 /，此处对齐
    if norm.startswith("//"):
        norm = norm[1:]
    return norm


def _is_sensitive_path(target: str) -> bool:
    """目标以 / 开头且命中敏感目录集合（前缀匹配）"""
    t = _normalize_target(target)
    if not t.startswith("/"):
        return False
    return any(t == d or t.startswith(d + "/") for d in _SENSITIVE_DIRS)


def _is_home_root(target: str) -> bool:
    """目标是否就是用户主目录本身（~ / $HOME / ${HOME}）"""
    t = _normalize_target(target).rstrip("/")
    return t in ("~", "$HOME", "${HOME}")


def _is_home_relative(target: str) -> bool:
    """目标是否位于用户主目录下（~/... / $HOME/...）"""
    t = _normalize_target(target)
    return (
        _is_home_root(t)
        or t.startswith("~/")
        or t.startswith("$HOME/")
        or t.startswith("${HOME}/")
    )


def _is_ssh_path(target: str) -> bool:
    """目标是否位于任一 .ssh 目录下（凭据植入/擦除敏感点）"""
    t = _normalize_target(target)
    return "/.ssh" in t or t.endswith("/.ssh") or t == ".ssh"


def _is_persistence_target(target: str) -> bool:
    """写入目标是否命中持久化/植入类路径（重定向或 tee）"""
    t = _normalize_target(target)
    if any(t == p or t.startswith(p) for p in _PERSISTENCE_PREFIXES):
        return True
    # 任意 .ssh 目录下的写入（authorized_keys 植入等）
    return _is_ssh_path(t)


def _token_rm_find_analysis(segment: str) -> tuple[str, str | None]:
    """token 级 rm/rmdir/find/shred/truncate 分析

    - rm/rmdir：收集其后 flag token（- 开头，拆出字母集合，-- 长选项忽略）
      与第一个非 flag token（目标路径）；recursive（r/R）且目标为
      根目录/敏感目录 → hardline；其他 recursive + 绝对路径 → dangerous；
      非 recursive 删除敏感目录内文件 → dangerous
    - find：带 -delete/-exec/-execdir 且目标为敏感目录 → dangerous；目标为 / → hardline
    - shred/truncate：目标为敏感路径 → dangerous

    Returns:
        (level, reason)；未命中返回 ("safe", None)
    """
    try:
        # 检测副本已归一化且剥掉引号，posix 解析足够；errors kwarg 在本版本
        # shlex.split 不存在，直接解析即可（未闭合引号抛 ValueError）
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return ("safe", None)
    if not tokens:
        return ("safe", None)

    best: tuple[str, str | None] = ("safe", None)

    def _upd(level: str, reason: str) -> None:
        nonlocal best
        if _SEVERITY[level] > _SEVERITY[best[0]]:
            best = (level, reason)

    for idx, verb in enumerate(tokens):
        if verb in ("rm", "rmdir"):
            flags: set[str] = set()
            target: str | None = None
            j = idx + 1
            while j < len(tokens):
                t = tokens[j]
                if t.startswith("--"):
                    j += 1
                    continue
                if t.startswith("-") and len(t) > 1:
                    flags.update(t[1:])
                    j += 1
                    continue
                target = t
                break
            if target is None:
                continue
            recursive = ("r" in flags) or ("R" in flags)
            norm = _normalize_target(target)
            # 家目录整体删除（~ / $HOME / ~/* 等）视同根目录
            home_root_globs = {"~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*",
                               "${HOME}", "${HOME}/", "${HOME}/*"}
            if recursive:
                if norm == "/":
                    _upd("hardline", f"recursive delete of root filesystem ({verb} {target})")
                elif norm in home_root_globs or (_is_home_root(norm) and _is_ssh_path(norm)):
                    _upd("hardline", f"recursive delete of home directory or .ssh ({verb} {target})")
                elif _is_home_relative(norm) and _is_ssh_path(norm):
                    _upd("hardline", f"recursive delete of .ssh under home ({verb} {target})")
                elif _is_home_relative(norm):
                    _upd("dangerous", f"recursive delete under home directory ({verb} {target})")
                elif norm.startswith("/"):
                    if _is_sensitive_path(norm):
                        _upd("hardline", f"recursive delete of sensitive system path ({verb} {target})")
                    else:
                        _upd("dangerous", f"recursive delete of absolute path ({verb} {target})")
            elif _is_sensitive_path(norm) or _is_home_relative(norm):
                _upd("dangerous", f"delete of sensitive system path or home ({verb} {target})")
        elif verb == "find":
            target = None
            destructive = False
            for t in tokens[idx + 1:]:
                if t in ("-delete", "-exec", "-execdir"):
                    destructive = True
                if target is None and not t.startswith("-"):
                    target = t
            if target is not None:
                norm = _normalize_target(target)
                if destructive:
                    if norm == "/":
                        _upd("hardline", "find -delete/-exec on root filesystem")
                    elif _is_sensitive_path(norm) or _is_home_relative(norm):
                        _upd("dangerous", f"find -delete/-exec on sensitive system path ({target})")
        elif verb in ("shred", "truncate"):
            target = None
            for t in tokens[idx + 1:]:
                if not t.startswith("-"):
                    target = t
                    break
            if target is not None and (_is_sensitive_path(target) or _is_home_relative(target)):
                _upd("dangerous", f"{verb} on sensitive system path ({target})")

    return best


def _extract_subshell_contents(text: str) -> list[str]:
    """提取 $() 与反引号内层内容（$() 支持嵌套）"""
    inners: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 1
            j = i + 2
            start = j
            while j < n and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inners.append(text[start:j])
            i = j + 1
        elif ch == "`":
            j = text.find("`", i + 1)
            if j == -1:
                break
            inners.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return inners


def _classify_subshells(segment: str) -> tuple[str, str | None]:
    """递归分类 $()/反引号 内层命令，取最高严重度"""
    best: tuple[str, str | None] = ("safe", None)
    for inner in _extract_subshell_contents(segment):
        level, reason = classify_command(inner)
        if _SEVERITY[level] > _SEVERITY[best[0]]:
            best = (level, reason)
    return best


def _strip_prefix_wrappers(tokens: list[str]) -> list[str]:
    """剥离命令前的包装器（env/nohup/sudo/...）与环境赋值（FOO=bar），返回真实 token 序列"""
    out: list[str] = []
    for t in tokens:
        if not out and (t in _WRAPPERS or (t and t[0].isalpha() and "=" in t and not t.startswith("-"))):
            continue
        out.append(t)
    return out


def _classify_interpreter(segment: str) -> tuple[str, str | None]:
    """首 token 为解释器/脚本执行器 → dangerous（需审批）

    字符串参数内容对 token 级分析不可见，解释器调用（sh -c / python -c /
    bash script / base64 -d|sh 等）是间接执行的通用载体，一律不得判 safe。
    内层字面危险命令仍由正则表/子 shell 递归升级（如 sh -c "rm -rf /" → hardline）。
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return ("dangerous", "unparseable segment (conservative)")
    tokens = _strip_prefix_wrappers(tokens)
    if not tokens:
        return ("safe", None)
    first = tokens[0]
    base = first.rsplit("/", 1)[-1]
    base_lower = base.lower()
    if base in _INTERPRETERS or base_lower in _INTERPRETERS \
            or base.startswith(_INTERPRETER_PREFIXES) or base_lower.startswith(_INTERPRETER_PREFIXES):
        return ("dangerous", f"interpreter/indirect execution ({base})")
    return ("safe", None)


# 读取类动词：目标命中 _SECRET_FILE_RE 时升级
_READ_VERBS = {"cat", "less", "more", "head", "tail", "cp", "scp", "rsync", "dd", "strings", "od", "xxd"}


def _classify_secret_read(segment: str) -> tuple[str, str | None]:
    """读取认证凭据/权限文件（/etc/shadow、*.ssh/id_* 等）→ dangerous"""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return ("safe", None)
    tokens = _strip_prefix_wrappers(tokens)
    if not tokens:
        return ("safe", None)
    verb = tokens[0].rsplit("/", 1)[-1].lower()
    if verb not in _READ_VERBS:
        return ("safe", None)
    for t in tokens[1:]:
        if _SECRET_FILE_RE.search(t):
            return ("dangerous", f"read of credential file ({t})")
    return ("safe", None)


# B-3 修复：以"落盘新文件/移动目标"为主要副作用的动词 —
# 最后一个非 flag 参数即写入目标，与 > / >> / tee 同等判定
_WRITE_VERBS = {"cp", "mv", "install", "ln", "rsync"}


def _classify_write_targets(text: str) -> tuple[str, str | None]:
    """扫描重定向 > / >> 、tee 与 cp/mv/install/ln/rsync 的写入目标（全串，跨段有效）：
    - 持久化/植入目标（/etc/shadow、sudoers、任意 .ssh 等）→ hardline
    - 其他敏感目录（/etc、/boot、...）或主目录下 → dangerous
    排除 <<< here-string 与 2>&1 类噪声（见 _REDIRECT_RE）。
    """
    best: tuple[str, str | None] = ("safe", None)

    def _check_target(t: str) -> None:
        nonlocal best
        if _is_persistence_target(t):
            if best[0] != "hardline":
                best = ("hardline", f"write to persistence/credential target ({t})")
        elif _is_sensitive_path(t) or _is_home_relative(t):
            if _SEVERITY[best[0]] < _SEVERITY["dangerous"]:
                best = ("dangerous", f"write to sensitive path ({t})")

    # 1. 重定向 > / >>（剥引号副本上扫描，引号无法隐藏目标）
    for m in _REDIRECT_RE.finditer(text):
        _check_target(m.group(1))
    # 2. tee 的文件参数
    for m in re.finditer(r"\btee\b([^\n|;&]*)", text):
        argstr = m.group(1)
        try:
            args = shlex.split(argstr, posix=True)
        except ValueError:
            args = argstr.split()
        for a in args:
            if a.startswith("-"):
                continue
            _check_target(a)
            break  # 只取第一个文件目标
    # 3. 写动词目标：cp/mv/install/ln/rsync 的最后一个非 flag 参数（B-3）
    #    与重定向/tee 用同一套 _check_target 判定，结果取更严重者（_check_target 内部合并）
    for segment in _split_command_chain(text):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        tokens = _strip_prefix_wrappers(tokens)
        if not tokens:
            continue
        verb = tokens[0].rsplit("/", 1)[-1].lower()
        if verb not in _WRITE_VERBS:
            continue
        target = None
        for t in tokens[1:]:
            if not t.startswith("-"):
                target = t  # 逐个覆盖，最终为最后一个非 flag 参数
        if target is not None:
            _check_target(target)
    return best


def _classify_download_exec(segments: list[str]) -> tuple[str, str | None]:
    """下载后执行启发式（跨段）：curl/wget 带 -o/-O/--output 输出文件，
    且后续段执行/授权该文件（chmod +x、./x、绝对路径执行、sh x）→ dangerous"""
    # 1. 找下载段，确定目标文件
    # curl -O = 远端 basename（目标未知）；wget -O <file> = 指定本地文件（目标已知）
    has_download = False
    download_target: str | None = None  # None = 有下载但目标未知
    for seg in segments:
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = seg.split()
        if not tokens or tokens[0].rsplit("/", 1)[-1].lower() not in ("curl", "wget"):
            continue
        has_download = True
        verb = tokens[0].rsplit("/", 1)[-1].lower()
        for i in range(1, len(tokens)):
            t = tokens[i]
            if t in ("-o", "--output") and i + 1 < len(tokens):
                download_target = tokens[i + 1]
                break
            if t == "-O" and verb == "wget" and i + 1 < len(tokens):
                download_target = tokens[i + 1]
                break
            if t == "-O":
                download_target = None
                break
            if t.startswith("--output="):
                download_target = t.split("=", 1)[1]
                break
        break
    if not has_download:
        return ("safe", None)

    base = (download_target or "").rsplit("/", 1)[-1]
    # 2. 检查后续段是否执行/授权该文件
    for seg in segments:
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        t0 = tokens[0]
        related = (download_target is None
                   or download_target in seg
                   or (base and base in seg))
        if t0 == "chmod" and "+x" in tokens[1:] and related:
            return ("dangerous", "chmod +x on downloaded file")
        if t0.startswith("./") and related:
            return ("dangerous", "execute downloaded file")
        if t0.startswith("/") and t0 == download_target:
            return ("dangerous", "execute downloaded file")
        if t0 in ("sh", "bash", "zsh", "dash", "ksh") and related:
            return ("dangerous", "execute downloaded script with shell")
        if download_target is None and t0.startswith(("/tmp/", "/var/tmp/", "/dev/shm/")):
            return ("dangerous", "execute file from temp dir after download")
    return ("safe", None)


def _classify_segment(segment: str) -> tuple[str, str | None]:
    """对单个命令段做完整检测，取最高严重度（hardline > dangerous > safe）

    规则（按序累积，reason 说明命中的规则）：
    1. 现有 HARDLINE/DANGEROUS 正则表（保留不动）
    2. 间接展开规则：破坏性动词 + $( )/反引号/${ } → 至少 dangerous
    3. token 级 rm/rmdir/find/shred/truncate 分析
    4. $()/反引号 内层递归
    5. 解释器/间接执行：首 token 为解释器 → 至少 dangerous
    6. 敏感文件读取：cat/less/dd 等命中凭据文件 → dangerous
    """
    best: tuple[str, str | None] = ("safe", None)

    def _upd(level: str, reason: str) -> None:
        nonlocal best
        if _SEVERITY[level] > _SEVERITY[best[0]]:
            best = (level, reason)

    # 1. 现有正则表
    for pattern, reason in HARDLINE_PATTERNS:
        if pattern.search(segment):
            _upd("hardline", reason)
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(segment):
            _upd("dangerous", reason)

    # 2. 间接展开规则
    if _DESTRUCTIVE_VERB_RE.search(segment) and _INDIRECT_EXPANSION_RE.search(segment):
        _upd("dangerous", "destructive command with indirect expansion")

    # 3. token 级分析
    level, reason = _token_rm_find_analysis(segment)
    if level != "safe":
        _upd(level, reason)

    # 4. 子 shell 递归
    level, reason = _classify_subshells(segment)
    if level != "safe":
        _upd(level, reason)

    # 5. 解释器/间接执行
    level, reason = _classify_interpreter(segment)
    if level != "safe":
        _upd(level, reason)

    # 6. 敏感文件读取
    level, reason = _classify_secret_read(segment)
    if level != "safe":
        _upd(level, reason)

    return best


# ──────────────────────────────────────────────────────────────────────────────
# 主检测函数
# ──────────────────────────────────────────────────────────────────────────────

def classify_command(command: str) -> tuple[str, str | None]:
    """分类命令风险等级

    Returns:
        (level, reason)
        level: "hardline" (无条件阻断) | "dangerous" (需审批) | "safe" (放行)
        reason: 阻断/需审批的原因（level=safe 时为 None）

    加固流程（保留 normalize + 正则表作为第一层）：
    1. sudo stdin guard（先于其他检查）
    2. 生成检测副本（归一化 + 剥掉所有单双引号）
    3. 全串正则表检测（修复管道类模式：`|` 是分段符，curl|sh 等模式 per-segment
       永远匹配不到；fork bomb 完整形态同理）
    4. 全串写目标扫描（> / >> / tee → 持久化/敏感路径）
    5. 命令链分段（; / && / || / | / 换行），对每段跑完整检测（含解释器/家目录/敏感读取）
    6. 跨段下载执行启发式（curl/wget -o && 执行）
    7. 取所有检测的最高严重度（hardline > dangerous > safe）
    """
    # 1. sudo stdin guard（基于原始命令）
    blocked, reason = check_sudo_stdin_guard(command)
    if blocked:
        return ("hardline", reason)

    # 2. 检测副本：归一化 + 剥引号（仅用于模式匹配）
    detection = _detection_copy(command)

    # 3. 全串正则表（与 per-segment 取并集：全串覆盖跨段模式，segment 覆盖段内 $ 边界）
    best: tuple[str, str | None] = ("safe", None)
    for pattern, hreason in HARDLINE_PATTERNS:
        if pattern.search(detection):
            return ("hardline", hreason)
    for pattern, dreason in DANGEROUS_PATTERNS:
        if pattern.search(detection):
            best = ("dangerous", dreason)

    # 4. 全串写目标扫描（重定向 / tee）
    level, reason = _classify_write_targets(detection)
    if _SEVERITY[level] > _SEVERITY[best[0]]:
        best = (level, reason)
    if best[0] == "hardline":
        return best

    # 5. 命令链分段检测
    segments = _split_command_chain(detection)
    for segment in segments:
        level, reason = _classify_segment(segment)
        if _SEVERITY[level] > _SEVERITY[best[0]]:
            best = (level, reason)
        if best[0] == "hardline":
            break  # 已到最高严重度，提前结束

    # 6. 跨段下载执行启发式
    level, reason = _classify_download_exec(segments)
    if _SEVERITY[level] > _SEVERITY[best[0]]:
        best = (level, reason)
    return best


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
