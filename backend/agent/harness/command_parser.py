"""命令解析 — 归一化、引号感知分段、wrapper 与执行位置工具。

本模块只负责“把命令文本变成可判定的结构”，不做任何风险分级。
`safety.py` 与 `readonly_policy.py` 共用这里的结构，避免两套切分逻辑漂移。
"""
from __future__ import annotations

import os
import re
import shlex
import unicodedata
from dataclasses import dataclass

__all__ = [
    "ChainSegment",
    "CommandParseError",
    "first_exec_verb",
    "get_normalized_for_display",
    "normalize_command_for_detection",
    "split_command_chain",
    "strip_prefix_wrappers",
    "strip_quotes",
    "tokenize_segment",
    "verb_of",
]

# ── 归一化 ──────────────────────────────────────────────────────────────────

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NULL_BYTE_RE = re.compile(r"\x00")
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([^\n])")
_EMPTY_STRING_RE = re.compile(r"(?<!\\)''|(?<!\\)\"\"")
_ANSI_C_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_WIN_PATH_FOLD_RE = re.compile(r"(?:[A-Za-z]:|~)\\[^\s]*")

_ANSI_C_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _decode_ansi_c(match: re.Match) -> str:
    s = match.group(1)
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in _ANSI_C_ESCAPES:
                out.append(_ANSI_C_ESCAPES[n])
                i += 2
                continue
            if n == "x" and i + 3 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if n in "01234567" and i + 3 < len(s):
                try:
                    out.append(chr(int(s[i + 1:i + 4], 8)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_backslash_escape(match: re.Match) -> str:
    # `\'` / `\"` 原样保留：单引号里反斜杠本来不转义，双引号里 `\"` 也不闭合引号。
    # 先吃掉这层反斜杠会让 `'it'\''s'`（awk/sed 最常见的 POSIX 写法）在引号感知
    # 分段时变成未闭合 → 整条命令 fail-closed 判 dangerous。
    ch = match.group(1)
    return match.group(0) if ch in ("'", '"') else ch


def normalize_command_for_detection(command: str) -> str:
    """命令归一化 — 抵御 ANSI / Unicode / 转义 / ANSI-C 混淆。"""
    command = _ANSI_ESCAPE_RE.sub("", command)
    command = _NULL_BYTE_RE.sub("", command)
    command = unicodedata.normalize("NFKC", command)

    home = os.path.expanduser("~")
    if home and command.startswith(home):
        command = "~" + command[len(home):]

    command = _ANSI_C_RE.sub(_decode_ansi_c, command)
    command = _WIN_PATH_FOLD_RE.sub(lambda m: m.group(0).replace("\\", "/"), command)

    while True:
        new_command = _BACKSLASH_ESCAPE_RE.sub(_strip_backslash_escape, command)
        if new_command == command:
            break
        command = new_command

    return _EMPTY_STRING_RE.sub("", command)


def get_normalized_for_display(command: str) -> str:
    """归一化后的可读形态，仅用于 UI 展示。"""
    normalized = normalize_command_for_detection(command)
    try:
        return " ".join(shlex.split(normalized, posix=True))
    except ValueError:
        return normalized.strip()


# ── 分段 ────────────────────────────────────────────────────────────────────

_QUOTE_STRIP_RE = re.compile(r"['\"]")


def strip_quotes(text: str) -> str:
    return _QUOTE_STRIP_RE.sub("", text)


class CommandParseError(ValueError):
    """命令结构无法安全解析（未闭合引号 / 括号等）→ 调用方必须 fail-closed。"""


@dataclass(frozen=True)
class ChainSegment:
    """raw = 归一化后原文（保留引号）；detection = 剥引号检测副本。"""

    raw: str
    detection: str


def split_command_chain(command: str) -> list[ChainSegment]:
    """按顶层 ; && || | & 换行切分命令，保留每段原文与剥引号检测副本。

    引号、反引号、$( ) 与括号内部的分隔符不切；未闭合则抛 CommandParseError。
    解析失败必须由调用方升级为 dangerous，不能退回“整串当一段”继续放行。
    """
    text = normalize_command_for_detection(command)
    spans: list[tuple[int, int]] = []
    start = 0
    quote = ""
    escaped = False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end < 0:
                raise CommandParseError("unclosed backtick")
            i = end + 1
            continue
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "(":
            depth += 1
            i += 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth == 0:
                raise CommandParseError("unbalanced ')'")
            depth -= 1
            i += 1
            continue
        if depth == 0:
            sep = ""
            if text.startswith("&&", i) or text.startswith("||", i):
                sep = text[i:i + 2]
            elif ch in ";|&\n":
                if ch == "&" and (
                    (i > 0 and text[i - 1] in "<>")
                    or (i + 1 < len(text) and text[i + 1] == ">")
                ):
                    i += 1
                    continue
                sep = ch
            if sep:
                spans.append((start, i))
                i += len(sep)
                start = i
                continue
        i += 1
    if quote:
        raise CommandParseError(f"unclosed quote {quote}")
    if depth:
        raise CommandParseError("unclosed '('")
    spans.append((start, len(text)))

    segments: list[ChainSegment] = []
    for a, b in spans:
        raw = text[a:b].strip()
        if not raw:
            continue
        detection = strip_quotes(raw).strip()
        if detection:
            segments.append(ChainSegment(raw=raw, detection=detection))
    return segments


# ── token / wrapper / 执行位置 ─────────────────────────────────────────────

def tokenize_segment(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc


_WRAPPERS = {"env", "nohup", "time", "sudo", "doas", "nice", "stdbuf", "xargs", "command"}

_WRAPPER_FLAGS: dict[str, dict[str, bool]] = {
    "env": {"-i": False, "--ignore-environment": False, "-0": False,
            "-u": True, "--unset": True, "-C": True, "-S": True, "-c": True},
    "nice": {"-n": True},
    "stdbuf": {"-o": True, "-e": True, "-i": True},
    "doas": {"-u": True, "-g": True, "-C": False},
    "setsid": {"-f": False, "-w": False, "-c": True},
    "command": {"-v": False, "-p": False, "-V": False},
    "xargs": {"-I": True, "--replace": True, "-i": False, "-n": True,
              "--max-args": True, "-L": True, "-P": True, "--max-procs": True,
              "-d": True, "-E": True, "-a": True, "-e": True, "-0": False,
              "-r": False, "--arg-file": True},
}

_SHELL_CONTROL_KEYWORDS = frozenset({
    "do", "then", "if", "while", "until", "for", "else", "elif", "fi", "done",
    "esac", "in", "case", "select", "function", "time", "!",
})

_TOKEN_PUNCT_RE = re.compile(r"^[^A-Za-z0-9_.\-/]+|[^A-Za-z0-9_.\-/]+$")
_ASSIGNMENT_RE = re.compile(r"[a-z_][a-z0-9_]*=\S*")


def strip_prefix_wrappers(tokens: list[str]) -> list[str]:
    """剥离 env/nohup/sudo/... 与环境赋值，返回真正执行的 token 序列。"""
    out: list[str] = list(tokens)
    changed = True
    while changed and out:
        changed = False
        t = out[0]
        base = t.rsplit("/", 1)[-1]
        if t in _WRAPPERS or base in _WRAPPERS:
            out = out[1:]
            changed = True
            if base == "timeout" and out and not out[0].startswith("-"):
                out = out[1:]
                continue
            flags = _WRAPPER_FLAGS.get(base, {})
            while out:
                f = out[0]
                if f not in flags:
                    break
                out = out[1:]
                changed = True
                if flags[f] and out:
                    out = out[1:]
        elif t and t[0].isalpha() and "=" in t and not t.startswith("-"):
            out = out[1:]
            changed = True
    return out


def verb_of(token: str) -> str:
    stripped = _TOKEN_PUNCT_RE.sub("", token).strip("/.").lower()
    return stripped.rsplit("/", 1)[-1]


def first_exec_verb(tokens: list[str]) -> tuple[str, list[str]]:
    for i, t in enumerate(tokens):
        base = verb_of(t)
        if not base or base in _SHELL_CONTROL_KEYWORDS or _ASSIGNMENT_RE.fullmatch(base):
            continue
        return base, tokens[i + 1:]
    return "", []
