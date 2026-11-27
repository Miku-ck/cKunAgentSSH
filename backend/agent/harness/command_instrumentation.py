"""复合命令拆段与逐段退出码插桩。"""
from __future__ import annotations

from dataclasses import dataclass

STEP_MARKER_PREFIX = "__AGENT_STEP__"
STEP_MARKER_PLACEHOLDER = "__AGENT_MARKER__"

_INSTRUMENT_BLOCKED_HEADS = frozenset({
    "if", "then", "elif", "else", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "select", "function", "in", "time",
})


@dataclass(frozen=True)
class CompoundSplit:
    """parts[i] 与前一段之间的分隔符存在 separators[i]（首段为空串）。"""
    parts: list[str]
    separators: list[str]


def split_with_separators(command: str) -> CompoundSplit:
    """引号/转义/注释感知拆段；单管道留在段内，复杂展开保守不拆。"""
    stripped = command.strip()
    if not stripped:
        return CompoundSplit([], [])
    if "$(" in command or "`" in command or "<<" in command:
        return CompoundSplit([stripped], [""])

    parts: list[str] = []
    seps: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    comment = False
    pending_sep = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if comment:
            buf.append(ch)
            if ch == "\n":
                comment = False
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "#" and (not buf or buf[-1] in " \t\n"):
            comment = True
            buf.append(ch)
            i += 1
            continue
        if ch in (";", "&", "|"):
            sep = ""
            if command.startswith("&&", i) or command.startswith("||", i):
                sep = command[i:i + 2]
            elif ch == ";":
                sep = ";"
            if sep:
                parts.append("".join(buf))
                seps.append(pending_sep)
                buf = []
                pending_sep = sep
                i += len(sep)
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    seps.append(pending_sep)

    out_parts: list[str] = []
    out_seps: list[str] = []
    for part, sep in zip(parts, seps):
        part = part.strip()
        if not part:
            continue
        out_parts.append(part)
        out_seps.append(sep)
    return CompoundSplit(out_parts, out_seps)


def split_compound_command(command: str) -> list[str]:
    """引号外的 ; / && / || 为分隔符，单管道 | 留在段内。"""
    return split_with_separators(command).parts


def _instrument_head(part: str) -> str:
    """段首命令词（跳过 ! 与环境赋值），用于识别 shell 语法关键字。"""
    for token in part.split():
        token = token.strip("{}()!;")
        if not token or ("=" in token and not token.startswith("-")):
            continue
        return token.lower()
    return ""


def build_instrumented_command(command: str) -> str:
    """只对纯 ; 链插入退出码标记；条件链和 shell 语法体回退整条执行。"""
    split = split_with_separators(command)
    parts = split.parts
    if len(parts) < 2:
        return ""
    if any(sep and sep != ";" for sep in split.separators):
        return ""
    for part in parts:
        if "#" in part or "<<" in part:
            return ""
        if part.rstrip().endswith("\\"):
            return ""
        if part.lstrip().startswith(("{", "}", "(", ")")):
            return ""
        if _instrument_head(part) in _INSTRUMENT_BLOCKED_HEADS:
            return ""
    chunks: list[str] = []
    for idx, part in enumerate(parts, 1):
        chunks.append(part)
        chunks.append(
            "printf '" + STEP_MARKER_PREFIX + " " + STEP_MARKER_PLACEHOLDER +
            ":" + str(idx) + ":%s\\n' \"$?\""
        )
    return " ; ".join(chunks)
