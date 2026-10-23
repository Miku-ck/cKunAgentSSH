"""只读证明 — 声明式只读规则、破坏性写守卫、执行注入守卫。

三条判定链互相独立，调用方（safety.py）按固定顺序执行：
  1. classify_exec_injection：命令是否把执行权交给外部程序
  2. classify_destructive_write：命令是否含写/删除动作
  3. classify_readonly：剩余命令能否被证明只读（失败即回默认审批）

本模块不做整串分级，也不读取意图/环境；只回答关于“这一段命令文本”的三个问题。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .command_parser import (
    ChainSegment,
    CommandParseError,
    split_command_chain,
    strip_prefix_wrappers,
    tokenize_segment,
)

__all__ = [
    "ReadonlyVerdict",
    "classify_destructive_write",
    "classify_exec_injection",
    "classify_readonly",
    "classify_legacy_readonly",
    "is_provably_readonly",
    "tar_list_mode",
]


@dataclass(frozen=True)
class ReadonlyVerdict:
    safe: bool
    rule_id: str = ""
    reason: str | None = None


_SAFE = ReadonlyVerdict(True)


def _bad(rule_id: str, reason: str) -> ReadonlyVerdict:
    return ReadonlyVerdict(False, rule_id, reason)


# ── 通用前置：写重定向 / 命令替换 / wrapper ─────────────────────────────────

def _has_unquoted_expansion(raw: str) -> bool:
    """未加单引号的 $() / ${} / 反引号 / <() / >() → 存在命令替换。

    $() / 反引号在双引号内仍然生效；<() / >() 进程替换在双引号内是字面量，只在未加引号时检出。
    """
    quote = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = ""
                i += 1
                continue
            if ch == "`" or (ch == "$" and i + 1 < len(raw) and raw[i + 1] in "({"):
                return True
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "`" or (ch == "$" and i + 1 < len(raw) and raw[i + 1] in "({"):
            return True
        if ch in "<>" and i + 1 < len(raw) and raw[i + 1] == "(":
            return True
        i += 1
    return False


def _write_redirect_target(raw: str) -> str:
    """返回第一个非安全设备的重定向目标；仅 > / >> / >| / &> 生效。

    引号内的 > 不算（jq 过滤器常见写法）；2>&1、>&2 是 fd 复制，不算写盘；
    /dev/null、/dev/fd/*、/dev/stdout、/dev/stderr 不算持久化。
    """
    quote = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == ">" and (i == 0 or raw[i - 1] not in "<>"):
            j = i + 1
            if j < len(raw) and raw[j] == ">":
                j += 1
            if j < len(raw) and raw[j] == "|":
                j += 1
            while j < len(raw) and raw[j].isspace():
                j += 1
            if j < len(raw) and raw[j] == "&":
                i = j + 1
                continue
            k = j
            while k < len(raw) and not raw[k].isspace() and raw[k] not in ";|&<>":
                k += 1
            target = raw[j:k]
            if target.startswith("/dev/"):
                rest = target[len("/dev/"):]
                if rest in {"null", "zero", "random", "urandom", "full", "stdin",
                            "stdout", "stderr", "console"} or rest.startswith(
                                ("tty", "pts", "fd", "shm")):
                    i = k
                    continue
            if target:
                return target
            i = k
            continue
        i += 1
    return ""


def _tokens_or_none(segment: str) -> list[str] | None:
    try:
        tokens = tokenize_segment(segment)
    except CommandParseError:
        return None
    return strip_prefix_wrappers(tokens)


# ── 只读证明 ────────────────────────────────────────────────────────────────

_SIMPLE_READONLY: dict[str, str] = {
    "jq": "ro.verb.jq.v1",
    "yq": "ro.verb.yq.v1",
    "dig": "ro.verb.dig.v1",
    "nslookup": "ro.verb.nslookup.v1",
    "host": "ro.verb.host.v1",
    "ping": "ro.verb.ping.v1",
    "traceroute": "ro.verb.traceroute.v1",
    "lsblk": "ro.verb.lsblk.v1",
    "blkid": "ro.verb.blkid.v1",
    "findmnt": "ro.verb.findmnt.v1",
    "dmidecode": "ro.verb.dmidecode.v1",
    "zipinfo": "ro.verb.zipinfo.v1",
    "date": "ro.verb.date.v1",
    "find": "ro.verb.find.v1",
}

_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "jq": frozenset({"-i", "--in-place"}),
    "yq": frozenset({"-i", "--inplace", "--in-place", "-s", "--split-exp", "--split-exp-file"}),
    "blkid": frozenset({"-w", "--write-cache"}),
    "dmidecode": frozenset({"--dump-bin"}),
    "openssl": frozenset({"-out", "-keyout", "-signkey", "-CAcreateserial", "-CAserial"}),
}


def _flag_hit(args: list[str], flags: frozenset[str]) -> bool:
    for t in args:
        if t in flags:
            return True
        if t.startswith("--"):
            stem = t.split("=", 1)[0]
            if stem in flags:
                return True
            # getopt_long 接受长选项的无歧义前缀（`blkid --write-c`）
            if len(stem) >= 6 and any(
                f.startswith("--") and f.startswith(stem) for f in flags
            ):
                return True
    return False


def _yq_write_flags(args: list[str]) -> bool:
    for t in args:
        if t.startswith("--"):
            if t.split("=", 1)[0] in _WRITE_FLAGS["yq"]:
                return True
            continue
        if t.startswith("-") and len(t) > 1 and any(c in t[1:] for c in "is"):
            return True
    return False


# dig 的 +option 里藏着执行开关：`+cmd` 直接把后续参数交给 shell，`+file` 读指令
# 文件。格式化/统计这一小组才是纯读形态，其余一律不证明。
_DIG_SAFE_PLUS = frozenset({
    "short", "noall", "nocmd", "comments", "nocomment", "qrdcomment",
    "stats", "time", "nortime", "identify", "ttlid", "answer", "authority",
    "additional", "recurse", "norecurse", "showfailed", "showretry", "showall",
})


def _classify_simple(base: str, tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if base == "yq":
        if _yq_write_flags(args):
            return ReadonlyVerdict(False)
    elif _flag_hit(args, _WRITE_FLAGS.get(base, frozenset())):
        return ReadonlyVerdict(False)
    if base == "dig" and any(
        t.startswith("+") and t[1:].split("=", 1)[0] not in _DIG_SAFE_PLUS
        for t in args
    ):
        return ReadonlyVerdict(False)
    if base == "ping" and any(t in ("-f", "--flood") for t in args):
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, _SIMPLE_READONLY[base])


def _classify_getent(tokens: list[str]) -> ReadonlyVerdict:
    args = [t for t in tokens[1:] if not t.startswith("-")]
    if not args or args[0] not in {"passwd", "group"}:
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.sub.getent.v1")


_SMARTCTL_INFO = frozenset({
    "-i", "--info", "-a", "--all", "-H", "--health", "-A", "--attributes",
    "-x", "--xall", "-c", "--capabilities", "-g", "--general",
})
_SMARTCTL_ACTIONS = (
    "-t", "--test", "-s", "--smart", "-o", "--offlineauto", "-S", "--saveauto",
    "-X", "--abort", "--set", "-e",
)


def _classify_smartctl(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if not args or any(t.startswith(_SMARTCTL_ACTIONS) for t in args):
        return ReadonlyVerdict(False)
    if not any(t in _SMARTCTL_INFO or t.split("=", 1)[0] in _SMARTCTL_INFO for t in args):
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.smartctl.v1")


_ETHTOOL_WRITE = frozenset({
    "-s", "--change", "-K", "--features", "-G", "--set-ring", "-L", "--set-channels",
    "-A", "--pause", "-C", "--coalesce", "-N", "--set-ntuple", "-f", "--flash",
    "-E", "--change-eeprom", "-p", "--identify", "-r", "--reset", "-t", "--test",
    "-e", "--eeprom-dump", "--set-priv-flags", "--set-eee", "--set-fec",
})


def _classify_ethtool(tokens: list[str]) -> ReadonlyVerdict:
    for t in tokens[1:]:
        if t in _ETHTOOL_WRITE:
            return ReadonlyVerdict(False)
        if t.startswith("--"):
            stem = t.split("=", 1)[0]
            if len(stem) >= 6 and any(opt.startswith(stem) for opt in _ETHTOOL_WRITE):
                return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.ethtool.v1")


def _classify_tc(tokens: list[str]) -> ReadonlyVerdict:
    words = [t for t in tokens[1:] if not t.startswith("-")]
    if len(words) >= 2 and words[0] in {"qdisc", "class", "filter", "link"} \
            and words[1] in {"show", "ls", "list"}:
        return ReadonlyVerdict(True, "ro.sub.tc.v1")
    return ReadonlyVerdict(False)


def _classify_nft(tokens: list[str]) -> ReadonlyVerdict:
    words = [t for t in tokens[1:] if not t.startswith("-")]
    if words and words[0] == "list":
        return ReadonlyVerdict(True, "ro.sub.nft.v1")
    return ReadonlyVerdict(False)


_IPTABLES_LONG = frozenset({
    "--list", "--list-rules", "--numeric", "--verbose", "--exact",
    "--table", "--line-numbers", "--wait",
})


def _classify_iptables(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    listing = False
    i = 0
    while i < len(args):
        t = args[i]
        if t in {"-t", "--table"}:
            i += 2
            continue
        if t.startswith("--table="):
            i += 1
            continue
        if t.startswith("--"):
            stem = t.split("=", 1)[0]
            if stem not in _IPTABLES_LONG:
                return ReadonlyVerdict(False)
            if stem in {"--list", "--list-rules"}:
                listing = True
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if any(c not in "LnvSxwW" for c in letters):
                return ReadonlyVerdict(False)
            if "L" in letters or "S" in letters:
                listing = True
            i += 1
            continue
        return ReadonlyVerdict(False)
    if not listing:
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.iptables.v1")


_DOCKER_GLOBAL_VALUES = frozenset({
    "-H", "--host", "--context", "--config", "--tlscacert", "--tlscert",
    "--tlskey", "--log-level",
})
_DOCKER_READ_SUBCOMMANDS = frozenset({"ps", "images", "inspect", "logs", "version", "info"})
_COMPOSE_READ_SUBCOMMANDS = frozenset({"ps", "logs", "config", "images", "top"})
_COMPOSE_VALUE_FLAGS = frozenset({
    "-f", "--file", "-p", "--project-name", "--env-file", "--profile",
    "--project-directory", "--ansi", "--parallel",
})


def _docker_head(tokens: list[str]) -> tuple[str, list[str]]:
    args = tokens[1:]
    i = 0
    while i < len(args):
        t = args[i]
        if not t.startswith("-"):
            break
        if t in _DOCKER_GLOBAL_VALUES:
            i += 2
        else:
            i += 1
    if i >= len(args):
        return "", []
    return args[i], args[i + 1:]


def _classify_docker(tokens: list[str]) -> ReadonlyVerdict:
    sub, rest = _docker_head(tokens)
    if not sub:
        return ReadonlyVerdict(False)
    if sub in _DOCKER_READ_SUBCOMMANDS:
        return ReadonlyVerdict(True, "ro.sub.docker.v1")
    if sub == "stats":
        return ReadonlyVerdict(True, "ro.sub.docker.v1") if "--no-stream" in rest else ReadonlyVerdict(False)
    if sub == "system" and rest and rest[0] in {"df", "info"}:
        return ReadonlyVerdict(True, "ro.sub.docker.v1")
    if sub == "compose":
        if any(t.startswith("-o") or t == "--output" or t.startswith("--output=")
               for t in rest):
            return ReadonlyVerdict(False)
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in _COMPOSE_VALUE_FLAGS:
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            if t in _COMPOSE_READ_SUBCOMMANDS:
                return ReadonlyVerdict(True, "ro.sub.docker_compose.v1")
            return ReadonlyVerdict(False)
    return ReadonlyVerdict(False)


_KUBECTL_GLOBAL_VALUES = frozenset({
    "-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
    "--server", "--token",
})


def _classify_kubectl(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in _KUBECTL_GLOBAL_VALUES:
            i += 2
        else:
            i += 1
    if i >= len(args):
        return ReadonlyVerdict(False)
    if args[i] == "rollout" and i + 1 < len(args) and args[i + 1] in {"status", "history"}:
        return ReadonlyVerdict(True, "ro.sub.kubectl.v1")
    if args[i] == "auth" and i + 1 < len(args) and args[i + 1] == "can-i":
        return ReadonlyVerdict(True, "ro.sub.kubectl.v1")
    return ReadonlyVerdict(False)


_HELM_GLOBAL_VALUES = frozenset({"-n", "--namespace", "--kube-context", "--kubeconfig"})


def _classify_helm(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in _HELM_GLOBAL_VALUES:
            i += 2
        else:
            i += 1
    if i + 1 < len(args) and args[i] == "show" \
            and args[i + 1] in {"chart", "values", "readme", "all"}:
        return ReadonlyVerdict(True, "ro.sub.helm.v1")
    return ReadonlyVerdict(False)


def _classify_unzip(tokens: list[str]) -> ReadonlyVerdict:
    listing = False
    for t in tokens[1:]:
        if t.startswith("--"):
            if t.split("=", 1)[0] == "--list":
                listing = True
            continue
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if any(c in letters for c in "ltvZ"):
                listing = True
            if any(c in letters for c in "oxdn"):
                return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.unzip.v1") if listing else ReadonlyVerdict(False)


def _classify_openssl(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if not args or args[0] != "x509":
        return ReadonlyVerdict(False)
    if _flag_hit(args[1:], _WRITE_FLAGS["openssl"]):
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.openssl_x509.v1")


_TAR_LONG_WRITE = frozenset({
    "--extract", "--get", "--append", "--update", "--delete",
    "--concatenate", "--catenate",
})
_TAR_LONG_LIST = frozenset({"--list"})
_TAR_LONG_CREATE = frozenset({"--create"})
# POSIX 裸模式串（不带 - 的 `tar xzf a.tgz`、`tar x app.tar`）是合法形态。
# 只在尚未确定模式时识别，因此档案/成员名（`tar cf a.tar cat`）不会被误读；
# 代价是 `tar --file=a.tar x` 这类把成员名写成操作字母的形态保守升级一次审批。
_TAR_BARE_OPS = "rxutcA"
_TAR_BARE_LETTERS = frozenset("rxutcAvpfzjJOam")


def _tar_mode(tokens: list[str]) -> str:
    mode = ""
    for t in tokens[1:]:
        if t.startswith("--"):
            stem = t.split("=", 1)[0]
            if stem in _TAR_LONG_WRITE:
                return "write"
            if stem in _TAR_LONG_LIST and not mode:
                mode = "list"
            elif stem in _TAR_LONG_CREATE and not mode:
                mode = "create"
            continue
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if any(c in letters for c in "xurA"):
                return "write"
            if "t" in letters and not mode:
                mode = "list"
            elif "c" in letters and not mode:
                mode = "create"
            continue
        if not mode and t[0] in _TAR_BARE_OPS \
                and all(c in _TAR_BARE_LETTERS for c in t):
            if any(c in t for c in "rxuA"):
                return "write"
            if "t" in t:
                mode = "list"
            elif "c" in t:
                mode = "create"
    return mode


def tar_list_mode(tokens: list[str]) -> bool:
    return _tar_mode(tokens) == "list"


_GIT_GLOBAL_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env", "--super-prefix",
})
_GIT_CONFIG_WRITE = frozenset({
    "--add", "--replace-all", "--unset", "--unset-all", "--rename-section",
    "--remove-section", "--edit", "-e", "--set",
})

_GIT_TRANSPORT_OPTS = ("--upload-pack", "--receive-pack", "--exec")


def _git_transport_opt(stem: str) -> bool:
    """git parse-options 接受无歧义长选项前缀（--exe / --upload-p）。"""
    return len(stem) >= 3 and any(opt.startswith(stem) for opt in _GIT_TRANSPORT_OPTS)


def _git_exec_path_opt(stem: str) -> bool:
    """git 接受 --exec-path 的无歧义前缀（--exec-p / --exec-pat）。"""
    return len(stem) >= 5 and "--exec-path".startswith(stem)


def _classify_git(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        t = args[i]
        stem = t.split("=", 1)[0]
        if stem in {"-c", "--config-env"} or _git_exec_path_opt(stem):
            return ReadonlyVerdict(False)
        if stem in _GIT_GLOBAL_VALUE:
            i += 1 if "=" in t else 2
        else:
            i += 1
    if i >= len(args):
        return ReadonlyVerdict(False)
    sub, rest = args[i], args[i + 1:]
    if any(_git_transport_opt(t.split("=", 1)[0]) for t in rest):
        return ReadonlyVerdict(False)
    if sub == "ls-remote":
        for t in rest:
            if not t.startswith("-") and t.startswith(("ext::", "fd::", "file://")):
                return ReadonlyVerdict(False)
        return ReadonlyVerdict(True, "ro.sub.git_ls_remote.v1")
    if sub == "config":
        if not any(t in {"--get", "--get-all", "--list", "-l"} for t in rest):
            return ReadonlyVerdict(False)
        if any(t in _GIT_CONFIG_WRITE for t in rest):
            return ReadonlyVerdict(False)
        return ReadonlyVerdict(True, "ro.flag.git_config.v1")
    return ReadonlyVerdict(False)


_SYSTEMCTL_GLOBAL_VALUES = frozenset({
    "-H", "--host", "-M", "--machine", "-n", "--lines", "-o", "--output",
    "-t", "--type", "-p", "--property", "--state", "--root", "--kill-who",
    "-s", "--signal",
})


def _classify_systemctl(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in _SYSTEMCTL_GLOBAL_VALUES:
            i += 2
        else:
            i += 1
    if i < len(args) and args[i] == "list-dependencies":
        return ReadonlyVerdict(True, "ro.sub.systemctl.v1")
    return ReadonlyVerdict(False)


def _classify_crontab(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if "-l" not in args or any(t in {"-e", "-r", "-i"} for t in args):
        return ReadonlyVerdict(False)
    i = 0
    while i < len(args):
        if args[i] == "-u":
            i += 2
            continue
        if args[i] == "-l":
            i += 1
            continue
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.crontab.v1")


def _classify_at(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if any(t in {"-r", "-d", "-f"} for t in args):
        return ReadonlyVerdict(False)
    if "-l" in args:
        return ReadonlyVerdict(True, "ro.flag.at.v1")
    if "-c" in args and len(args) >= 2:
        return ReadonlyVerdict(True, "ro.flag.at.v1")
    return ReadonlyVerdict(False)


def _classify_tar(tokens: list[str]) -> ReadonlyVerdict:
    # 只读证明独立于执行注入守卫也要拒绝 hook 选项：`tar -tI 'sh -c id'` 的
    # 列表模式照常成立，只靠调用方按顺序跑注入守卫会留一条自主绕过的路径。
    if not _tar_injection(tokens).safe:
        return ReadonlyVerdict(False)
    for t in tokens[1:]:
        stem = t.split("=", 1)[0]
        if stem.startswith("--") and len(stem) >= 5 and "--index-file".startswith(stem):
            return ReadonlyVerdict(False)
    return ReadonlyVerdict(True, "ro.flag.tar_list.v1") if tar_list_mode(tokens) else ReadonlyVerdict(False)


_SPECIAL = {
    "getent": _classify_getent,
    "smartctl": _classify_smartctl,
    "ethtool": _classify_ethtool,
    "tc": _classify_tc,
    "nft": _classify_nft,
    "iptables": _classify_iptables,
    "docker": _classify_docker,
    "kubectl": _classify_kubectl,
    "helm": _classify_helm,
    "unzip": _classify_unzip,
    "openssl": _classify_openssl,
    "tar": _classify_tar,
    "git": _classify_git,
    "systemctl": _classify_systemctl,
    "crontab": _classify_crontab,
    "at": _classify_at,
}


def classify_readonly(segment: str) -> ReadonlyVerdict:
    """证明一段命令只读；任何不确定都返回 safe=False（调用方回默认审批）。"""
    try:
        tokens = tokenize_segment(segment)
    except CommandParseError:
        return ReadonlyVerdict(False)
    stripped = strip_prefix_wrappers(tokens)
    if not stripped or len(stripped) != len(tokens):
        return ReadonlyVerdict(False)
    if _has_unquoted_expansion(segment) or _write_redirect_target(segment):
        return ReadonlyVerdict(False)
    base = stripped[0].rsplit("/", 1)[-1]
    if base in {"ssh"}:
        if stripped[1:] == ["-V"]:
            return ReadonlyVerdict(True, "ro.verb.ssh_version.v1")
        return ReadonlyVerdict(False)
    special = _SPECIAL.get(base)
    if special is not None:
        return special(stripped)
    if base in _SIMPLE_READONLY:
        return _classify_simple(base, stripped)
    return ReadonlyVerdict(False)


# ── 可见终端 prod 自动交付：旧白名单里的纯读动词 ───────────────────────────
# 新只读规则按 spec §6.0 不重复定义旧表结论，但可见终端要回答的是另一个问题：
# 「这条 safe 命令能不能证明不改状态」。这里只认显式纯读动词/子命令；写行为
# 已知或有歧义的一律不证明（tee/curl/wget/sed/awk/top/less/env/cd 不入表）。
# 旧表证明的 rule_id 为空 —— 它不是新只读规则，审计里不能冒充 readonly_rule。

_LEGACY_READONLY_VERBS = frozenset({
    # 文件/目录读取
    "ls", "ll", "dir", "cat", "head", "tail", "wc", "file", "stat", "tree",
    "du", "df", "grep", "egrep", "fgrep", "rg", "cut", "uniq", "tr", "diff",
    "column", "strings", "od", "xxd", "basename", "dirname", "realpath",
    "readlink", "md5sum", "sha1sum", "sha256sum", "cksum", "test",
    # 系统状态读取
    "ps", "uptime", "who", "whoami", "w", "id", "uname", "which", "whereis",
    "type", "pwd", "printenv", "echo", "printf", "true", "false", "sleep",
    "free", "vmstat", "iostat", "lsof", "netstat", "ss", "pgrep", "pidof",
    "dmesg", "journalctl", "sort",
})

# 这些动词的唯一非只读开关（位置参数写文件的另见 _LEGACY_WRITE_POSITIONALS）
_LEGACY_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "sort": frozenset({"-o", "--output", "--compress-program"}),
    "tree": frozenset({"-o", "--output"}),
    "diff": frozenset({"-o", "--output"}),
    "journalctl": frozenset({"--rotate", "--flush", "--sync"}),
    # -c 读后清空环形缓冲；-D/-E/-n 改控制台输出行为 —— 都不是只读
    "dmesg": frozenset({"-C", "-c", "-D", "-E", "-n", "-k",
                        "--clear", "--read-clear", "--klogd",
                        "--console-level", "--console-off", "--console-on"}),
    "iostat": frozenset({"-o"}),
    # rg 的 --pre/--hostname-bin 直接执行外部程序，--index 写索引
    "rg": frozenset({"--pre", "--pre-glob", "--hostname-bin", "--index"}),
    # -C/--compile 把 magic 数据库编译写到 .mgc 文件
    "file": frozenset({"-C", "--compile", "--recompile"}),
}

# journalctl 的写动作不止轮转：catalog 更新与 FSK 密钥生成都会落盘
_JOURNALCTL_EXTRA_WRITE = frozenset({
    "--update-catalog", "--setup-keys", "--rebuild-catalog",
})


def _legacy_write_flag_hit(args: list[str], flags: frozenset[str]) -> bool:
    """写开关匹配，覆盖 getopt 的三种合法形态。

    精确匹配会被 `sort -oout f`（短选项粘连取值）、`sort --out=/tmp/x f` 与
    `diff --out /tmp/f a b`（长选项无歧义缩写）躲过，而这些形态在 prod 是真实写盘。
    """
    if not flags:
        return False
    for t in args:
        if t in flags:
            return True
        if t.startswith("--"):
            stem = t.split("=", 1)[0]
            if stem in flags:
                return True
            # getopt_long 的无歧义缩写可以只剩一个字母（`sort --o=/tmp/x`）；
            # 真有多个候选时工具自己会报错，所以按前缀一律拒绝是安全方向。
            if len(stem) >= 3 and any(
                f.startswith("--") and f.startswith(stem) for f in flags
            ):
                return True
            continue
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            if any(len(f) == 2 and f[1] in letters for f in flags):
                return True
    return False

# 第二个位置参数是输出文件（uniq INPUT OUTPUT / xxd INFILE OUTFILE）
_LEGACY_WRITE_POSITIONALS: dict[str, int] = {"uniq": 2, "xxd": 2}

# 子命令型旧白名单：只读子命令也要拒绝其后出现的写动作（ip link set / service restart）
_LEGACY_READ_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({"status", "log", "diff", "show", "rev-parse", "shortlog",
                      "blame", "describe", "ls-files", "version"}),
    "systemctl": frozenset({"status", "show", "is-active", "is-enabled",
                            "is-failed", "is-allowed", "list-units",
                            "list-unit-files", "list-sockets", "list-timers",
                            "cat", "version"}),
    "service": frozenset({"status", "--status-all"}),
    "ip": frozenset({"addr", "address", "link", "route", "neigh", "saddr",
                     "maddr", "-V"}),
    "apt": frozenset({"list", "show", "search", "policy"}),
    "apt-get": frozenset({"list", "show", "search", "policy"}),
    "dpkg": frozenset({"-l", "--list", "-s", "--status", "-L", "--listfiles",
                       "-V", "--verify"}),
    # kubectl 的列表/查询型子命令在新只读规则里没有证明（_classify_kubectl 只认
    # rollout/auth），但它们都在旧白名单里、且无写盘形态 —— 交付证明只能由旧表接住。
    "kubectl": frozenset({"get", "describe", "logs", "explain", "top",
                          "version", "cluster-info", "api-resources"}),
}

_LEGACY_SUBCOMMAND_WRITES = frozenset({
    "set", "del", "delete", "flush", "down", "add", "change", "replace",
    "update", "upgrade", "restart", "start", "stop", "enable", "disable",
    "reload", "kill", "daemon-reload", "mask", "unmask",
})

_GIT_DELIVERY_WRITE_FLAGS = frozenset({
    "--output", "--output-directory", "--ext-diff", "--textconv",
    "--upload-pack", "--receive-pack",
})
_GIT_DELIVERY_EXEC_VALUE_FLAGS = frozenset({"-c", "--config-env", "--exec-path"})


def _kubectl_raw_api_token(token: str) -> bool:
    """kubectl get --raw 是任意 API 路径直通，配合 -X/--method 就是集群写操作。

    `kubectl get pods -X` 单独出现不改状态，但 HTTP 方法名是大写、写法自由，
    旧表无法从字符串证明它不会写；一律不证明。
    """
    low = token.lower()
    stem = low.split("=", 1)[0]
    return stem.startswith("--raw") or stem.startswith("-x") or stem == "--method"


def classify_legacy_readonly(segment: str) -> ReadonlyVerdict:
    """旧白名单里的纯读用法 → safe=True（rule_id 为空，不冒充新规则）。

    保守方向：wrapper/env 前缀、命令替换、非 /dev/null 重定向、未知动词、
    子命令型白名单里的写动作，一律不证明。
    """
    try:
        raw_tokens = tokenize_segment(segment)
    except CommandParseError:
        return ReadonlyVerdict(False)
    tokens = strip_prefix_wrappers(raw_tokens)
    if not tokens or len(tokens) != len(raw_tokens):
        return ReadonlyVerdict(False)
    if _has_unquoted_expansion(segment) or _write_redirect_target(segment):
        return ReadonlyVerdict(False)

    base = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]

    if base in _LEGACY_READONLY_VERBS:
        if _legacy_write_flag_hit(args, _LEGACY_WRITE_FLAGS.get(base, frozenset())):
            return ReadonlyVerdict(False)
        if base == "journalctl" and (
            any(t.startswith("--vacuum") for t in args)
            or _legacy_write_flag_hit(args, _JOURNALCTL_EXTRA_WRITE)
        ):
            return ReadonlyVerdict(False)
        limit = _LEGACY_WRITE_POSITIONALS.get(base)
        if limit is not None:
            positional = [t for t in args if not t.startswith("-")]
            if len(positional) >= limit:
                return ReadonlyVerdict(False)
        return ReadonlyVerdict(True)

    allowed = _LEGACY_READ_SUBCOMMANDS.get(base)
    if allowed is None:
        return ReadonlyVerdict(False)
    if base == "git" and (
        _flag_hit(args, _GIT_DELIVERY_WRITE_FLAGS)
        or _flag_hit(args, _GIT_DELIVERY_EXEC_VALUE_FLAGS)
    ):
        return ReadonlyVerdict(False)
    if base == "dpkg":
        if not args or args[0] not in allowed:
            return ReadonlyVerdict(False)
    elif base == "service":
        # service <name> status：只读子命令不一定是第一个位置参数
        if not any(t in allowed for t in args):
            return ReadonlyVerdict(False)
    else:
        rest = args
        if base == "kubectl":
            # `-n prod` 这类全局取值开关会把子命令挤出 positional[0]，
            # 与新规则里的 _classify_kubectl 同一口径跳过
            i = 0
            while i < len(rest) and rest[i].startswith("-"):
                stem = rest[i].split("=", 1)[0]
                if stem in _KUBECTL_GLOBAL_VALUES:
                    i += 1
                    if i < len(rest) and not rest[i].startswith("-"):
                        i += 1
                else:
                    i += 1
            rest = rest[i:]
        positional = [t for t in rest if not t.startswith("-")]
        if not positional or positional[0] not in allowed:
            return ReadonlyVerdict(False)
    if base == "kubectl" and any(_kubectl_raw_api_token(t) for t in args):
        return ReadonlyVerdict(False)
    if any(t in _LEGACY_SUBCOMMAND_WRITES for t in args):
        return ReadonlyVerdict(False)
    return ReadonlyVerdict(True)


_FOLLOW_FORMS: dict[str, frozenset[str]] = {
    "tail": frozenset({"-f", "-F", "--follow"}),
    "journalctl": frozenset({"-f", "-F", "--follow"}),
    "dmesg": frozenset({"-w", "--follow"}),
}
# `-f` 在这些动词里只在某个子命令下才是跟随（`kubectl get -f file`、`docker ps -f` 是读）
_FOLLOW_SUBCOMMANDS = {"kubectl": "logs", "docker": "logs"}
_REPEAT_FORMS: dict[str, frozenset[str]] = {
    "netstat": frozenset({"-c", "--continuous"}),
    "lsof": frozenset({"-r", "--repeat"}),
}
# 不给文件名就退化成等 stdin：PTY 里永远不会送来输入，交付只能等到超时
_STDIN_WAITING_VERBS = frozenset({
    "cat", "tail", "head", "wc", "sort", "grep", "egrep", "fgrep", "rg",
    "uniq", "cut", "tr", "column", "xxd", "od", "strings",
    "md5sum", "sha1sum", "sha256sum", "cksum",
})
# 除 /dev/null 外的字符设备是无限源：`cat /dev/zero` 会淹没终端
_QUIET_DEVICES = frozenset({"null"})
# 模式+文件两个操作数才不构成等 stdin；只有一个参数时它是模式（grep 从 stdin 读）
_GREP_LIKE_VERBS = frozenset({"grep", "egrep", "fgrep", "rg"})
# tr 没有文件操作数，只能从 stdin 读
_TR_VERBS = frozenset({"tr"})
# kubectl 的 watch 形态在所有只读子命令下都是无限流
_KUBECTL_WATCH_FLAGS = frozenset({"-w", "--watch", "--watch-only"})
_GREP_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-f", "--file"})
# 超过这个时长的等待/循环不自动交付：可见终端交付本身就要在 300s 超时收口
_MAX_AUTO_WAIT_SECONDS = 280.0


def _looks_like_file_operand(token: str) -> bool:
    """把 `cut -b 1` 的 1 这种「选项取值」从文件操作数里剔出去。

    只用于「要不要等 stdin」这一判断，方向保守：认不出文件就多开一张确认卡。
    """
    return not token.isdigit()


def _sleep_seconds(tokens: list[str]) -> float | None:
    total = 0.0
    for t in tokens:
        unit = t[-1:]
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
        number = t[:-1] if multiplier and unit in "smhd" else t
        if multiplier is None:
            multiplier = 1
        try:
            total += float(number) * multiplier
        except ValueError:
            return None
    return total


def _option_value(args: list[str], names: tuple[str, ...]) -> str | None:
    for i, t in enumerate(args):
        if t in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if t.startswith(name + "="):
                return t.split("=", 1)[1]
    return None


def _blocks_terminal(seg: ChainSegment) -> bool:
    """跟随/无限运行形态：不改磁盘状态，但会把可见终端占死。

    交付等待靠超时收口，`tail -f` 自动写进 prod 终端后这一条 run 就挂在捕获上，
    用户看不到「已执行完」也拿不到输出摘要；证明阶段直接不认，让它走确认卡。
    """
    try:
        tokens = strip_prefix_wrappers(tokenize_segment(seg.raw))
    except CommandParseError:
        return True
    if not tokens:
        return False
    base = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    positional = [t for t in args if not t.startswith("-")]

    if _legacy_write_flag_hit(args, _FOLLOW_FORMS.get(base, frozenset())):
        return True
    if base in _FOLLOW_SUBCOMMANDS \
            and _FOLLOW_SUBCOMMANDS[base] in positional \
            and _legacy_write_flag_hit(args, frozenset({"-f", "-F", "--follow"})):
        return True
    if _legacy_write_flag_hit(args, _REPEAT_FORMS.get(base, frozenset())):
        return True
    if base == "kubectl" and (
        _legacy_write_flag_hit(args, _KUBECTL_WATCH_FLAGS)
        or (_option_value(args, ("-o", "--output")) or "").split("+")[0]
        in {"watch", "json-watch", "yaml-watch"}
    ):
        return True
    # 管道段的上游就是它的 stdin，不算挂等；只有链首/分号后的段才会等 PTY 输入
    if not seg.stdin_from_pipe:
        files = [t for t in positional if _looks_like_file_operand(t)]
        if base in _TR_VERBS:
            return True
        if base in _GREP_LIKE_VERBS and len(files) < 2 \
                and not any(t.split("=", 1)[0] in _GREP_PATTERN_FLAGS for t in args):
            return True
        if base in _STDIN_WAITING_VERBS and not files:
            return True
    if any(t.startswith("/dev/") and t[len("/dev/"):] not in _QUIET_DEVICES
           for t in positional):
        return True
    if base == "sleep":
        seconds = _sleep_seconds(positional)
        if seconds is None or seconds > _MAX_AUTO_WAIT_SECONDS:
            return True
    if base == "ping":
        count = _option_value(args, ("-c", "--count"))
        interval = _option_value(args, ("-i", "--interval"))
        if count is None:
            return True
        try:
            total = float(count) * float(interval if interval is not None else 1)
        except ValueError:
            return True
        if total > _MAX_AUTO_WAIT_SECONDS:
            return True
    if base in ("vmstat", "iostat"):
        numbers = [t for t in positional if t.isdigit()]
        if len(numbers) == 1:
            return True
        if len(numbers) >= 2 and float(numbers[0]) * float(numbers[1]) > _MAX_AUTO_WAIT_SECONDS:
            return True
    return False


def _segment_wait_seconds(seg: ChainSegment) -> float:
    """本段的有意等待时长（sleep/ping/vmstat/iostat），用于整链求和。

    单段各自 280s 合规、四段串起来 1120s 就会越过交付超时；只看每段是漏的。
    """
    try:
        tokens = strip_prefix_wrappers(tokenize_segment(seg.raw))
    except CommandParseError:
        return 0.0
    if not tokens:
        return 0.0
    base = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    positional = [t for t in args if not t.startswith("-")]
    if base == "sleep":
        seconds = _sleep_seconds(positional)
        return seconds if seconds is not None else 0.0
    if base == "ping":
        count = _option_value(args, ("-c", "--count"))
        interval = _option_value(args, ("-i", "--interval"))
        try:
            return float(count) * float(interval if interval is not None else 1)
        except (TypeError, ValueError):
            return 0.0
    if base in ("vmstat", "iostat"):
        numbers = [t for t in positional if t.isdigit()]
        if len(numbers) >= 2:
            try:
                return float(numbers[0]) * float(numbers[1])
            except ValueError:
                return 0.0
    return 0.0


def is_provably_readonly(command: str) -> ReadonlyVerdict:
    """整条命令的只读证明（可见终端 prod 自动交付用）。

    调用方必须先用 `classify_command` 确认整条命令为 safe；本函数只回答
    「每个段是否都能证明不改状态」。解析失败 → False（fail-closed）。
    任一破坏性写 / 执行注入 / 未证明段 → False。rule_id 只取新只读规则。
    """
    try:
        segments = split_command_chain(command)
    except CommandParseError:
        return ReadonlyVerdict(False)
    if not segments:
        return ReadonlyVerdict(False)

    rule_id = ""
    wait_seconds = 0.0
    for seg in segments:
        if not classify_destructive_write(seg.raw).safe:
            return ReadonlyVerdict(False)
        if not classify_exec_injection(seg.raw).safe:
            return ReadonlyVerdict(False)
        if _blocks_terminal(seg):
            return ReadonlyVerdict(False)
        wait_seconds += _segment_wait_seconds(seg)
        if wait_seconds > _MAX_AUTO_WAIT_SECONDS:
            return ReadonlyVerdict(False)
        verdict = classify_readonly(seg.raw)
        if not verdict.safe:
            verdict = classify_legacy_readonly(seg.raw)
        if not verdict.safe:
            return ReadonlyVerdict(False)
        rule_id = rule_id or verdict.rule_id
    return ReadonlyVerdict(True, rule_id)


# ── 破坏性写守卫 ────────────────────────────────────────────────────────────

_DATE_VALUE_FLAGS = frozenset({"-d", "--date", "-f", "--file", "-r", "--reference"})
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls"})


def _date_write(tokens: list[str]) -> ReadonlyVerdict:
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in {"-s", "--set"} or t.startswith("--set=") or (
            t.startswith("-s") and not t.startswith("--")
        ):
            return _bad("rw.destructive.date_set.v1", f"date sets system clock ({t})")
        if t in _DATE_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith(("-d", "--date=", "--file=", "--reference=")) and "=" in t:
            i += 1
            continue
        if t.startswith("-") or t.startswith("+"):
            i += 1
            continue
        return _bad("rw.destructive.date_positional.v1", f"date sets system clock ({t})")
    return _SAFE


def _find_write(tokens: list[str], allowed: bool) -> ReadonlyVerdict:
    if allowed:
        return _SAFE
    for t in tokens[1:]:
        if t in _FIND_ACTIONS or t.startswith(("-fprint", "-fprintf")):
            return _bad("rw.destructive.find_action.v1",
                        f"find performs a destructive action ({t})")
    return _SAFE


def classify_destructive_write(
    segment: str,
    *,
    find_actions_allowed: bool = False,
) -> ReadonlyVerdict:
    """识别 date/find/tar/journalctl/yq 的写动作；safe=True 表示没发现破坏性写。"""
    tokens = _tokens_or_none(segment)
    if tokens is None:
        return _bad("", "unparseable segment (conservative)")
    if not tokens:
        return _SAFE
    base = tokens[0].rsplit("/", 1)[-1]
    if base == "date":
        return _date_write(tokens)
    if base == "find":
        return _find_write(tokens, find_actions_allowed)
    if base == "tar" and _tar_mode(tokens) == "write":
        return _bad("rw.destructive.tar_write.v1",
                    "tar extracts or updates archive (writes files)")
    if base == "journalctl":
        for t in tokens[1:]:
            if (t.startswith("--vacuum") or t in {"--rotate", "--flush", "--sync"}
                    or t.split("=", 1)[0] in _JOURNALCTL_EXTRA_WRITE):
                return _bad("rw.destructive.journalctl_write.v1",
                            f"journalctl log deletion/rotation ({t})")
    if base == "yq" and _yq_write_flags(tokens[1:]):
        return _bad("rw.destructive.yq_inplace.v1", "yq in-place edit writes files")
    return _SAFE


# ── 执行注入守卫 ────────────────────────────────────────────────────────────

def _git_injection(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args):
        t = args[i]
        if not t.startswith("-"):
            break
        stem = t.split("=", 1)[0]
        if stem in {"-c", "--config-env"} or _git_exec_path_opt(stem):
            return _bad("ex.inject.git_config_env.v1",
                        f"git executes configured helper ({stem})")
        if _git_transport_opt(stem):
            return _bad("ex.inject.git_transport.v1",
                        f"git transport helper injection ({stem})")
        if stem in _GIT_GLOBAL_VALUE:
            i += 1 if "=" in t else 2
        else:
            i += 1
    sub = args[i] if i < len(args) else ""
    for t in args:
        stem = t.split("=", 1)[0]
        if _git_transport_opt(stem):
            return _bad("ex.inject.git_transport.v1",
                        f"git transport helper injection ({stem})")
        if sub == "ls-remote" and not t.startswith("-") \
                and t.startswith(("ext::", "fd::", "file://")):
            return _bad("ex.inject.git_transport.v1", f"git remote helper URL ({t})")
    return _SAFE


_TAR_HOOK_LONGS = (
    "--checkpoint", "--to-command", "--rsh-command", "--info-script",
    "--new-volume-script", "--use-compress-program", "--gzip-program",
    "--bzip2-program", "--xz-program", "--zstd-program",
)
# 过滤器开关本身（`--zstd` == -z）不是外部程序 hook；缩写匹配必须放行这些
# 精确名字，否则 `tar --zstd -tvf a.tar.zst` 这类只读列表要白跑一张审批卡。
# 真正危险的是其 `-program` 形态（`--zstd-prog=`），仍由缩写分支拦截。
_TAR_FILTER_LONGS = frozenset({
    "--gzip", "--gunzip", "--ungzip", "--bzip2", "--bunzip2", "--compress",
    "--uncompress", "--xz", "--unxz", "--lzma", "--unlzma", "--zstd",
    "--lzip", "--lz4", "--no-gzip",
})


def _tar_injection(tokens: list[str]) -> ReadonlyVerdict:
    for t in tokens[1:]:
        low = t.lower()
        if low.startswith(_TAR_HOOK_LONGS):
            return _bad("ex.inject.tar_hook.v1",
                        "tar runs an external program (checkpoint/--to-command)")
        stem = low.split("=", 1)[0]
        if stem.startswith("--") and len(stem) >= 6 and stem not in _TAR_FILTER_LONGS \
                and any(opt.startswith(stem) for opt in _TAR_HOOK_LONGS):
            return _bad("ex.inject.tar_hook.v1",
                        "tar runs an external program (checkpoint/--to-command)")
        if t.startswith("-") and not t.startswith("--") and len(t) > 1 \
                and any(c in t[1:] for c in "IF"):
            return _bad("ex.inject.tar_hook.v1",
                        "tar pipes the archive through an external program")
    return _SAFE


def _openssl_injection(tokens: list[str]) -> ReadonlyVerdict:
    for t in tokens[1:]:
        if t in {"-engine", "-provider", "-provider-path"} or t.startswith(
            ("--engine", "--provider")
        ):
            return _bad("ex.inject.openssl_engine.v1",
                        f"openssl loads external engine/provider ({t})")
    return _SAFE


def _awk_injection(segment: str, tokens: list[str]) -> ReadonlyVerdict:
    if tokens[0].rsplit("/", 1)[-1] in {"awk", "gawk", "mawk", "nawk"} and (
        re.search(r"\bsystem\s*\(", segment)
        or re.search(r"\|\s*&?\s*getline\b", segment)
    ):
        return _bad("ex.inject.awk_system.v1",
                    "awk executes shell commands (system/getline)")
    return _SAFE


def classify_exec_injection(segment: str) -> ReadonlyVerdict:
    """识别把执行权交给外部程序的开关；safe=True 表示没发现注入面。"""
    tokens = _tokens_or_none(segment)
    if tokens is None:
        return _bad("", "unparseable segment (conservative)")
    if not tokens:
        return _SAFE
    base = tokens[0].rsplit("/", 1)[-1]
    if base == "git":
        return _git_injection(tokens)
    if base == "tar":
        return _tar_injection(tokens)
    if base == "openssl":
        return _openssl_injection(tokens)
    return _awk_injection(segment, tokens)
