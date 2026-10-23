# Agent 审批优化（只读证明 · 审批聚合 · 审计闭环）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不放宽任何写操作审批的前提下，把“已证明只读”的命令移出人工审批，按 run + 主机聚合待批卡片，并让每次自动放行与审批结论都能被审计和统计。

**Architecture:** 新增 `command_parser.py`（引号感知分段）与 `readonly_policy.py`（声明式只读 / 破坏性写 / 执行注入规则）；`safety.py` 改为输出带 `source`、`rule_id` 的 `CommandAssessment`，`classify_command()` 签名保持不变。意图识别只输出 `readonly_expected` 提示，作为卡片展示与漂移审计，绝不参与授权。审计结果追加 `detail=v1;source=...;rule=...;intent=...`，新增 `requested` 结论与 `/audit/approval-stats`。前端按 run + host 聚合卡片、展示意图漂移、run 摘要与审计概览。

**Tech Stack:** Python 3.11 / FastAPI / sqlite3 / pytest；Vue 3 / Pinia / TypeScript / Vitest。

**Spec:** `docs/superpowers/specs/2026-10-23-agent-approval-optimization-design.md`

## Global Constraints

以下约束逐字生效，任何任务都不得绕过：

- 意图识别**永不参与授权**；安全结论只由命令内容、目标主机、运行环境决定。
- 本轮**只扩大 safe 集合**，不自动放行任何写操作，不为 dangerous 新增会话级免审。
- 解析失败 / 规则未覆盖 / 不确定 → 默认审批（fail-closed）。
- prod 危险命令仅接受预登记 allowlist，不变。
- 每条只读自动放行必须有 `source`；新增只读规则还必须有非空 `rule_id` 与审计记录。
- 不收紧 `wget` 默认落盘、`curl -o/-O`、`tee`、普通重定向等既有良性写侧口径。
- 计划模式白名单保持严格子集，本轮不自动继承新只读规则。
- `/audit/append` 只接受 `executed` / `failed`；审批类结论只能服务端写入。
- **不要自行 `git commit`**；本计划所有 commit 步骤都必须在用户明确授权后才执行。
- 每个任务先写失败测试，再实现；禁止先改实现再补测试。

## File Structure

**新增（后端）**
- `backend/agent/harness/command_parser.py` — 归一化、引号感知分段、token / wrapper / 执行位置工具；唯一职责是“把命令文本变成可判定的结构”。
- `backend/agent/harness/readonly_policy.py` — 声明式只读规则、破坏性写守卫、执行注入守卫；只回答“这一段是否被证明只读 / 是否含写动作 / 是否把执行权交给外部程序”，不做整体分级。

**修改（后端）**
- `backend/agent/harness/safety.py` — 产出 `CommandAssessment`；接入只读证明；`passwd` 位置化；`tar` 列表模式进入证明流程。
- `backend/agent/harness/command_explain.py` — 新 reason 的中文后果文案。
- `backend/agent/intention.py` — `readonly_expected` 字段与只读纪律文案。
- `backend/agent/audit.py` — `requested` 结论、`detail` 编解码、`approval_stats`。
- `backend/routes/audit.py` — `/audit/append` 收窄、`/audit/approval-stats`。
- `backend/routes/agent.py` — grant / deny 审计追加 `approval_key` 等 detail。
- `backend/agent/harness/reins/guards.py` — `readonly_allowed` 事件、`requested` 审计、stash assessment。
- `backend/agent/harness/reins/gate.py` — `requested` / 过期审计 detail。
- `backend/agent/harness/reins/core.py` — 卡片 payload 的 `intent` / `intent_drift` / `approval_key` / `approval_ttl_seconds`。
- `backend/agent/harness/telemetry.py` — `_KIND_MAP` 与投影字段（spec §14 漏列，本计划补上）。

**新增（前端）**
- `src/utils/approval-groups.ts` — run + host 分组纯函数。
- `src/utils/approval-groups.test.ts` — 分组、最坏风险、漂移聚合测试。

**修改（前端）**
- `src/api/sse.ts` — 事件 union、`readonly_allowed`、审批字段、`readonly_expected`。
- `src/api/audit.ts` — `requested`、`ApprovalStats`、`approvalStats()`、收窄 append。
- `src/stores/agent.ts` — 意图字段、`readonly_allowed` 事件、runStats。
- `src/stores/agent.test.ts` — 新事件与统计测试。
- `src/components/agent/ApprovalDock.vue` — 分组渲染。
- `src/components/agent/ApprovalCard.vue` — 意图行、漂移警告、主机/TTL/哈希落款。
- `src/components/agent/AgentRunPanel.vue` — run 摘要。
- `src/views/ssh/AuditView.vue` — 概览卡与 `requested` 筛选。

**测试（后端）**
- 新增：`test_command_parser.py`、`test_readonly_policy.py`、`test_safety_assessment.py`、`test_intention_readonly_expected.py`、`test_audit_detail.py`、`test_approval_stats.py`、`test_approval_audit_detail.py`、`test_readonly_event_wire.py`。
- 修改：`test_safety_misjudgment.py`、`test_safety_relative_escape.py`、`test_ai_loop_round13c.py`、`test_audit_append_endpoint.py`、`test_command_explain.py`、`test_plan_mode.py`、`test_safety_classify_endpoint.py`、`test_trace_projection_keys_s106.py`。

## Task Map

| # | 任务 | 主要产物 |
|---|------|----------|
| 1 | 命令解析器 | `command_parser.py` + 归一化迁移 |
| 2 | 只读策略模块 | `readonly_policy.py` |
| 3 | 安全分类器改造 | `CommandAssessment` + 只读证明接入 |
| 4 | 判定消费方 | 后果文案 / shell_exec / 接口 / 计划模式 |
| 5 | 意图只读预期 | `readonly_expected` + 纪律文案 |
| 6 | 审计详情编解码 | `detail=v1` |
| 7 | `requested` 结论 | 三处清单 10→11 + append 收窄 |
| 8 | 审批 payload + 事件全链路 | intent / drift / readonly_allowed |
| 9 | 前端 store | 字段 + `runStats` |
| 10 | ApprovalDock 分组 | 纯函数 + UI |
| 11 | ApprovalCard | 意图行 / 漂移 / 落款 |
| 12 | 运行摘要 | AgentRunPanel |
| 13 | 审批审计闭环 | grant / deny / expired detail |
| 14 | 审计统计 | `approval_stats` + 接口 |
| 15 | 审计页概览 | AuditView |
| 16 | 端到端回归 | 全量测试 + 手工验收 |

---

### Task 1: 命令解析器 — 引号感知分段与归一化迁移

**Files:**
- Create: `backend/agent/harness/command_parser.py`
- Modify: `backend/agent/harness/safety.py`（删除已迁移定义，改为导入）
- Test: `backend/tests/test_command_parser.py`

**Interfaces:**
- Produces:
  - `normalize_command_for_detection(command: str) -> str`
  - `get_normalized_for_display(command: str) -> str`
  - `strip_quotes(text: str) -> str`
  - `class CommandParseError(ValueError)`
  - `@dataclass(frozen=True) class ChainSegment: raw: str; detection: str`
  - `split_command_chain(command: str) -> list[ChainSegment]`
  - `tokenize_segment(segment: str) -> list[str]`
  - `strip_prefix_wrappers(tokens: list[str]) -> list[str]`
  - `verb_of(token: str) -> str`
  - `first_exec_verb(tokens: list[str]) -> tuple[str, list[str]]`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_command_parser.py`：

```python
"""命令解析器：归一化迁移 + 引号感知分段 + wrapper / 执行位置工具。"""
import pytest

from agent.harness import command_parser as cp
from agent.harness import safety


def _raws(command: str) -> list[str]:
    return [seg.raw for seg in cp.split_command_chain(command)]


def test_归一化与展示函数已从_safety_迁出且仍可回导():
    cmd = r"rm$'\x2f' -rf $'\x2f'"
    assert cp.normalize_command_for_detection(cmd) == "rm/ -rf /"
    assert safety.normalize_command_for_detection(cmd) == "rm/ -rf /"
    assert cp.get_normalized_for_display("rm   -rf   /tmp/x ") == "rm -rf /tmp/x"
    assert safety.get_normalized_for_display is cp.get_normalized_for_display


def test_引号内的分隔符不切段():
    assert _raws("echo 'a;b' && rm -rf /tmp/x") == ["echo 'a;b'", "rm -rf /tmp/x"]
    assert _raws('printf "%s|%s" a b | cat') == ['printf "%s|%s" a b', "cat"]


def test_子shell内部的分隔符不切外层段():
    segs = cp.split_command_chain("echo $(date; id) && uptime")
    assert [s.raw for s in segs] == ["echo $(date; id)", "uptime"]


def test_fd复制与重定向保护():
    assert _raws("ps aux 2>&1 | grep nginx") == ["ps aux 2>&1", "grep nginx"]
    assert _raws("cmd &> /tmp/out") == ["cmd &> /tmp/out"]


def test_detection_剥引号但raw保留原文():
    seg = cp.split_command_chain('rm -rf "/"')[0]
    assert seg.raw == 'rm -rf "/"'
    assert seg.detection == "rm -rf /"


@pytest.mark.parametrize("cmd", ["echo 'unclosed", "echo $(date", "echo )"])
def test_未闭合引号或括号抛解析错误(cmd: str):
    with pytest.raises(cp.CommandParseError):
        cp.split_command_chain(cmd)


def test_wrapper与环境赋值剥离():
    assert cp.strip_prefix_wrappers(
        ["env", "FOO=1", "nice", "-n", "5", "jq", "."]
    ) == ["jq", "."]
    assert cp.strip_prefix_wrappers(["sudo", "id"]) == ["id"]


def test_动词与执行位置():
    assert cp.verb_of("/usr/bin/rm/") == "rm"
    assert cp.first_exec_verb(["then", "reboot", "-f"]) == ("reboot", ["-f"])
    assert cp.tokenize_segment("echo 'a b'") == ["echo", "a b"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_command_parser.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'agent.harness.command_parser'`。

- [ ] **Step 3: 创建 `command_parser.py`**

```python
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
_EMPTY_STRING_RE = re.compile(r"''|\"\"")
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
        new_command = _BACKSLASH_ESCAPE_RE.sub(r"\1", command)
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
```

- [ ] **Step 4: 从 `safety.py` 删除已迁移定义并改为导入**

在 `safety.py` 顶部新增：

```python
from .command_parser import (
    ChainSegment,
    CommandParseError,
    first_exec_verb,
    get_normalized_for_display,
    normalize_command_for_detection,
    split_command_chain,
    strip_prefix_wrappers,
    strip_quotes,
    tokenize_segment,
    verb_of,
)

_verb_of = verb_of
_first_exec_verb = first_exec_verb
_strip_prefix_wrappers = strip_prefix_wrappers
```

删除 `safety.py` 中这些定义（函数/常量整块删除，不再保留第二份）：

- `_ANSI_ESCAPE_RE`、`_NULL_BYTE_RE`、`_BACKSLASH_ESCAPE_RE`、`_EMPTY_STRING_RE`、`_ANSI_C_RE`、`_WIN_PATH_FOLD_RE`、`_ANSI_C_ESCAPES`、`_decode_ansi_c`、`get_normalized_for_display`、`normalize_command_for_detection`
- `_QUOTE_STRIP_RE`、`_COMMAND_CHAIN_SPLIT_RE`
- `_WRAPPER_FLAGS`、`_SHELL_CONTROL_KEYWORDS`、`_TOKEN_PUNCT_RE`、`_ASSIGNMENT_RE`、`_verb_of`、`_first_exec_verb`、`_strip_prefix_wrappers`
- `_WRAPPERS`（解析器已持有；safety 不再直接使用）

保留并改写 `_detection_copy` 与 `_split_command_chain`：

```python
def _detection_copy(command: str) -> str:
    return strip_quotes(normalize_command_for_detection(command))


_FALLBACK_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||(?<!>)&|\n)\s*")


def _split_command_chain(text: str) -> list[str]:
    """兼容旧调用：返回 detection 串。解析失败时退回旧正则，仅用于写目标/
    下载执行的辅助扫描；主分类路径必须直接调用 split_command_chain() 并 fail-closed。"""
    try:
        return [seg.detection for seg in split_command_chain(text)]
    except CommandParseError:
        return [s.strip() for s in _FALLBACK_CHAIN_SPLIT_RE.split(text) if s.strip()]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_command_parser.py -v`

Expected: PASS。

- [ ] **Step 6: 回归现有 safety 测试**

Run: `cd backend && python -m pytest tests/test_safety_misjudgment.py tests/test_safety_default_approval.py tests/test_safety_relative_escape.py -q`

Expected: PASS（此步只搬家不改口径；若有差异必须先定位，不得放宽断言）。

- [ ] **Step 7: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/harness/command_parser.py backend/agent/harness/safety.py backend/tests/test_command_parser.py
git commit -m "refactor(agent): 抽出引号感知命令解析器"
```

---

### Task 2: 只读策略模块 — 规则表、破坏性写守卫、执行注入守卫

**Files:**
- Create: `backend/agent/harness/readonly_policy.py`
- Test: `backend/tests/test_readonly_policy.py`

**Interfaces:**
- Consumes: Task 1 的 `CommandParseError`、`tokenize_segment`、`strip_prefix_wrappers`。
- Produces:
  - `@dataclass(frozen=True) class ReadonlyVerdict: safe: bool; rule_id: str = ""; reason: str | None = None`
  - `classify_readonly(segment: str) -> ReadonlyVerdict` — `safe=True` 表示“这一段被证明只读”。
  - `classify_destructive_write(segment: str, *, find_actions_allowed: bool = False) -> ReadonlyVerdict` — `safe=False` 表示“发现写/删除动作”，`reason` 是给审计与卡片的中文可解释英文短句。
  - `classify_exec_injection(segment: str) -> ReadonlyVerdict` — `safe=False` 表示“命令把执行权交给外部程序”。
  - `tar_list_mode(tokens: list[str]) -> bool`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_readonly_policy.py`：

```python
"""只读证明 / 破坏性写 / 执行注入三条判定链的规则级测试。"""
import pytest

from agent.harness.readonly_policy import (
    classify_destructive_write,
    classify_exec_injection,
    classify_readonly,
    tar_list_mode,
)
from agent.harness.command_parser import tokenize_segment

READONLY_CASES = [
    ("jq '.items[] | .name' app.json", "ro.verb.jq.v1"),
    ("jq -n '{a: 1}'", "ro.verb.jq.v1"),
    ("yq '.spec.replicas' deploy.yaml", "ro.verb.yq.v1"),
    ("getent passwd", "ro.sub.getent.v1"),
    ("getent group wheel", "ro.sub.getent.v1"),
    ("dig +short example.com A", "ro.verb.dig.v1"),
    ("nslookup example.com", "ro.verb.nslookup.v1"),
    ("host example.com", "ro.verb.host.v1"),
    ("ping -c 3 10.0.0.1", "ro.verb.ping.v1"),
    ("traceroute -n 10.0.0.1", "ro.verb.traceroute.v1"),
    ("lsblk -f", "ro.verb.lsblk.v1"),
    ("blkid /dev/sda1", "ro.verb.blkid.v1"),
    ("findmnt /", "ro.verb.findmnt.v1"),
    ("dmidecode -t system", "ro.verb.dmidecode.v1"),
    ("smartctl -a /dev/sda", "ro.flag.smartctl.v1"),
    ("ethtool eth0", "ro.flag.ethtool.v1"),
    ("ethtool -i eth0", "ro.flag.ethtool.v1"),
    ("tc qdisc show dev eth0", "ro.sub.tc.v1"),
    ("nft list ruleset", "ro.sub.nft.v1"),
    ("iptables -t nat -L -n -v", "ro.flag.iptables.v1"),
    ("iptables -S", "ro.flag.iptables.v1"),
    ("docker ps --format '{{.Names}}'", "ro.sub.docker.v1"),
    ("docker logs -n 100 web", "ro.sub.docker.v1"),
    ("docker stats --no-stream", "ro.sub.docker.v1"),
    ("docker system df", "ro.sub.docker.v1"),
    ("docker compose -f docker-compose.yml ps", "ro.sub.docker_compose.v1"),
    ("kubectl -n prod rollout status deploy/web", "ro.sub.kubectl.v1"),
    ("kubectl auth can-i get pods", "ro.sub.kubectl.v1"),
    ("helm -n prod show values app", "ro.sub.helm.v1"),
    ("unzip -l app.zip", "ro.flag.unzip.v1"),
    ("zipinfo app.zip", "ro.verb.zipinfo.v1"),
    ("openssl x509 -in cert.pem -noout -text", "ro.flag.openssl_x509.v1"),
    ("tar -tzf app.tgz", "ro.flag.tar_list.v1"),
    ("git ls-remote origin", "ro.sub.git_ls_remote.v1"),
    ("git config --get user.name", "ro.flag.git_config.v1"),
    ("systemctl list-dependencies nginx", "ro.sub.systemctl.v1"),
    ("crontab -l", "ro.flag.crontab.v1"),
    ("at -c 7", "ro.flag.at.v1"),
    ("ssh -V", "ro.verb.ssh_version.v1"),
    ("date +%Y-%m-%d", "ro.verb.date.v1"),
    ("find /var/log -name '*.log' -mtime +7", "ro.verb.find.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", READONLY_CASES)
def test_只读证明命中且_rule_id_非空(cmd: str, rule_id: str):
    verdict = classify_readonly(cmd)
    assert verdict.safe is True, f"{cmd!r} 应被证明只读"
    assert verdict.rule_id == rule_id, f"{cmd!r} 命中了错误规则 {verdict.rule_id!r}"


UNSAFE_READONLY_CASES = [
    "jq -i '.a=1' app.json",
    "yq -i '.a=1' deploy.yaml",
    "getent shadow",
    "ping -f 10.0.0.1",
    "blkid -w /tmp/cache /dev/sda",
    "dmidecode --dump-bin /tmp/d.bin",
    "smartctl -t short /dev/sda",
    "ethtool -s eth0 speed 100",
    "tc qdisc add dev eth0 root pfifo",
    "nft add rule inet filter input drop",
    "iptables -F",
    "iptables -L -Z",
    "docker compose up -d",
    "docker stats",
    "kubectl rollout restart deploy/web",
    "helm upgrade app chart",
    "unzip app.zip",
    "openssl req -new -key k -out r.csr",
    "tar -xzf app.tgz",
    "git ls-remote --upload-pack='sh -c id' origin",
    "git config user.name Neo",
    "systemctl restart nginx",
    "crontab -r",
    "at -f /tmp/job now",
    "ssh root@10.0.0.1",
    "jq '.x' > /tmp/out",
    'jq "$(id)"',
    "env jq .",
    "echo hi",
    "make -j8",
]


@pytest.mark.parametrize("cmd", UNSAFE_READONLY_CASES)
def test_只读证明对写动作_注入_未知命令拒绝(cmd: str):
    assert classify_readonly(cmd).safe is False, f"{cmd!r} 不得被判只读"


DESTRUCTIVE_CASES = [
    ("date -s '2020-01-01'", "rw.destructive.date_set.v1"),
    ("date 202001010000", "rw.destructive.date_positional.v1"),
    ("find /tmp -delete", "rw.destructive.find_action.v1"),
    ("find /tmp -exec rm {} +", "rw.destructive.find_action.v1"),
    ("find /tmp -fprint /tmp/out", "rw.destructive.find_action.v1"),
    ("tar -xzf app.tgz", "rw.destructive.tar_write.v1"),
    ("tar --delete -f app.tar old.txt", "rw.destructive.tar_write.v1"),
    ("journalctl --rotate", "rw.destructive.journalctl_write.v1"),
    ("journalctl --vacuum-time=1s", "rw.destructive.journalctl_write.v1"),
    ("yq -i '.a=1' deploy.yaml", "rw.destructive.yq_inplace.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", DESTRUCTIVE_CASES)
def test_破坏性写守卫(cmd: str, rule_id: str):
    verdict = classify_destructive_write(cmd)
    assert verdict.safe is False
    assert verdict.rule_id == rule_id


@pytest.mark.parametrize("cmd", [
    "date +%s",
    "date -u +%Y-%m-%dT%H:%M:%SZ",
    "date -d yesterday +%F",
    "find /var/log -name '*.log' -mtime +7",
    "tar -czf /tmp/backup.tgz /home/user/project",
    "tar -tzf app.tgz",
    "journalctl -u nginx -n 50",
    "yq '.a' deploy.yaml",
])
def test_破坏性写守卫不误伤纯只读(cmd: str):
    assert classify_destructive_write(cmd).safe is True


def test_托管日志目录的_find_delete_可豁免():
    log_dir = "/home/admin-pl/.trae-cn-server/manager-logs"
    cmd = f"find {log_dir} -name '*.log' -mtime +7 -delete"
    assert classify_destructive_write(cmd).safe is False
    assert classify_destructive_write(cmd, find_actions_allowed=True).safe is True


EXEC_INJECTION_CASES = [
    ("git -c core.pager=sh status", "ex.inject.git_config_env.v1"),
    ("git --exec-path=/tmp status", "ex.inject.git_config_env.v1"),
    ("git log --upload-pack='sh -c id'", "ex.inject.git_transport.v1"),
    ("git ls-remote ext::sh origin", "ex.inject.git_transport.v1"),
    ("tar -I 'sh -c' -cf /tmp/x.tar /tmp/data", "ex.inject.tar_hook.v1"),
    ("tar --to-command=sh -xf app.tar", "ex.inject.tar_hook.v1"),
    ("openssl x509 -engine dynamic -in c.pem -noout", "ex.inject.openssl_engine.v1"),
    ("awk '{system(\"id\")}' /tmp/x", "ex.inject.awk_system.v1"),
]


@pytest.mark.parametrize("cmd,rule_id", EXEC_INJECTION_CASES)
def test_执行注入守卫(cmd: str, rule_id: str):
    verdict = classify_exec_injection(cmd)
    assert verdict.safe is False
    assert verdict.rule_id == rule_id


@pytest.mark.parametrize("cmd", [
    "git log -c --oneline",
    "git config --get core.pager",
    "tar -czf /tmp/x.tgz /tmp/data",
    "openssl x509 -in c.pem -noout",
    "awk '{print $1}' /tmp/x",
])
def test_执行注入守卫不误伤正常用法(cmd: str):
    assert classify_exec_injection(cmd).safe is True


def test_tar_模式判定():
    assert tar_list_mode(tokenize_segment("tar -tzf app.tgz")) is True
    assert tar_list_mode(tokenize_segment("tar --list -f app.tar")) is True
    assert tar_list_mode(tokenize_segment("tar -czf app.tgz dir")) is False
    assert tar_list_mode(tokenize_segment("tar -xzf app.tgz")) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_readonly_policy.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'agent.harness.readonly_policy'`。

- [ ] **Step 3: 实现 `readonly_policy.py`**

```python
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
    CommandParseError,
    strip_prefix_wrappers,
    tokenize_segment,
)

__all__ = [
    "ReadonlyVerdict",
    "classify_destructive_write",
    "classify_exec_injection",
    "classify_readonly",
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
    """未加单引号的 $() / ${} / 反引号 → 存在命令替换（双引号内仍然生效）。"""
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
    "yq": frozenset({"-i", "--inplace", "--in-place"}),
    "blkid": frozenset({"-w", "--write-cache"}),
    "dmidecode": frozenset({"--dump-bin"}),
    "openssl": frozenset({"-out", "-keyout", "-signkey", "-CAcreateserial", "-CAserial"}),
}


def _flag_hit(args: list[str], flags: frozenset[str]) -> bool:
    for t in args:
        if t in flags:
            return True
        if t.startswith("--") and t.split("=", 1)[0] in flags:
            return True
    return False


def _classify_simple(base: str, tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    if _flag_hit(args, _WRITE_FLAGS.get(base, frozenset())):
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
    "-e", "--eeprom-dump",
})


def _classify_ethtool(tokens: list[str]) -> ReadonlyVerdict:
    if any(t in _ETHTOOL_WRITE for t in tokens[1:]):
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
    "--table", "--line-numbers", "--wait", "--modprobe",
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


def _classify_git(tokens: list[str]) -> ReadonlyVerdict:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        t = args[i]
        stem = t.split("=", 1)[0]
        if stem in {"-c", "--config-env", "--exec-path"}:
            return ReadonlyVerdict(False)
        if stem in _GIT_GLOBAL_VALUE:
            i += 1 if "=" in t else 2
        else:
            i += 1
    if i >= len(args):
        return ReadonlyVerdict(False)
    sub, rest = args[i], args[i + 1:]
    if any(t.split("=", 1)[0] in {"--upload-pack", "--receive-pack"} for t in rest):
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
            if t.startswith("--vacuum") or t in {"--rotate", "--flush", "--sync"}:
                return _bad("rw.destructive.journalctl_write.v1",
                            f"journalctl log deletion/rotation ({t})")
    if base == "yq" and _flag_hit(tokens[1:], _WRITE_FLAGS["yq"]):
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
        if stem in {"-c", "--config-env", "--exec-path"}:
            return _bad("ex.inject.git_config_env.v1",
                        f"git executes configured helper ({stem})")
        if stem in {"--upload-pack", "--receive-pack"}:
            return _bad("ex.inject.git_transport.v1",
                        f"git transport helper injection ({stem})")
        if stem in _GIT_GLOBAL_VALUE:
            i += 1 if "=" in t else 2
        else:
            i += 1
    sub = args[i] if i < len(args) else ""
    for t in args:
        stem = t.split("=", 1)[0]
        if stem in {"--upload-pack", "--receive-pack"}:
            return _bad("ex.inject.git_transport.v1",
                        f"git transport helper injection ({stem})")
        if sub == "ls-remote" and not t.startswith("-") \
                and t.startswith(("ext::", "fd::", "file://")):
            return _bad("ex.inject.git_transport.v1", f"git remote helper URL ({t})")
    return _SAFE


def _tar_injection(tokens: list[str]) -> ReadonlyVerdict:
    for t in tokens[1:]:
        low = t.lower()
        if low.startswith(("--checkpoint", "--to-command", "--rsh-command")):
            return _bad("ex.inject.tar_hook.v1",
                        "tar runs an external program (checkpoint/--to-command)")
        if t == "-I" or low.startswith((
            "--use-compress-program", "--gzip-program", "--bzip2-program",
            "--xz-program", "--zstd-program",
        )):
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_readonly_policy.py -v`

Expected: PASS。若某条规则与测试期望不符，先修规则，不放宽测试（测试表达的是 spec §6.4/§6.5 的安全口径）。

- [ ] **Step 5: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/harness/readonly_policy.py backend/tests/test_readonly_policy.py
git commit -m "feat(agent): 增加声明式只读证明与执行注入规则"
```

---

### Task 3: 安全分类器改造 — CommandAssessment、passwd 位置修复、只读证明接入

**Files:**
- Modify: `backend/agent/harness/safety.py`
- Test: `backend/tests/test_safety_assessment.py`（新增）
- Modify: `backend/tests/test_safety_misjudgment.py`、`backend/tests/test_safety_relative_escape.py`

**Interfaces:**
- Consumes: Task 2 的 `classify_readonly`、`classify_destructive_write`、`classify_exec_injection`、`tar_list_mode`。
- Produces:
  - `@dataclass(frozen=True) class CommandAssessment: level: str; reason: str | None; source: str; rule_id: str = ""; normalized: str = ""`
  - `CommandAssessment.as_pair() -> tuple[str, str | None]`
  - `classify_command_detailed(command: str, apply_default_approval: bool = True) -> CommandAssessment`
  - `classify_command(command, apply_default_approval=True)` 行为不变，返回 pair。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_safety_assessment.py`：

```python
"""CommandAssessment：分级之外的 source / rule_id 契约，以及本轮新增判定。"""
from agent.harness.safety import classify_command, classify_command_detailed


def _a(cmd: str, **kw):
    return classify_command_detailed(cmd, **kw)


def test_未知命令仍走默认审批且_source_诚实():
    a = _a("mycohortool --deploy")
    assert (a.level, a.source, a.rule_id) == ("dangerous", "default_approval", "")
    assert a.reason == "unknown command, default approval (mycohortool)"


def test_只读规则命中_readonly_rule_并带_rule_id():
    a = _a("jq '.a' app.json")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.verb.jq.v1")
    assert a.reason is None


def test_既有白名单保持_legacy_safe_verb_且不冒领_rule_id():
    for cmd in ("ls -la /tmp", "date +%s", "find /tmp -name '*.log'", "tar -czf /tmp/x.tgz /tmp/data"):
        a = _a(cmd)
        assert a.level == "safe", cmd
        assert a.source == "legacy_safe_verb", cmd
        assert a.rule_id == "", cmd


def test_getent_passwd_不再被密码规则误伤():
    a = _a("getent passwd")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.sub.getent.v1")


def test_passwd_仍按执行位置判密码修改():
    a = _a("passwd root")
    assert a.level == "dangerous"
    assert a.reason == "password change (passwd)"
    assert a.source == "exec_position"


def test_date_set_与_find_delete_进入破坏性写守卫():
    a = _a("date -s '2020-01-01'")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "destructive_write", "rw.destructive.date_set.v1")
    a = _a("find . -name '*.log' -mtime +7 -delete")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "destructive_write", "rw.destructive.find_action.v1")


def test_tar_解压危险_列表走只读证明_创建保持旧口径():
    a = _a("tar -xzf app.tgz -C /srv")
    assert (a.level, a.rule_id) == ("dangerous", "rw.destructive.tar_write.v1")
    a = _a("tar -tzf app.tgz")
    assert (a.level, a.source, a.rule_id) == ("safe", "readonly_rule", "ro.flag.tar_list.v1")
    a = _a("tar -czf /tmp/x.tgz /tmp/data")
    assert (a.level, a.source) == ("safe", "legacy_safe_verb")


def test_git_执行注入与_awk_system_在旧白名单之前拦截():
    a = _a("git -c core.pager=sh status")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "exec_injection", "ex.inject.git_config_env.v1")
    a = _a("awk '{system(\"id\")}' /tmp/x")
    assert (a.level, a.source, a.rule_id) == (
        "dangerous", "exec_injection", "ex.inject.awk_system.v1")


def test_托管日志目录_find_delete_豁免仍为_safe():
    cmd = "find /home/admin-pl/.trae-cn-server/manager-logs -name '*.log' -mtime +7 -delete"
    a = _a(cmd)
    assert (a.level, a.source) == ("safe", "agent_log_exemption")


def test_构造模板口径不套默认审批_但结构守卫仍生效():
    a = _a("mycohortool --deploy", apply_default_approval=False)
    assert (a.level, a.source) == ("safe", "constructed_no_default")
    a = _a("tar -xzf app.tgz", apply_default_approval=False)
    assert (a.level, a.source) == ("dangerous", "destructive_write")


def test_未闭合引号整串_fail_closed():
    a = _a("echo 'unclosed")
    assert a.level == "dangerous"
    assert a.source == "unparseable"


def test_classify_command_仍返回二元组():
    assert classify_command("jq '.a' app.json") == ("safe", None)
    assert classify_command("date -s '2020-01-01'")[0] == "dangerous"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_safety_assessment.py -v`

Expected: FAIL — `ImportError: cannot import name 'classify_command_detailed'`。

- [ ] **Step 3: 在 `safety.py` 增加评估类型与导入**

顶部新增：

```python
from dataclasses import dataclass

from .readonly_policy import (
    classify_destructive_write,
    classify_exec_injection,
    classify_readonly,
    tar_list_mode,
)
```

`__all__` 增加：

```python
    "CommandAssessment",
    "classify_command_detailed",
```

在 `_SEVERITY` 下方新增：

```python
@dataclass(frozen=True)
class CommandAssessment:
    """一次命令判定的完整结论：级别 + 原因 + 证据来源 + 规则编号。

    source 是审计“为什么放行/拦截”的唯一依据；rule_id 只在新只读规则命中时有值。
    normalized 仅供展示与审计，不参与任何授权。
    """
    level: str
    reason: str | None
    source: str
    rule_id: str = ""
    normalized: str = ""

    def as_pair(self) -> tuple[str, str | None]:
        return (self.level, self.reason)
```

- [ ] **Step 4: 修复 `passwd` 位置判定**

在 `_EXEC_POSITION_REASONS` 的 frozenset 中追加：

```python
    # getent passwd 是只读查询；只有执行位置上的 passwd 才是改密
    "password change (passwd)",
```

在 `_classify_exec_position()` 的 `dd` 判断之后、`init/telinit` 判断之前追加：

```python
    if real == "passwd":
        return ("dangerous", "password change (passwd)")
```

- [ ] **Step 5: 让 `tar` 列表模式进入只读证明**

把 `_classify_unknown_default()` 中 `if base == "tar":` 分支整体替换为：

```python
    if base == "tar":
        # 敏感源 / 外部程序钩子已由 _classify_write_targets 判过。
        # 列表模式不是旧白名单能证明的只读形态：先降为 dangerous fallback，
        # 交给 readonly_policy 证明并带 rule_id；创建/无模式保持旧 safe 口径。
        if tar_list_mode(tokens):
            return ("dangerous", "tar list mode (readonly candidate)")
        return ("safe", None)
```

- [ ] **Step 6: 用 `_assess_segment` 替换 `_classify_segment`**

删除 `_classify_segment`，按下列顺序实现（保留原有每一条规则的检测顺序）：

```python
def _assess_segment(segment: ChainSegment, apply_default_approval: bool = True) -> CommandAssessment:
    """单段完整检测：既有规则 → 执行注入/破坏性写守卫 → 默认审批 → 只读证明。"""
    detection = segment.detection
    raw = segment.raw
    best = CommandAssessment("safe", None, "default")

    def _upd(level: str, reason: str | None, source: str, rule_id: str = "") -> None:
        nonlocal best
        if reason and _SEVERITY[level] > _SEVERITY[best.level]:
            best = CommandAssessment(level, reason, source, rule_id)

    # 1. 既有正则表（电源动词/sudo/su/passwd 交执行位置判定）
    for pattern, reason in HARDLINE_PATTERNS:
        if reason in _EXEC_POSITION_REASONS:
            continue
        if pattern.search(detection):
            return CommandAssessment("hardline", reason, "hardline_pattern")
    for pattern, reason in DANGEROUS_PATTERNS:
        if reason in _EXEC_POSITION_REASONS:
            continue
        if pattern.search(detection):
            _upd("dangerous", reason, "dangerous_pattern")
    _upd(*_classify_exec_position(detection), "exec_position")

    # 2. 间接展开
    if _DESTRUCTIVE_VERB_RE.search(detection) and _INDIRECT_EXPANSION_RE.search(detection):
        _upd("dangerous", "destructive command with indirect expansion", "indirect_expansion")

    # 3. token 级 / 子 shell / 解释器 / 外传 / 凭据读取
    level, reason = _token_rm_find_analysis(detection)
    if level != "safe":
        _upd(level, reason, "token_analysis")
    level, reason = _classify_subshells(detection)
    if level != "safe":
        _upd(level, reason, "subshell")
    level, reason = _classify_interpreter(detection)
    if level != "safe":
        _upd(level, reason, "interpreter")
    level, reason = _classify_exfiltration(detection)
    if level != "safe":
        _upd(level, reason, "exfiltration")
    level, reason = _classify_secret_read(detection)
    if level != "safe":
        _upd(level, reason, "secret_read")

    # 4. 执行注入守卫（token 感知，先于旧白名单：awk system/git -c 等旧白名单漏洞）
    injection = classify_exec_injection(raw)
    if not injection.safe:
        _upd("dangerous", injection.reason, "exec_injection", injection.rule_id)

    # 5. 破坏性写守卫（date/find/tar/journalctl/yq）
    exempt = _agent_log_cleanup_exempt(detection)
    destructive = classify_destructive_write(raw, find_actions_allowed=exempt)
    if not destructive.safe:
        _upd("dangerous", destructive.reason, "destructive_write", destructive.rule_id)

    if best.level != "safe":
        return best

    # 6. 构造模板口径：不套默认审批，也不尝试只读证明
    if not apply_default_approval:
        return CommandAssessment("safe", None, "constructed_no_default")

    # 7. 托管日志目录豁免：写规则点已按目标放行，默认审批必须在最后一环同样放行
    if exempt:
        return CommandAssessment("safe", None, "agent_log_exemption")

    # 8. 旧白名单（legacy）优先；命中 safe 时不进入证明，避免 legacy 动词冒领 rule_id
    level, reason = _classify_unknown_default(detection)
    if level == "safe":
        return CommandAssessment("safe", None, "legacy_safe_verb")

    # 9. 旧表没证明为 safe → 只读证明；失败则保留默认审批结论
    proof = classify_readonly(raw)
    if proof.safe:
        return CommandAssessment("safe", None, "readonly_rule", proof.rule_id)
    return CommandAssessment(level, reason, "default_approval")
```

- [ ] **Step 7: 重写主检测函数**

在 `classify_command_detailed()` 中按下列顺序实现，并让 `classify_command()` 委托：

```python
def classify_command_detailed(
    command: str,
    apply_default_approval: bool = True,
) -> CommandAssessment:
    """带证据来源的命令判定。classify_command() 是它的二元组视图。"""
    blocked, reason = check_sudo_stdin_guard(command)
    if blocked:
        return CommandAssessment("hardline", reason, "hardline_pattern",
                                 normalized=normalize_command_for_detection(command))

    detection = _detection_copy(command)
    normalized = normalize_command_for_detection(command)
    best = CommandAssessment("safe", None, "default", normalized=normalized)

    def _upd(level: str, reason: str | None, source: str, rule_id: str = "") -> None:
        nonlocal best
        if reason and _SEVERITY[level] > _SEVERITY[best.level]:
            best = CommandAssessment(level, reason, source, rule_id, normalized)

    # 全串正则表
    for pattern, hreason in HARDLINE_PATTERNS:
        if hreason in _EXEC_POSITION_REASONS:
            continue
        if pattern.search(detection):
            return CommandAssessment("hardline", hreason, "hardline_pattern",
                                     normalized=normalized)
    for pattern, dreason in DANGEROUS_PATTERNS:
        if dreason in _EXEC_POSITION_REASONS:
            continue
        if pattern.search(detection):
            _upd("dangerous", dreason, "dangerous_pattern")

    # 全串写目标扫描
    level, reason = _classify_write_targets(detection)
    if level != "safe":
        _upd(level, reason, "write_target")
    if best.level == "hardline":
        return best

    # 引号感知分段：结构不可解析 → fail-closed
    try:
        segments = split_command_chain(command)
    except CommandParseError as exc:
        return CommandAssessment(
            "dangerous",
            f"unparseable command chain ({exc}; conservative)",
            "unparseable",
            normalized=normalized,
        )
    if not segments and detection.strip():
        segments = [ChainSegment(raw=command, detection=detection)]

    safe_segments: list[CommandAssessment] = []
    for segment in segments:
        assessment = _assess_segment(segment, apply_default_approval)
        if assessment.level == "safe":
            safe_segments.append(assessment)
        elif _SEVERITY[assessment.level] > _SEVERITY[best.level]:
            best = CommandAssessment(
                assessment.level, assessment.reason, assessment.source,
                assessment.rule_id, normalized,
            )
        if best.level == "hardline":
            return best

    # 全串 safe 时补回证据来源：起始值就是 safe，若只按「更严重才覆盖」，
    # 单段 jq 的 readonly_rule / 单段 ls 的 legacy_safe_verb 都会被丢成 "default"。
    # 多段命令只有**每一段**都由只读规则证明才允许整串标 readonly_rule；混合来源
    # 标 mixed_safe —— 审计里 source=readonly_rule 是「没有改状态」的证据，不能拿
    # 部分证明冒领，也不能把它降级成 legacy 后反过来吞掉真实规则。
    if best.level == "safe" and safe_segments:
        if len(safe_segments) == 1:
            seg = safe_segments[0]
            best = CommandAssessment(
                seg.level, seg.reason, seg.source, seg.rule_id, normalized)
        elif all(a.source == "readonly_rule" for a in safe_segments):
            best = CommandAssessment(
                "safe", None, "readonly_rule", safe_segments[0].rule_id, normalized)
        else:
            best = CommandAssessment("safe", None, "mixed_safe", "", normalized)

    # 跨段下载执行
    level, reason = _classify_download_exec([s.raw for s in segments])
    if level != "safe":
        _upd(level, reason, "download_exec")
    return best


def classify_command(command: str, apply_default_approval: bool = True) -> tuple[str, str | None]:
    """分类命令风险等级（签名与返回口径保持不变）。"""
    return classify_command_detailed(command, apply_default_approval).as_pair()
```

注意：`classify_constructed_command()` 保持 `classify_command(command, apply_default_approval=False)` 不变；结构守卫（破坏性写 / 执行注入）在 `apply_default_approval=False` 时仍生效。`mixed_safe` 只出现在多段全 safe 且证据来源混合时；它不是只读证明，不发 `readonly_allowed` 事件，也不计入 `readonly_auto`。

- [ ] **Step 8: 更新与本轮口径冲突的旧测试**

`backend/tests/test_safety_misjudgment.py` 的 `BENIGN_G` 列表中：

- 删除 `"find . -name '*.log' -mtime +7 -delete"` 与 `"tar xzf app.tgz -C /srv"`；
- 追加 `"find . -name '*.log' -mtime +7"` 与 `"tar -tzf app.tgz"`。

`backend/tests/test_safety_relative_escape.py` 的目录内清理参数化列表中，把 `"find . -name '*.log' -mtime +7 -delete"` 替换为 `"find . -name '*.log' -mtime +7"`，并在文件末尾追加：

```python
def test_目录内_find_delete_升级为审批() -> None:
    """find -delete 是删除动作，不再因为目标在 cwd 内就免审。"""
    level, reason = classify_command("find . -name '*.log' -mtime +7 -delete")
    assert level == "dangerous"
    assert "destructive" in (reason or "")
```

不要改动 `backend/tests/test_safety_default_approval.py` 中托管日志目录 `find -delete` 的用例；它必须继续为 safe（`agent_log_exemption`）。

- [ ] **Step 9: 运行新增测试**

Run: `cd backend && python -m pytest tests/test_safety_assessment.py -v`

Expected: PASS。

- [ ] **Step 10: 跑全量 safety 回归并处理口径升级**

Run: `cd backend && python -m pytest tests/test_safety*.py tests/test_delivery_explain.py -q`

Expected: PASS。若还有旧用例断言 `tar -x/-xzf/-xf` 或 `find -delete` 为 safe，按本轮 spec 改为 dangerous；不得为了过测试放宽分类器。

- [ ] **Step 11: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/harness/safety.py backend/tests/test_safety_assessment.py \
  backend/tests/test_safety_misjudgment.py backend/tests/test_safety_relative_escape.py
git commit -m "feat(agent): 命令判定携带来源并接入只读证明"
```

---

### Task 4: 判定消费方 — 后果文案、shell_exec、接口、计划模式回归

**Files:**
- Modify: `backend/agent/harness/command_explain.py`
- Modify: `backend/tests/test_command_explain.py`
- Modify: `backend/tests/test_visible_delivery.py`
- Modify: `backend/tests/test_safety_classify_endpoint.py`
- Modify: `backend/tests/test_plan_mode.py`

**Interfaces:**
- Consumes: Task 3 的 `classify_command` 新 reason 与 `source`。
- Produces: 新 reason 全部有静态中文后果文案；可见终端确认、`/agent/safety/classify`、计划模式继续使用同一判定源。

- [ ] **Step 1: 写失败测试（后果表覆盖）**

在 `backend/tests/test_command_explain.py` 的 `DYNAMIC_SAMPLES` 末尾追加：

```python
    # 2026-10-23 只读证明 / 破坏性写 / 执行注入
    "date sets system clock (-s)",
    "date sets system clock (202001010000)",
    "find performs a destructive action (-delete)",
    "tar extracts or updates archive (writes files)",
    "yq in-place edit writes files",
    "git executes configured helper (-c)",
    "git transport helper injection (--upload-pack)",
    "git remote helper URL (ext::sh)",
    "openssl loads external engine/provider (-engine)",
    "awk executes shell commands (system/getline)",
    "tar runs an external program (checkpoint/--to-command)",
    "tar pipes the archive through an external program",
    "unparseable command chain (unclosed quote '; conservative)",
```

在 `CORPUS_COMMANDS` 末尾追加：

```python
    "date -s '2020-01-01'",
    "find /tmp -name '*.log' -delete",
    "tar -xzf app.tgz -C /srv",
    "yq -i '.a=1' deploy.yaml",
    "git -c core.pager=sh status",
    "awk '{system(\"id\")}' /tmp/x",
    "tar -I 'sh -c' -cf /tmp/x.tar /tmp/data",
    "openssl x509 -engine dynamic -in c.pem -noout",
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_command_explain.py -q`

Expected: FAIL — 新增 reason 未命中静态后果表（退化为 fallback）。

- [ ] **Step 3: 在 `CONSEQUENCE_RULES` 末尾追加规则**

在列表闭合 `]` 前插入：

```python
    # ── 2026-10-23 只读证明 / 破坏性写 / 执行注入 ──
    (re.compile(r"^date sets system clock"),
     "修改系统时间（{target}），会影响日志时间线、证书校验与定时任务，生产环境可能导致数据错乱。", False),
    (re.compile(r"^find performs a destructive action"),
     "find 在遍历过程中执行删除或外部命令（{target}），实际影响范围由匹配结果决定，删除后无法恢复。", False),
    (re.compile(r"^tar extracts or updates archive"),
     "解压或更新归档会把文件写入磁盘，同名文件可能被覆盖，恶意条目还可能写到目标目录之外。", False),
    (re.compile(r"^yq in-place edit writes files"),
     "原地修改 YAML 文件，原内容被覆盖；配置写错会直接影响服务启动。", False),
    (re.compile(r"^git executes configured helper"),
     "通过 git 配置让外部程序参与执行（{target}），实际运行的命令可能不是你以为的那条。", False),
    (re.compile(r"^git transport helper injection"),
     "让 git 调用外部传输助手（{target}），可在拉取或推送时执行任意程序。", False),
    (re.compile(r"^git remote helper URL"),
     "使用可执行的外部传输协议访问远端（{target}），可能触发任意命令。", False),
    (re.compile(r"^openssl loads external engine/provider"),
     "让 openssl 加载外部引擎或 provider（{target}），等于把执行权交给第三方动态库。", False),
    (re.compile(r"^awk executes shell commands"),
     "awk 脚本里调用 system()/getline 执行外部命令，真实行为取决于脚本内容。", False),
    (re.compile(r"^tar runs an external program"),
     "tar 被配置为调用外部程序处理数据流（{target}），等于执行任意命令。", False),
    (re.compile(r"^tar pipes the archive through an external program"),
     "tar 通过外部程序压缩或解压数据（{target}），等于执行任意命令。", False),
    (re.compile(r"^unparseable command chain"),
     "命令结构无法安全解析（引号或括号未闭合），无法判断它实际会执行什么，按最保守口径拦截。", False),
```

`journalctl --rotate/--flush/--sync` 复用已有的 `^journalctl log deletion` 前缀规则；`passwd` 复用已有规则，不重复添加。

- [ ] **Step 4: 运行后果表测试**

Run: `cd backend && python -m pytest tests/test_command_explain.py -q`

Expected: PASS（含死规则反向锁）。

- [ ] **Step 5: 写 shell_exec / 接口 / 计划模式回归测试**

在 `backend/tests/test_visible_delivery.py` 追加：

```python
def test_只读证明放行的命令在可见终端不弹确认():
    assert _compute_requires_confirm("jq '.a' app.json", "test") is False
    assert _compute_requires_confirm("getent passwd", "test") is False
    assert _compute_requires_confirm("tar -tzf app.tgz", "test") is False


def test_新增破坏性判定在可见终端仍需确认():
    assert _compute_requires_confirm("date -s '2020-01-01'", "test") is True
    assert _compute_requires_confirm("find /tmp -name '*.log' -delete", "test") is True
    assert _compute_requires_confirm("tar -xzf app.tgz", "test") is True
```

在 `backend/tests/test_safety_classify_endpoint.py` 的 `CROSS_CHECK` 追加：

```python
    "jq '.a' app.json",
    "getent passwd",
    "tar -tzf app.tgz",
    "date -s '2020-01-01'",
    "git -c core.pager=sh status",
```

在 `test_levels` 的参数化中追加：

```python
    ("jq '.a' app.json", "safe"),
    ("getent passwd", "safe"),
    ("tar -tzf app.tgz", "safe"),
    ("date -s '2020-01-01'", "dangerous"),
```

在 `test_dangerous_and_hardline_carry_chinese_consequence` 的命令列表中追加 `"date -s '2020-01-01'"` 与 `"git -c core.pager=sh status"`。

在 `backend/tests/test_plan_mode.py` 的 `DENIED_COMMANDS` 末尾追加：

```python
    # 2026-10-23 新增只读规则不自动继承计划模式（白名单保持严格子集）
    "jq '.a' app.json",
    "getent passwd",
    "docker compose ps",
    "tar -tzf app.tgz",
    "git ls-remote origin",
```

并在 `DENIED_COMMANDS` 的测试之后追加独立反向锁：

```python
NEW_READONLY_NOT_IN_PLAN_MODE = [
    "jq '.a' app.json",
    "getent passwd",
    "docker compose ps",
    "tar -tzf app.tgz",
    "git ls-remote origin",
    "openssl x509 -in c.pem -noout -text",
]


@pytest.mark.parametrize("cmd", NEW_READONLY_NOT_IN_PLAN_MODE)
def test_本轮新增只读规则不继承计划模式(cmd):
    """safety 已证明只读 ≠ 计划模式放行：计划模式白名单必须逐条显式登记。"""
    allowed, _ = pm.classify_plan_command(cmd)
    assert allowed is False, f"新只读规则意外泄漏进计划模式: {cmd!r}"
```

- [ ] **Step 6: 运行消费方测试**

Run: `cd backend && python -m pytest tests/test_visible_delivery.py tests/test_safety_classify_endpoint.py tests/test_plan_mode.py -q`

Expected: PASS。

- [ ] **Step 7: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/harness/command_explain.py backend/tests/test_command_explain.py \
  backend/tests/test_visible_delivery.py backend/tests/test_safety_classify_endpoint.py \
  backend/tests/test_plan_mode.py
git commit -m "feat(agent): 新判定接入后果文案与消费方回归"
```

### Task 5: 意图侧只读预期（展示与引导，永不授权）

**Files:**
- Modify: `backend/agent/intention.py`（`IntentionResult`、`to_dict`、四个构造点、`_READONLY_DISCIPLINE`、`get_guide_for_iteration`）
- Test: `backend/tests/test_intention_readonly_expected.py`（新建）

**Interfaces:**
- Consumes: `IntentionResult`、`TaskType`、`classify`、`get_guide_for_iteration`、`reclassify`。
- Produces: `IntentionResult.readonly_expected: bool`（默认 `False`）；`to_dict()["readonly_expected"]`；模块常量 `_READONLY_DISCIPLINE: str`。**该字段只用于展示/引导/审计，任何代码不得读取它来决定放行。**

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_intention_readonly_expected.py`：

```python
"""只读预期（readonly_expected）的行为与边界。

只读预期是意图侧对「这次任务期望不产生副作用」的推断，只用于：
1. 逐轮引导语追加只读纪律；
2. 审批卡展示 intent，并在需要审批时标注 intent_drift；
3. 审计统计。

它**永不参与授权**。本文件最后一条测试专门锁住这条边界。
"""
import pytest

from agent.harness.safety import classify_command
from agent.intention import (
    IntentionLevel,
    IntentionResult,
    TaskType,
    _READONLY_DISCIPLINE,
    classify,
    get_guide_for_iteration,
    reclassify,
)


@pytest.mark.parametrize("task", [
    "看看磁盘还有多少空间",          # QUERY
    "分析下 nginx 的错误日志",        # RESEARCH
    "为什么 nginx 起不来",            # DEBUG
    "排查为什么 mysql 服务挂了",      # DEBUG
    "你好",                          # CONVERSATION
])
def test_readonly_expected_for_readonly_tasks(task):
    result = classify(task)
    assert result.is_change is False
    assert result.readonly_expected is True


@pytest.mark.parametrize("task", [
    "重启 nginx",
    "修复 nginx 配置里的错误",
    "git commit 提交这次改动",
    "把 redis 配置文件里的 maxmemory 改成 2gb",
])
def test_no_readonly_expected_for_change_tasks(task):
    result = classify(task)
    assert result.is_change is True
    assert result.readonly_expected is False


def test_empty_task_is_readonly_expected():
    result = classify("")
    assert result.task_type is TaskType.QUERY
    assert result.is_change is False
    assert result.readonly_expected is True


def test_to_dict_contains_readonly_expected():
    payload = classify("看看磁盘还有多少空间").to_dict()
    assert payload["readonly_expected"] is True
    assert payload["is_change"] is False


def test_readonly_discipline_only_for_readonly_expected():
    readonly = classify("看看磁盘还有多少空间")
    change = classify("重启 nginx")

    assert _READONLY_DISCIPLINE in get_guide_for_iteration(readonly, 1)
    assert _READONLY_DISCIPLINE not in get_guide_for_iteration(change, 1)


def test_readonly_discipline_not_duplicated():
    readonly = classify("看看磁盘还有多少空间")
    guide = get_guide_for_iteration(readonly, 1)
    assert guide.count(_READONLY_DISCIPLINE.strip()) == 1


def test_reclassify_keeps_readonly_expected_for_readonly_task():
    original = classify("看看磁盘还有多少空间")
    assert original.readonly_expected is True

    updated = reclassify(
        original,
        iteration=1,
        tool_calls_log=[{"success": False}, {"success": False}, {"success": False}],
        errors=["boom", "boom", "boom"],
        max_iterations=10,
    )

    assert updated is not original          # 确认升级分支真的执行了
    assert updated.level is IntentionLevel.DEBUG
    assert updated.readonly_expected is True


def test_reclassify_keeps_change_task_non_readonly():
    original = classify("重启 nginx")
    updated = reclassify(
        original,
        iteration=1,
        tool_calls_log=[{"success": False}, {"success": False}, {"success": False}],
        errors=["boom", "boom", "boom"],
        max_iterations=10,
    )

    assert updated is not original
    assert updated.readonly_expected is False


def test_readonly_expected_never_authorizes_write():
    """意图说「预期只读」，不等于命令安全 —— 安全结论只看命令内容。"""
    intent = classify("看看磁盘还有多少空间")
    assert intent.readonly_expected is True

    level, reason = classify_command("systemctl restart nginx")
    assert level == "dangerous"
    assert reason


def test_manual_intention_result_defaults_to_not_readonly_expected():
    """默认值必须是 False：忘记赋值的构造点不得默认为「预期只读」。"""
    result = IntentionResult(
        level=IntentionLevel.SIMPLE,
        task_type=TaskType.MODIFY,
        reasoning_budget=512,
        allow_planning_phase=False,
        guide_prompt="x",
    )
    assert result.readonly_expected is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_intention_readonly_expected.py -q`

Expected: FAIL，`AttributeError: 'IntentionResult' object has no attribute 'readonly_expected'`。

- [ ] **Step 3: 实现字段与构造逻辑**

`backend/agent/intention.py` 的 `IntentionResult` 增加字段并写入 `to_dict`：

```python
    # 任务是否「预期」只读：由 task_type + is_change 推导，只用于引导语、
    # 审批卡展示、intent_drift 标注和审计。**永不参与授权** —— 安全结论
    # 只由命令内容、目标主机、运行环境决定（见 safety / permission gate）。
    readonly_expected: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "task_type": self.task_type.value,
            "reasoning_budget": self.reasoning_budget,
            "allow_planning_phase": self.allow_planning_phase,
            "confidence": self.confidence,
            "is_change": self.is_change,
            "readonly_expected": self.readonly_expected,
        }
```

在 `_CHANGE_DISCIPLINE` 之后新增常量：

```python
# 只读纪律（readonly_expected=True 时追加到逐轮引导）
# 与 _CHANGE_DISCIPLINE 对称：driver 把逐轮引导作为最后一条 user 消息注入，
# 这是提醒模型「不要为了省事顺手改状态」的最后位置。它只影响模型行为，
# 不改变任何命令的安全判定。
_READONLY_DISCIPLINE = (
    "\n\n[只读纪律] 本任务预期只做只读操作：优先使用查询/读取类命令，"
    "不要为了省事改写目标机状态；确需变更时先说明理由并等待审批。"
    "意图判定不构成授权——每条命令是否放行仍由安全判定与审批决定。"
)
```

空任务构造点（`if not task or not task.strip():` 分支）追加：

```python
            readonly_expected=True,  # QUERY + 非变更
```

CONVERSATION 快速路径返回处追加：

```python
            readonly_expected=True,  # 纯对话不产生副作用
```

`classify()` 主返回处追加：

```python
        readonly_expected=(
            not is_change
            and task_type in (
                TaskType.QUERY,
                TaskType.RESEARCH,
                TaskType.DEBUG,
                TaskType.CONVERSATION,
            )
        ),
```

`reclassify()` 的最终 `IntentionResult(...)` 追加（与 `is_change` 同一原则：工具结果不改变任务性质，按原始字段重算而不是透传可能被手改的布尔值）：

```python
        readonly_expected=(
            not original.is_change
            and original.task_type in (
                TaskType.QUERY,
                TaskType.RESEARCH,
                TaskType.DEBUG,
                TaskType.CONVERSATION,
            )
        ),
```

`get_guide_for_iteration` 追加：

```python
def get_guide_for_iteration(
    intention: IntentionResult,
    iteration: int,
) -> str:
    """逐轮引导语（公开入口）。

    在 `_guide_for_iteration_base` 的基础上，对变更类任务追加
    `_CHANGE_DISCIPLINE`，对只读预期任务追加 `_READONLY_DISCIPLINE`。
    二者互斥（readonly_expected 蕴含 not is_change）。
    """
    guide = _guide_for_iteration_base(intention, iteration)
    if intention.is_change and _CHANGE_DISCIPLINE not in guide:
        guide += _CHANGE_DISCIPLINE
    if intention.readonly_expected and not intention.is_change and _READONLY_DISCIPLINE not in guide:
        guide += _READONLY_DISCIPLINE
    return guide
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `cd backend && python -m pytest tests/test_intention_readonly_expected.py -q`

Expected: PASS（17 passed）。

- [ ] **Step 5: 运行既有意图测试防回归**

Run: `cd backend && python -m pytest tests/test_intention_ops.py tests/test_intention.py -q`

Expected: PASS。若 `test_intention.py` 不存在，只跑存在的文件。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/intention.py backend/tests/test_intention_readonly_expected.py
git commit -m "feat(agent): 意图侧增加只读预期，仅用于引导与展示"
```

### Task 6: 审计 detail 后缀格式与解析

**Files:**
- Modify: `backend/agent/audit.py`（`format_audit_detail`、`parse_audit_detail`、`append_record`）
- Test: `backend/tests/test_audit_detail.py`（新建）

**Interfaces:**
- Consumes: `audit.append_record`、`audit._AUDIT_SCHEMA`。
- Produces:
  - `format_audit_detail(fields: dict[str, Any]) -> str`，返回 `detail=v1;k=v;...`；
  - `parse_audit_detail(result: str) -> dict[str, str]`，无 detail 返回 `{}`；
  - `append_record(..., detail: str = "")`：detail 非空时以 `;` 追加到 `result` 末尾。
- **不改表结构、不改 `_canonical`**：detail 落在已有 `result` 字段内，天然进入链式签名。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_audit_detail.py`：

```python
"""审计 result 的 detail=v1 后缀：格式化、转义、解析、落库。"""
import sqlite3

import pytest

import database as db
from agent import audit


def _wired(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    return conn


def test_format_roundtrip():
    fields = {
        "source": "readonly_rule",
        "rule": "ro.verb.jq.v1",
        "intent": "query/trivial/ro/0.82",
    }
    encoded = audit.format_audit_detail(fields)

    assert encoded.startswith("detail=v1;")
    assert audit.parse_audit_detail(encoded) == fields


def test_format_escapes_separator_and_percent():
    fields = {
        "reason": "unknown; default=approval 100%",
        "source": "approval",
    }
    encoded = audit.format_audit_detail(fields)

    assert "%3B" in encoded
    assert "%3D" in encoded
    assert "%25" in encoded
    assert audit.parse_audit_detail(encoded) == fields


def test_format_skips_empty_values():
    encoded = audit.format_audit_detail({"source": "approval", "rule": ""})
    assert encoded == "detail=v1;source=approval"


def test_parse_keeps_human_prefix():
    result = (
        "approval_expired: shell_exec;"
        "detail=v1;source=approval_timeout;approval_key=k1"
    )
    parsed = audit.parse_audit_detail(result)
    assert parsed["source"] == "approval_timeout"
    assert parsed["approval_key"] == "k1"


def test_parse_uses_last_detail_marker():
    result = "x;detail=v1;source=old;detail=v1;source=new"
    assert audit.parse_audit_detail(result) == {"source": "new"}


@pytest.mark.parametrize("result", ["", "exit=0", "approval_expired: shell_exec", None])
def test_parse_without_detail_returns_empty(result):
    assert audit.parse_audit_detail(result) == {}


def test_parse_ignores_malformed_parts():
    parsed = audit.parse_audit_detail("detail=v1;source=approval;broken;=x")
    assert parsed == {"source": "approval"}


def test_append_record_appends_detail_after_prefix(wired):
    detail = audit.format_audit_detail({
        "source": "readonly_rule",
        "rule": "ro.verb.jq.v1",
        "intent": "query/trivial/ro/0.82",
    })

    record = audit.append_record(
        action="tool",
        conclusion="executed",
        command_raw="jq '.a' app.json",
        result="exit=0",
        detail=detail,
    )
    assert record is not None

    row = wired.execute(
        f"SELECT result FROM {audit.AUDIT_TABLE} WHERE id=?", (record["id"],)
    ).fetchone()
    assert row["result"].startswith("exit=0;detail=v1;")
    assert audit.parse_audit_detail(row["result"])["rule"] == "ro.verb.jq.v1"


def test_append_record_with_detail_only_result(wired):
    detail = audit.format_audit_detail({"source": "approval", "risk": "dangerous"})
    record = audit.append_record(
        action="tool",
        conclusion="requested",
        command_raw="systemctl restart nginx",
        detail=detail,
    )
    assert record is not None

    row = wired.execute(
        f"SELECT result FROM {audit.AUDIT_TABLE} WHERE id=?", (record["id"],)
    ).fetchone()
    assert row["result"] == "detail=v1;source=approval;risk=dangerous"


def test_detail_participates_in_chain_hash(wired):
    first = audit.append_record(action="tool", conclusion="executed", result="exit=0")
    second = audit.append_record(
        action="tool",
        conclusion="executed",
        result="exit=0",
        detail=audit.format_audit_detail({"source": "readonly_rule"}),
    )
    assert first is not None and second is not None
    assert first["hash"] != second["hash"]
    assert second["prev_hash"] == first["hash"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_audit_detail.py -q`

Expected: FAIL，`AttributeError: module 'agent.audit' has no attribute 'format_audit_detail'`。

- [ ] **Step 3: 实现格式化与解析**

在 `backend/agent/audit.py` 的 `command_hash` 之后新增：

```python
# ──────────────────────────────────────────────────────────────────────────────
# detail=v1 后缀（不改表结构，落在既有 result 字段内，天然进入链式签名）
# ──────────────────────────────────────────────────────────────────────────────

_DETAIL_MARKER = "detail=v1;"


def _detail_encode(value: Any) -> str:
    # 顺序固定：先转义 %，否则后续替换产生的 % 会被二次编码。
    return (
        str(value)
        .replace("%", "%25")
        .replace(";", "%3B")
        .replace("=", "%3D")
    )


def _detail_decode(value: str) -> str:
    # 与 _detail_encode 反序：%25 必须最后解，否则 %253B 会被二次解码。
    return (
        value
        .replace("%3B", ";")
        .replace("%3D", "=")
        .replace("%25", "%")
    )


def format_audit_detail(fields: dict[str, Any]) -> str:
    """把字段字典格式化为可追加到 result 的 detail 后缀。

    值内的 % ; = 做百分号转义，否则 reason 里的分号会把字段劈开。
    空值与 None 跳过，避免产生 `approval_key=` 这类半条字段。
    """
    parts = [
        f"{k}={_detail_encode(v)}"
        for k, v in fields.items()
        if v is not None and str(v) != ""
    ]
    return _DETAIL_MARKER + ";".join(parts)


def parse_audit_detail(result: str) -> dict[str, str]:
    """解析 result 中最末尾的 detail=v1; 后缀。

    旧记录没有 detail 时返回空 dict，不报错；格式损坏的片段忽略。
    """
    text = str(result or "")
    idx = text.rfind(_DETAIL_MARKER)
    if idx < 0:
        return {}
    payload = text[idx + len(_DETAIL_MARKER):]
    parsed: dict[str, str] = {}
    for part in payload.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if not key:
            continue
        parsed[key] = _detail_decode(value)
    return parsed
```

`append_record` 增加参数（放在 `result` 之后）：

```python
    result: str = "",
    detail: str = "",
    operator: Optional[str] = None,
```

并在 docstring 增加一句：

```python
    detail：已由 format_audit_detail 格式化好的 detail=v1 后缀，非空时
    以分号追加到 result 末尾；人类可读前缀原样保留。
```

在 `environment = environment or _DEFAULT_ENV` 之前插入：

```python
    if detail:
        result = f"{result};{detail}" if result else detail
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `cd backend && python -m pytest tests/test_audit_detail.py -q`

Expected: PASS（13 passed）。

- [ ] **Step 5: 运行既有审计测试防回归**

Run: `cd backend && python -m pytest tests/test_audit_append_endpoint.py tests/test_audit_conclusion_coercion.py tests/test_audit_commit_rollback.py tests/test_audit_read_honesty_s1101.py -q`

Expected: PASS（detail 默认空串，既有调用行为不变）。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/audit.py backend/tests/test_audit_detail.py
git commit -m "feat(audit): result 增加 detail=v1 后缀的格式化与解析"
```

### Task 7: 新增 `requested` 结论，收窄渲染层审计写入口

**Files:**
- Modify: `backend/agent/audit.py`（`LEGAL_CONCLUSIONS`）
- Modify: `backend/routes/audit.py`（`/audit/append` 结论校验）
- Modify: `src/api/audit.ts`（`AuditConclusion`、`AuditAppendInput.conclusion`）
- Modify: `src/views/ssh/AuditView.vue`（结论下拉）
- Test: `backend/tests/test_ai_loop_round13c.py`（三处清单守卫，10→11）
- Test: `backend/tests/test_audit_append_endpoint.py`（拒绝审批类结论）

**Interfaces:**
- Produces: `LEGAL_CONCLUSIONS` 含 `"requested"`（共 11 个）；`AuditConclusion` 联合类型含 `'requested'`；`AuditAppendInput.conclusion` 收窄为 `Extract<AuditConclusion, 'executed' | 'failed'>`。
- 约束：`requested` 与所有审批类结论（approved/denied/blocked/auto_allowed/released/revoked 等）**只能服务端写入**；`/audit/append` 只接受 `executed` / `failed`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_ai_loop_round13c.py` 的三处同源守卫中：

```python
    written = set(audit.LEGAL_CONCLUSIONS)
    assert len(written) == 11, f"枚举成员数变了（{sorted(written)}）：先确认三处是否同步"
```

并在该测试末尾的既有断言旁追加：

```python
    assert "requested" in written, "requested 是审批统计的分母，必须三处同源"
```

在 `backend/tests/test_audit_append_endpoint.py` 末尾追加：

```python
@pytest.mark.parametrize("conclusion", [
    "requested", "approved", "denied", "blocked", "auto_allowed",
    "released", "revoked", "answered", "skipped",
])
def test_append_rejects_server_side_conclusions(wired, conclusion):
    """审批类结论只能服务端在决策发生时写入。

    渲染层通道若能写 requested/approved，前端就可以伪造审批记录污染
    approval_stats；即使写入的是"真事"，归因也失去了服务端保证。
    """
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(conclusion=conclusion))
    assert resp.status_code == 422, f"{conclusion} 不应被渲染层通道接受"
    assert _row_count(conn) == 0


def test_append_accepts_failed(wired):
    """failed 是渲染层通道的真实执行结果，必须保留。"""
    client, conn = wired
    resp = client.post("/api/audit/append", json=_payload(conclusion="failed"))
    assert resp.status_code == 200, resp.text
    assert _row_count(conn) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源 tests/test_audit_append_endpoint.py -q`

Expected: FAIL —— 枚举仍为 10、`requested` 等结论仍被接受（返回 200）。

- [ ] **Step 3: 实现后端**

`backend/agent/audit.py`：

```python
# "requested"：审批卡首次下发时写入，是审批统计的分母；只由服务端写，
# 渲染层通道（/audit/append）不接受。
LEGAL_CONCLUSIONS: tuple[str, ...] = (
    "executed", "failed", "blocked", "denied", "auto_allowed",
    "approved", "released", "revoked", "answered", "skipped", "requested",
)
```

`backend/routes/audit.py` 顶部新增常量：

```python
# 渲染层通道只记录"真实执行结果"；requested/approved/denied 等审批类结论
# 只能由服务端在决策发生时写入，否则前端可以伪造审批记录污染统计。
_RENDERER_APPEND_CONCLUSIONS = frozenset({"executed", "failed"})
```

替换 `_conclusion_is_legal` 的方法体：

```python
    @field_validator("conclusion")
    @classmethod
    def _conclusion_is_legal(cls, v: str) -> str:
        # HTTP 边界不沿用 append_record 的「非法结论改 executed + 留痕」兜底：
        # 那是为了不阻塞 Agent 内部流程；调用方把 "executed " 拼错时，静默改名
        # 等于让链式签名记录声称一条没发生过的执行。审批类结论另走服务端，
        # 传进来一律 422。
        if v not in _RENDERER_APPEND_CONCLUSIONS:
            raise ValueError(
                f"渲染层通道只能追加 executed/failed：{v!r}"
            )
        return v
```

- [ ] **Step 4: 实现前端类型与下拉**

`src/api/audit.ts`：

```ts
/** 与 backend/agent/audit.py::LEGAL_CONCLUSIONS 逐字相等（三处同源守卫见 backend/tests/test_ai_loop_round13c.py） */
export type AuditConclusion =
  | 'executed' | 'failed' | 'blocked' | 'denied' | 'auto_allowed'
  | 'approved' | 'released' | 'revoked' | 'answered' | 'skipped'
  | 'requested'
```

并把 `AuditAppendInput.conclusion` 改为：

```ts
  /** 渲染层通道只能写真实执行结果；审批类结论由服务端写入 */
  conclusion: Extract<AuditConclusion, 'executed' | 'failed'>
```

`src/views/ssh/AuditView.vue` 的结论下拉增加一项（放在 `denied` 之后）：

```vue
        <option value="requested">审批请求 (requested)</option>
```

同时把该下拉上方注释里的“合法结论共 10 种”改为“合法结论共 11 种”。

- [ ] **Step 5: 运行后端与前端校验**

Run:
```bash
cd backend && python -m pytest tests/test_ai_loop_round13c.py::test_audit_conclusion_三处清单同源 tests/test_audit_append_endpoint.py -q
```

Expected: PASS（三处同源 + 9 个拒绝用例 + failed 接受）。

Run:
```bash
cd /d/workspace/cKunAgentSSH && npm run typecheck
```

Expected: PASS（`RemoteAuditConclusion` 已是 executed/failed 子集，无需改动）。

- [ ] **Step 6: 运行既有审计测试防回归**

Run: `cd backend && python -m pytest tests/test_audit_coverage.py tests/test_audit_run_id_on_approvals_s204.py tests/test_governance_env_and_audit_failclosed.py -q`

Expected: PASS。

- [ ] **Step 7: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/audit.py backend/routes/audit.py src/api/audit.ts src/views/ssh/AuditView.vue \
  backend/tests/test_ai_loop_round13c.py backend/tests/test_audit_append_endpoint.py
git commit -m "feat(audit): 新增 requested 结论并收窄渲染层审计写入口"
```

### Task 8: 审批 payload、`requested` 审计与 `readonly_allowed` 事件全链路

**Files:**
- Modify: `backend/agent/harness/reins/core.py`（意图上下文辅助、`permission_display_fields`）
- Modify: `backend/agent/harness/reins/guards.py`（detailed 分类、assessment 挂载、只读事件、requested 审计、卡片 payload）
- Modify: `backend/agent/harness/reins/gate.py`（工具级 requested 审计、卡片 payload）
- Modify: `backend/agent/harness/telemetry.py`（`_KIND_MAP`、审批字段与只读事件投影）
- Modify: `src/api/sse.ts`（`readonly_allowed`、新审批字段）
- Test: `backend/tests/test_approval_intent_wire.py`（新建）
- Test: `backend/tests/test_permission_gate_content_bound_tools.py`（首卡结论改为 requested）
- Test: `backend/tests/test_trace_projection_keys_s106.py`（新增投影形状测试）

**Interfaces:**
- Consumes: `classify_command_detailed`（Task 3）、`format_audit_detail`（Task 6）、`IntentionResult.readonly_expected`（Task 5）。
- Produces:
  - `core.intent_payload(ctx) -> Optional[dict]`、`core.intent_summary(ctx) -> str`、`core.intent_drift(ctx) -> bool`；
  - `permission_display_fields` 新增 `intent`（对象或 `None`）、`intent_drift`（bool）、`approval_ttl_seconds`（float）；
  - 审批卡 payload 新增 `approval_key`；
  - `TraceEvent(kind="readonly_allowed")` 进入 `_KIND_MAP` 与前端 union；
  - 首次发卡审计由 `denied/approval_needed` 改为 `requested` + `detail=v1;source=approval;...`。

> 顺序要求：`readonly_allowed` 必须在同一个任务里补齐后端 `_KIND_MAP`、telemetry 投影、前端 `AgentSSEEvent.type`，否则 `test_kind_map_covers_every_emitted_kind` / `test_frontend_union_matches_backend_registry` 会失败。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_approval_intent_wire.py`：

```python
"""审批卡的意图上下文、requested 审计、readonly_allowed 事件与 SSE 投影。"""
import asyncio
from types import SimpleNamespace

import pytest

import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.audit import parse_audit_detail
from agent.harness.contracts import AgentConfig, RunContext, TraceEvent
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.harness.reins.core import (
    intent_drift,
    intent_payload,
    intent_summary,
    permission_display_fields,
)
from agent.harness.telemetry import SSEAdapter
from agent.intention import classify
from agent.tools import create_registry


@pytest.fixture
def audits(monkeypatch):
    records = []

    async def _rec(**kw):
        records.append(kw)
        return None

    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    for _m in (_guards_mod, _gate_mod):
        monkeypatch.setattr(_m, "append_record_async", _rec)
    return records


def _saddle():
    return SimpleNamespace(registry=create_registry())


def _gate():
    return PermissionGate(saddle=_saddle())


def _guard(gate):
    return FilesystemGuard(permission_gate=gate, saddle=_saddle())


def _ctx() -> RunContext:
    return RunContext.new(task="看看磁盘还有多少空间", config=AgentConfig(),
                          environment="test")


def _log(ctx, tool, args):
    ctx.tool_calls_log.append({
        "tool": tool, "args": args, "iteration": len(ctx.tool_calls_log), "success": None,
    })


def test_intent_helpers_from_driver_metadata():
    ctx = _ctx()
    assert intent_payload(ctx) is None
    assert intent_summary(ctx) == ""
    assert intent_drift(ctx) is False

    ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
    payload = intent_payload(ctx)
    assert payload["task_type"] == "query"
    assert payload["readonly_expected"] is True
    assert intent_summary(ctx).startswith("query/")
    assert "/ro/" in intent_summary(ctx)
    assert intent_drift(ctx) is True


def test_permission_display_fields_carry_intent_and_ttl():
    ctx = _ctx()
    ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
    fields = permission_display_fields(ctx, "systemctl restart nginx")

    assert fields["intent"]["readonly_expected"] is True
    assert fields["intent_drift"] is True
    assert fields["approval_ttl_seconds"] > 0


def test_change_task_has_no_intent_drift():
    ctx = _ctx()
    ctx.metadata["intention"] = classify("重启 nginx").to_dict()
    fields = permission_display_fields(ctx, "systemctl restart nginx")

    assert fields["intent"]["is_change"] is True
    assert fields["intent_drift"] is False


def test_dangerous_card_carries_intent_key_and_ttl(audits):
    gate, ctx = _gate(), _ctx()
    ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
    _log(ctx, "shell_exec", {"command": "systemctl restart nginx"})

    decision = asyncio.run(_guard(gate).check(ctx))
    card = [e for e in decision.events if e.kind == "permission_needed"][0]

    assert decision.intercepted
    assert card.payload["intent"]["readonly_expected"] is True
    assert card.payload["intent_drift"] is True
    assert card.payload["approval_ttl_seconds"] > 0
    assert card.payload["approval_key"] == decision.approval_key


def test_first_card_writes_requested_audit_with_detail(audits):
    gate, ctx = _gate(), _ctx()
    ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
    _log(ctx, "shell_exec", {"command": "systemctl restart nginx"})

    decision = asyncio.run(_guard(gate).check(ctx))
    requested = [r for r in audits if r["conclusion"] == "requested"]
    assert len(requested) == 1
    assert requested[0]["run_id"] == ctx.run_id
    # 首卡没有人类前缀：只交 detail，不传 result。这里拦截的是 kwargs，
    # 所以断言的是「没有 result 键」，不是 result 的值。
    assert "result" not in requested[0]

    detail = parse_audit_detail(requested[0]["detail"])
    assert detail["source"] == "approval"
    assert detail["risk"] == "dangerous"
    assert detail["approval_key"] == decision.approval_key
    assert detail["intent"].startswith("query/")
    assert detail["intent_drift"] == "1"
    assert not [r for r in audits if r["conclusion"] == "denied"], \
        "首次发卡不再写 denied —— 审批统计的分母是 requested"


def test_repeat_request_does_not_duplicate_requested(audits):
    gate, ctx = _gate(), _ctx()
    _log(ctx, "shell_exec", {"command": "systemctl restart nginx"})
    asyncio.run(_guard(gate).check(ctx))
    asyncio.run(_guard(gate).check(ctx))

    assert len([r for r in audits if r["conclusion"] == "requested"]) == 1


def test_readonly_rule_emits_lightweight_event(audits):
    gate, ctx = _gate(), _ctx()
    events = []

    async def emit(event):
        events.append(event)

    ctx.metadata["emit_event"] = emit
    _log(ctx, "shell_exec", {"command": "jq '.a' app.json"})

    decision = asyncio.run(_guard(gate).check(ctx))
    assert not decision.intercepted
    assert [e.kind for e in events] == ["readonly_allowed"]
    assert events[0].payload["tool"] == "shell_exec"
    assert events[0].payload["command"] == "jq '.a' app.json"
    assert events[0].payload["rule_id"] == "ro.verb.jq.v1"


def test_legacy_safe_verb_does_not_emit_readonly_event(audits):
    gate, ctx = _gate(), _ctx()
    events = []

    async def emit(event):
        events.append(event)

    ctx.metadata["emit_event"] = emit
    _log(ctx, "shell_exec", {"command": "ls -la"})

    decision = asyncio.run(_guard(gate).check(ctx))
    assert not decision.intercepted
    assert events == [], "旧白名单不逐条发事件，否则长任务会刷屏"


def test_emit_failure_does_not_block_readonly_command(audits):
    gate, ctx = _gate(), _ctx()

    async def boom(event):
        raise RuntimeError("sse down")

    ctx.metadata["emit_event"] = boom
    _log(ctx, "shell_exec", {"command": "jq '.a' app.json"})

    decision = asyncio.run(_guard(gate).check(ctx))
    assert not decision.intercepted, "事件通道故障不得影响已证明只读的命令"


def test_audit_logger_writes_readonly_source_and_rule(audits):
    from agent.harness.reins import AuditLogger

    gate, ctx = _gate(), _ctx()
    _log(ctx, "shell_exec", {"command": "jq '.a' app.json"})
    asyncio.run(_guard(gate).check(ctx))
    ctx.tool_calls_log[-1]["success"] = True
    asyncio.run(AuditLogger().check(ctx))

    executed = [r for r in audits if r["conclusion"] == "executed"]
    assert len(executed) == 1
    detail = parse_audit_detail(executed[0]["detail"])
    assert detail["source"] == "readonly_rule"
    assert detail["rule"] == "ro.verb.jq.v1"


def test_readonly_event_wire_shape():
    out = SSEAdapter.to_sse_dict(TraceEvent(
        kind="readonly_allowed", source="reins.filesystem_guard",
        run_id="r1", iteration=2,
        payload={"tool": "shell_exec", "command": "jq '.a' app.json",
                 "rule_id": "ro.verb.jq.v1"},
    ))
    assert out["type"] == "readonly_allowed"
    assert out["tool"] == "shell_exec"
    assert out["command"] == "jq '.a' app.json"
    assert out["rule_id"] == "ro.verb.jq.v1"
    assert SSEAdapter._KIND_MAP["readonly_allowed"] == "readonly_allowed"


def test_permission_needed_projects_intent_host_and_key():
    out = SSEAdapter.to_sse_dict(TraceEvent(
        kind="permission_needed", source="reins.filesystem_guard",
        run_id="r2", iteration=3,
        payload={
            "tool": "shell_exec", "args": {"command": "systemctl restart nginx"},
            "risk": "dangerous", "reversible": False, "message": "需要用户授权",
            "environment": "test", "environment_source": "connection",
            "environment_source_label": "按主机连接配置判定",
            "host": "web-01", "ssh_session_id": "s1",
            "task": "看看磁盘", "command": "systemctl restart nginx",
            "intent": {"task_type": "query", "level": "trivial",
                       "confidence": 1.0, "readonly_expected": True,
                       "is_change": False},
            "intent_drift": True,
            "approval_key": "shell_exec:deadbeef",
            "approval_ttl_seconds": 600.0,
        },
    ))
    assert out["intent"]["readonly_expected"] is True
    assert out["intent_drift"] is True
    assert out["host"] == "web-01"
    assert out["environment_source"] == "connection"
    assert out["environment_source_label"] == "按主机连接配置判定"
    assert out["approval_key"] == "shell_exec:deadbeef"
    assert out["approval_ttl_seconds"] == 600.0


def test_tool_level_card_also_has_intent_drift(audits):
    gate, ctx = _gate(), _ctx()
    ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
    _log(ctx, "service_manage", {"action": "restart", "service": "nginx"})

    decision = asyncio.run(gate.check(ctx))
    card = [e for e in decision.events if e.kind == "permission_needed"][0]

    assert decision.intercepted
    assert card.payload["intent_drift"] is True
    assert card.payload["approval_key"] == decision.approval_key
    requested = [r for r in audits if r["conclusion"] == "requested"]
    assert len(requested) == 1
    assert parse_audit_detail(requested[0]["detail"])["source"] == "approval"


def test_intent_metadata_never_changes_interception(audits):
    command = "systemctl restart nginx"

    def run(with_intent):
        gate, ctx = _gate(), _ctx()
        if with_intent:
            ctx.metadata["intention"] = classify("看看磁盘还有多少空间").to_dict()
        _log(ctx, "shell_exec", {"command": command})
        decision = asyncio.run(_guard(gate).check(ctx))
        return decision.intercepted, decision.intercept_reason, decision.approval_key

    assert run(False) == run(True)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_approval_intent_wire.py -q`

Expected: FAIL —— `ImportError: cannot import name 'intent_payload'` / `KeyError: 'readonly_allowed'`。

- [ ] **Step 3: 实现 `core.py` 辅助与展示字段**

在 `approval_fallback_seconds` 之后新增：

```python
def intent_payload(ctx: "RunContext") -> Optional[dict[str, Any]]:
    """driver 写入的 `ctx.metadata["intention"]`（to_dict 形态）中与审批相关的子集。

    只读展示字段：意图不参与任何授权判定，缺字段/脏字段一律回 None。
    """
    raw = (getattr(ctx, "metadata", {}) or {}).get("intention")
    if not isinstance(raw, dict) or not raw.get("task_type"):
        return None
    try:
        confidence = float(raw.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "task_type": str(raw.get("task_type", "")),
        "level": str(raw.get("level", "")),
        "confidence": confidence,
        "readonly_expected": bool(raw.get("readonly_expected")),
        "is_change": bool(raw.get("is_change")),
    }


def intent_summary(ctx: "RunContext") -> str:
    """审计 detail 的 intent 值：`task_type/level/ro|chg/confidence(2位)`。"""
    payload = intent_payload(ctx)
    if payload is None:
        return ""
    flag = "ro" if payload["readonly_expected"] else (
        "chg" if payload["is_change"] else "none")
    return (
        f'{payload["task_type"]}/{payload["level"]}/{flag}/'
        f'{payload["confidence"]:.2f}'
    )


def intent_drift(ctx: "RunContext") -> bool:
    """任务本应只读、当前调用却需要审批 —— 仅展示/审计，不改变授权。"""
    payload = intent_payload(ctx)
    return bool(payload and payload["readonly_expected"])
```

`permission_display_fields` 的返回字典增加：

```python
    intent = intent_payload(ctx)
    return {
        ...
        "command": command,
        "intent": intent,
        "intent_drift": bool(intent and intent["readonly_expected"]),
        # 审批卡「始终允许」文案要说明等待/授权时限；0 = 永久等待
        "approval_ttl_seconds": approval_fallback_seconds(),
    }
```

（把 `return` 前移到局部变量之后，保持既有键逐字不变。）

- [ ] **Step 4: `guards.py` 接入 detailed 分类、只读事件与 requested**

导入改为：

```python
from agent.audit import append_record_async, format_audit_detail

from ..safety import classify_command_detailed
```

并在 `.core` 导入列表加入 `intent_drift, intent_summary`。

分类块替换为：

```python
        if bound:
            # 构造命令（file_write 等）沿用旧 wrapper 的「不做默认审批」语义；
            # detailed 的 structural guards 仍然生效（Task 3 已保证）。
            assessment = classify_command_detailed(
                bound, apply_default_approval=is_free_form
            )
            level, reason = assessment.as_pair()
            # POST 阶段 AuditLogger 直接读同一份判定，不重新分类：审计与放行同源。
            last["assessment"] = assessment
```

hardline 审计保留人类前缀并追加 detail：

```python
                    result=f"hardline: {reason}",
                    detail=format_audit_detail({
                        "source": assessment.source,
                        "reason": reason or "",
                        "intent": intent_summary(ctx),
                        "intent_drift": "1" if intent_drift(ctx) else "",
                    }),
```

prod 拒绝审计同样追加 `detail`（`source="prod_policy"`，其余同上）。

在 `elif level == "dangerous":` 分支结束、`if bound:` 块结束前，新增只读事件（放在 `# safe → 放行` 注释处）：

```python
            elif level == "safe" and assessment.source == "readonly_rule":
                emit = (ctx.metadata or {}).get("emit_event")
                if emit is not None:
                    try:
                        await emit(TraceEvent(
                            kind="readonly_allowed",
                            source="reins.filesystem_guard",
                            run_id=ctx.run_id,
                            iteration=ctx.iteration,
                            payload={
                                "tool": tool,
                                "command": str(cmd),
                                "rule_id": assessment.rule_id,
                            },
                        ))
                    except Exception as e:
                        logger.warning(
                            "readonly_allowed 事件发送失败（不影响放行）: %s", e)
```

`approval_key` 与 requested 审计：在 `is_first = first_ts is None` 之前计算：

```python
                        approval_key = self.permission_gate.approval_key_for_call(
                            tool, args, bound)
```

把原 `if is_first:` 下的 `denied/approval_needed` 审计整段替换为：

```python
                        if is_first:
                            pending[sig] = time.monotonic()
                            await append_record_async(
                                action=tool, conclusion="requested",
                                run_id=ctx.run_id, iteration=ctx.iteration,
                                target_type=_audit_target_type(ctx),
                                target_id=_audit_target_id(ctx),
                                environment=env,
                                command_raw=str(cmd),
                                detail=format_audit_detail({
                                    "source": "approval",
                                    "risk": "dangerous",
                                    "reason": reason or "",
                                    "approval_key": approval_key,
                                    "intent": intent_summary(ctx),
                                    "intent_drift": "1" if intent_drift(ctx) else "",
                                }),
                            )
```

（删除后面的 `if is_first: append denied` 块，避免同一次调用既有 requested 又有 denied。）

卡片 payload 增加 `"approval_key": approval_key`；返回值改用 `approval_key=approval_key`。

`AuditLogger`（同文件的 POST 阶段）补 detail：直接读 PRE 阶段挂到当前调用上的
`assessment`，不重新分类（spec §7.1 的「审计与放行同源」）：

```python
            assessment = last.get("assessment")
            await append_record_async(
                action=tool_name,
                conclusion="executed" if last.get("success") else "failed",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=_audit_environment(ctx),
                command_raw=command,
                detail=format_audit_detail({
                    "source": assessment.source,
                    "rule": assessment.rule_id,
                    "intent": intent_summary(ctx),
                    "intent_drift": "1" if intent_drift(ctx) else "",
                }) if assessment is not None else "",
            )
```

（只替换该调用的参数，现有 `bound` 提取与 command 选择保持原样；`assessment`
为空表示这次调用没有走命令分类，`detail` 留空而不是编一个 source。）

- [ ] **Step 5: `gate.py` 接入工具级 requested 与卡片字段**

导入增加 `format_audit_detail` 与 `.core` 的 `intent_drift, intent_summary`。

在 `is_first = first_ts is None` 之前计算：

```python
        approval_key = self.approval_key_for_call(tool, args, bound)
```

`is_first` 分支追加：

```python
        if is_first:
            pending[sig] = time.monotonic()
            await append_record_async(
                action=tool, conclusion="requested",
                run_id=ctx.run_id, iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=environment,
                command_raw=str(bound or ""),
                detail=format_audit_detail({
                    "source": "approval",
                    "risk": risk,
                    "reason": f"工具级风险 {risk} 需授权",
                    "approval_key": approval_key,
                    "intent": intent_summary(ctx),
                    "intent_drift": "1" if intent_drift(ctx) else "",
                }),
            )
```

卡片 payload 增加 `"approval_key": approval_key`；返回值改用 `approval_key=approval_key`。

- [ ] **Step 6: telemetry 与前端事件契约**

`backend/agent/harness/telemetry.py` 的 `_KIND_MAP` 增加：

```python
        "readonly_allowed": "readonly_allowed",
```

`permission_needed` 投影追加：

```python
            result["environment_source"] = payload.get("environment_source", "")
            result["environment_source_label"] = payload.get("environment_source_label", "")
            result["host"] = payload.get("host", "")
            result["intent"] = payload.get("intent") or None
            result["intent_drift"] = bool(payload.get("intent_drift", False))
            result["approval_key"] = payload.get("approval_key", "")
            result["approval_ttl_seconds"] = payload.get("approval_ttl_seconds", 0)
```

新增分支（放在 `permission_expired` 之后）：

```python
        elif kind == "readonly_allowed":
            result["tool"] = payload.get("tool", "")
            result["command"] = payload.get("command", "")
            result["rule_id"] = payload.get("rule_id", "")
```

`src/api/sse.ts`：

1. `AgentSSEEvent.type` 联合追加 `| 'readonly_allowed'`；
2. 增加字段：

```ts
  /** 审批卡意图上下文（只读展示，不参与授权） */
  intent?: AgentIntentionInfo | null
  /** 任务预期只读、本次调用却需要审批 —— 仅展示与审计 */
  intent_drift?: boolean
  /** 本次审批的稳定键：卡片、授权端点、审计 requested/approved 共用 */
  approval_key?: string
  /** 审批兜底等待时长（秒）；0 = 永久等待 */
  approval_ttl_seconds?: number
  /** readonly_allowed 事件：命中的只读规则 ID */
  rule_id?: string
```

3. `AgentIntentionInfo` 增加 `readonly_expected?: boolean` 与 `is_change?: boolean`。

- [ ] **Step 7: 更新首卡审计断言**

`backend/tests/test_permission_gate_content_bound_tools.py` 的参数化改为：

```python
@pytest.mark.parametrize("command,conclusion,result_prefix", [
    # hardline 仍阻断；dangerous 首次发卡写 requested（不再是 denied）
    ("rm -rf /", "blocked", "hardline"),
    ("systemctl restart nginx", "requested", "detail=v1;source=approval"),
])
```

并同步更新该测试的注释与函数名（语义从“行为不变”改为“hardline 口径不变、首卡结论改为 requested”）。

- [ ] **Step 8: 运行新测试与相关回归**

Run:
```bash
cd backend && python -m pytest tests/test_approval_intent_wire.py tests/test_trace_projection_keys_s106.py tests/test_permission_gate_content_bound_tools.py tests/test_audit_detail.py -q
```

Expected: PASS。

Run:
```bash
cd backend && python -m pytest tests/test_approval_blocking.py tests/test_auto_approve.py tests/test_permission_approval_lifecycle.py tests/test_permission_improvements.py -q
```

Expected: PASS。若出现对首次发卡 `denied` 的旧断言，按“`requested` 是首卡分母”的新语义同步修改，不要保留双写。

- [ ] **Step 9: 前端类型检查**

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS（`agent.ts` 尚未消费新字段，但 union 已声明）。

- [ ] **Step 10: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/harness/reins/core.py backend/agent/harness/reins/guards.py \
  backend/agent/harness/reins/gate.py backend/agent/harness/telemetry.py src/api/sse.ts \
  backend/tests/test_approval_intent_wire.py backend/tests/test_trace_projection_keys_s106.py \
  backend/tests/test_permission_gate_content_bound_tools.py
git commit -m "feat(agent): 审批卡携带意图上下文，只读放行与 requested 全链路上报"
```

### Task 9: 前端 store 接住意图、只读事件与 run 统计

**Files:**
- Modify: `src/stores/agent.ts`
- Test: `src/stores/agent.test.ts`

**Interfaces:**
- Consumes: `AgentSSEEvent` 的新字段（Task 8），`AgentIntentionInfo`。
- Produces:
  - `PermissionRequest.intent?: AgentIntentionInfo`、`intentDrift?: boolean`、`approvalKey?: string`、`approvalTtlSeconds?: number`；
  - `ReadonlyAllowance`（tool/command/ruleId/timestamp）与 `readonlyAllowances`；
  - `AgentRunStats`（readonlyAllowed/requested/approved/denied/expired）与 `runStats`；
  - store 导出 `runStats`、`readonlyAllowances`。

- [ ] **Step 1: 写失败测试**

在 `src/stores/agent.test.ts` 末尾追加：

```ts
describe('审批意图与只读放行统计', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('permission_needed 携带意图、漂移、审批键与 TTL', () => {
    const { store, push } = startRun()
    push({
      type: 'permission_needed',
      tool: 'shell_exec',
      args: { command: 'systemctl restart nginx' },
      command: 'systemctl restart nginx',
      risk: 'dangerous',
      intent: {
        level: 'trivial',
        task_type: 'query',
        reasoning_budget: 256,
        allow_planning_phase: false,
        confidence: 0.8,
        readonly_expected: true,
        is_change: false,
      },
      intent_drift: true,
      approval_key: 'shell_exec:deadbeef',
      approval_ttl_seconds: 600,
      run_id: 'r1',
    })

    expect(store.permissionRequests).toHaveLength(1)
    const card = store.permissionRequests[0]
    expect(card.intent?.readonly_expected).toBe(true)
    expect(card.intentDrift).toBe(true)
    expect(card.approvalKey).toBe('shell_exec:deadbeef')
    expect(card.approvalTtlSeconds).toBe(600)
    expect(store.runStats.requested).toBe(1)
  })

  it('重复 permission_needed 不重复计 requested', () => {
    const { store, push } = startRun()
    const event = {
      type: 'permission_needed',
      tool: 'shell_exec',
      command: 'systemctl restart nginx',
      risk: 'dangerous',
      run_id: 'r1',
    }
    push(event)
    push(event)
    expect(store.permissionRequests).toHaveLength(1)
    expect(store.runStats.requested).toBe(1)
  })

  it('readonly_allowed 累加统计并保留命令与规则 ID', () => {
    const { store, push } = startRun()
    push({
      type: 'readonly_allowed',
      tool: 'shell_exec',
      command: "jq '.a' app.json",
      rule_id: 'ro.verb.jq.v1',
      iteration: 2,
      run_id: 'r1',
    })

    expect(store.runStats.readonlyAllowed).toBe(1)
    expect(store.readonlyAllowances).toEqual([
      expect.objectContaining({
        tool: 'shell_exec',
        command: "jq '.a' app.json",
        ruleId: 'ro.verb.jq.v1',
      }),
    ])
  })

  it('done 清空卡片但保留 run 统计（摘要要在 run 结束后仍可读）', () => {
    const { store, push } = startRun()
    push({ type: 'readonly_allowed', tool: 'shell_exec', command: 'jq . a.json',
      rule_id: 'ro.verb.jq.v1', run_id: 'r1' })
    push({ type: 'permission_needed', tool: 'shell_exec', command: 'rm -f /tmp/x',
      risk: 'dangerous', run_id: 'r1' })
    push({ type: 'done', state: 'completed', iterations: 3, run_id: 'r1' })

    expect(store.permissionRequests).toHaveLength(0)
    expect(store.runStats.readonlyAllowed).toBe(1)
    expect(store.runStats.requested).toBe(1)
  })

  it('新 run 的 _reset 清空上一轮统计', () => {
    const { store, push } = startRun()
    push({ type: 'readonly_allowed', tool: 'shell_exec', command: 'jq . a.json',
      rule_id: 'ro.verb.jq.v1', run_id: 'r1' })

    store.runAgent({ task: 'next', conversationId: 'c1', modelId: 'm1' })
    expect(store.runStats.readonlyAllowed).toBe(0)
    expect(store.readonlyAllowances).toEqual([])
  })

  it('批准/拒绝/超时分别计数', async () => {
    const { store, push } = startRun()
    push({ type: 'permission_needed', tool: 'shell_exec', command: 'systemctl restart nginx',
      risk: 'dangerous', run_id: 'r1' })
    const id = store.permissionRequests[0].id

    await store.approvePermission(id, 'once')
    expect(store.runStats.approved).toBe(1)

    push({ type: 'permission_needed', tool: 'shell_exec', command: 'rm -f /tmp/x',
      risk: 'dangerous', run_id: 'r1' })
    const id2 = store.permissionRequests.find(r => !r.resolved)!.id
    await store.denyPermission(id2)
    expect(store.runStats.denied).toBe(1)

    push({ type: 'permission_needed', tool: 'shell_exec', command: 'service nginx reload',
      risk: 'dangerous', run_id: 'r1' })
    push({ type: 'permission_expired', tool: 'shell_exec',
      command: 'service nginx reload', run_id: 'r1' })
    expect(store.runStats.expired).toBe(1)
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/stores/agent.test.ts`

Expected: FAIL —— `store.runStats` 未定义、卡片字段为 `undefined`。

- [ ] **Step 3: 扩展类型与状态**

在 `PermissionRequest` 末尾增加：

```ts
  /** 审批卡意图上下文（只读展示，不参与授权） */
  intent?: AgentIntentionInfo | null
  /** 任务预期只读、本次调用却需要审批 */
  intentDrift?: boolean
  /** 本次审批的稳定键：与后端 requested/approved 审计共用 */
  approvalKey?: string
  /** 审批兜底等待时长（秒）；0 = 永久等待 */
  approvalTtlSeconds?: number
```

在 `PermissionRequest` 之前新增：

```ts
/** 只读规则自动放行的一次记录（run 摘要展开用） */
export interface ReadonlyAllowance {
  tool: string
  command: string
  ruleId: string
  timestamp: number
}

/** 单次 run 的审批/只读统计；done 清卡片但不清它 */
export interface AgentRunStats {
  readonlyAllowed: number
  requested: number
  approved: number
  denied: number
  expired: number
}

function emptyRunStats(): AgentRunStats {
  return { readonlyAllowed: 0, requested: 0, approved: 0, denied: 0, expired: 0 }
}
```

在 state 区（`permissionRequests` 附近）新增：

```ts
  const runStats = ref<AgentRunStats>(emptyRunStats())
  const readonlyAllowances = ref<ReadonlyAllowance[]>([])
```

`_reset()` 增加：

```ts
    runStats.value = emptyRunStats()
    readonlyAllowances.value = []
```

`clearRunNotice()` 同样增加这两行。

- [ ] **Step 4: 事件与动作接线**

`permission_needed` 的新卡对象追加：

```ts
            intent: event.intent || undefined,
            intentDrift: event.intent_drift === true,
            approvalKey: event.approval_key || undefined,
            approvalTtlSeconds: event.approval_ttl_seconds,
```

并在 `if (!alreadyPending) { permissionRequests.value.push({...}) ... }` 内、push 之后增加：

```ts
          runStats.value.requested += 1
```

`permission_expired` 命中卡片时增加：

```ts
            r.expired = true
            r.settling = false
            runStats.value.expired += 1
```

在 `case 'permission_needed'` 之前新增：

```ts
      case 'readonly_allowed': {
        // 旧白名单（legacy_safe_verb）不发这个事件；能到这里就是新只读规则证明的
        // 放行，逐条留命令与规则 ID，run 摘要可展开对账（精确审计在 audit_log）。
        const command = typeof event.command === 'string' ? event.command : ''
        runStats.value.readonlyAllowed += 1
        readonlyAllowances.value.push({
          tool: event.tool || '',
          command,
          ruleId: event.rule_id || '',
          timestamp: Date.now(),
        })
        if (readonlyAllowances.value.length > 100) readonlyAllowances.value.shift()
        break
      }
```

`approvePermission` 成功后：

```ts
      resolvePermission(id, true)
      runStats.value.approved += 1
      return 'granted'
```

`denyPermission` 成功后：

```ts
    dismissPermission(id)
    runStats.value.denied += 1
    return true
```

返回对象增加：

```ts
    runStats,
    readonlyAllowances,
```

- [ ] **Step 5: 运行前端测试**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/stores/agent.test.ts`

Expected: PASS。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add src/stores/agent.ts src/stores/agent.test.ts
git commit -m "feat(agent-ui): store 接住意图、只读放行与 run 统计"
```

### Task 10: 审批卡按 run + 主机聚合（不改变逐条授权）

**Files:**
- Create: `src/utils/approval-groups.ts`
- Create: `src/utils/approval-groups.test.ts`
- Modify: `src/components/agent/ApprovalDock.vue`

**Interfaces:**
- Consumes: `PermissionRequest`（Task 9 字段）。
- Produces: `groupApprovals(requests): ApprovalGroup[]`，按首次出现顺序分组；组内保留原卡顺序。

- [ ] **Step 1: 写失败测试**

新建 `src/utils/approval-groups.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import type { PermissionRequest } from '@/stores/agent'
import { groupApprovals } from './approval-groups'

function req(over: Partial<PermissionRequest>): PermissionRequest {
  return {
    id: over.id || 'p1',
    tool: over.tool || 'shell_exec',
    args: over.args || {},
    risk: over.risk || 'dangerous',
    reversible: over.reversible ?? false,
    message: over.message || '需要授权',
    timestamp: over.timestamp || 1,
    ...over,
  }
}

describe('groupApprovals', () => {
  it('按 run_id + host 分组，保持首次出现顺序', () => {
    const groups = groupApprovals([
      req({ id: 'a', runId: 'run-1', host: 'web-01' }),
      req({ id: 'b', runId: 'run-1', host: 'web-02' }),
      req({ id: 'c', runId: 'run-1', host: 'web-01' }),
      req({ id: 'd', runId: 'run-2', host: 'web-01' }),
    ])

    expect(groups.map(g => [g.runId, g.host, g.items.map(i => i.id)])).toEqual([
      ['run-1', 'web-01', ['a', 'c']],
      ['run-1', 'web-02', ['b']],
      ['run-2', 'web-01', ['d']],
    ])
  })

  it('组风险取最严：未知/空风险按 dangerous', () => {
    const groups = groupApprovals([
      req({ id: 'a', risk: 'safe' }),
      req({ id: 'b', risk: '' }),
    ])
    expect(groups[0].risk).toBe('dangerous')
  })

  it('聚合只读预期与漂移条数', () => {
    const intent = {
      level: 'trivial', task_type: 'query', reasoning_budget: 256,
      allow_planning_phase: false, confidence: 0.8,
      readonly_expected: true, is_change: false,
    }
    const groups = groupApprovals([
      req({ id: 'a', intent, intentDrift: true }),
      req({ id: 'b', intent, intentDrift: false }),
    ])
    expect(groups[0].readonlyExpected).toBe(true)
    expect(groups[0].intentDriftCount).toBe(1)
    expect(groups[0].intentLabel).toBe('query/trivial')
  })

  it('moderate 保持 moderate，不升级也不降级', () => {
    expect(groupApprovals([req({ risk: 'moderate' })])[0].risk).toBe('moderate')
    expect(groupApprovals([req({ risk: 'safe' })])[0].risk).toBe('safe')
  })

  it('空队列返回空数组', () => {
    expect(groupApprovals([])).toEqual([])
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/utils/approval-groups.test.ts`

Expected: FAIL —— 模块不存在。

- [ ] **Step 3: 实现纯函数**

新建 `src/utils/approval-groups.ts`：

```ts
import type { PermissionRequest } from '@/stores/agent'

export interface ApprovalGroup {
  key: string
  runId?: string
  host?: string
  items: PermissionRequest[]
  /** 组内最严风险；未知风险按 dangerous（fail-closed） */
  risk: 'safe' | 'moderate' | 'dangerous'
  /** 组内是否混有“任务预期只读”的卡；只做展示，不参与任何放行 */
  readonlyExpected: boolean
  intentDriftCount: number
  /** 组内首个带意图卡的 task_type/level，供组头展示 */
  intentLabel: string
}

const RISK_RANK: Record<string, number> = { safe: 0, moderate: 1, dangerous: 2 }
const RANK_RISK: Array<ApprovalGroup['risk']> = ['safe', 'moderate', 'dangerous']

function rankOf(risk: string): number {
  return RISK_RANK[risk] ?? 2
}

/**
 * 按 run_id + host 聚合待审批卡。
 *
 * 只影响呈现：每条卡仍逐条结算，「全部批准」仍受 batchApproveBlocked 约束。
 * 待办队列本身是全局的（跨 run 并行调用时一次可能有多张卡），分组让用户看得清
 * 「这批是哪个任务、打到哪台机器」，而不是把不同 run 的卡混成一列。
 */
export function groupApprovals(
  requests: ReadonlyArray<PermissionRequest>,
): ApprovalGroup[] {
  const groups = new Map<string, ApprovalGroup>()

  for (const item of requests) {
    const runId = item.runId || undefined
    const host = item.host || undefined
    const key = `${runId || ''}|${host || ''}`
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        runId,
        host,
        items: [],
        risk: 'safe',
        readonlyExpected: false,
        intentDriftCount: 0,
        intentLabel: '',
      }
      groups.set(key, group)
    }

    group.items.push(item)
    const rank = rankOf(item.risk)
    if (rank > RISK_RANK[group.risk]) group.risk = RANK_RISK[rank]
    if (item.intent?.readonly_expected) group.readonlyExpected = true
    if (item.intentDrift) group.intentDriftCount += 1
    if (!group.intentLabel && item.intent?.task_type) {
      group.intentLabel = `${item.intent.task_type}/${item.intent.level}`
    }
  }

  return [...groups.values()]
}
```

- [ ] **Step 4: ApprovalDock 按组渲染**

`src/components/agent/ApprovalDock.vue`：

1. 导入：

```ts
import { groupApprovals } from '@/utils/approval-groups'
```

2. computed：

```ts
const groups = computed(() => groupApprovals(pending.value))
```

3. 模板把单层 `v-for="req in pending"` 替换为：

```vue
      <div class="ad-body">
        <section v-for="group in groups" :key="group.key" class="ad-group">
          <header class="ad-group-head">
            <span class="ad-group-run">
              {{ group.runId ? `运行 ${group.runId.slice(0, 8)}` : '未绑定运行' }}
            </span>
            <span v-if="group.host" class="ad-group-host" :title="group.host">
              主机 {{ group.host }}
            </span>
            <span class="ad-group-count">{{ group.items.length }} 条</span>
            <span
              class="ad-group-risk"
              :class="`risk-${group.risk}`"
              :title="group.risk === 'dangerous'
                ? '组内存在危险操作，必须逐条审批'
                : group.risk === 'moderate' ? '组内存在需谨慎确认的操作' : '组内命令风险较低'"
            >
              {{ group.risk === 'dangerous' ? '含危险操作' : group.risk === 'moderate' ? '需谨慎' : '低风险' }}
            </span>
            <span v-if="group.readonlyExpected" class="ad-group-ro">预期只读</span>
            <span v-if="group.intentLabel" class="ad-group-intent">
              意图 {{ group.intentLabel }}
            </span>
            <span v-if="group.intentDriftCount" class="ad-group-drift">
              {{ group.intentDriftCount }} 条偏离只读预期
            </span>
          </header>
          <div class="ad-group-cards">
            <ApprovalCard v-for="req in group.items" :key="req.id" :req="req" />
          </div>
        </section>
      </div>
```

4. 样式追加：

```css
.ad-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ad-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 2px;
  font-size: 11px;
  color: var(--text-tertiary);
  min-width: 0;
}

.ad-group-run,
.ad-group-host {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ad-group-host { color: var(--text-secondary) }
.ad-group-count { flex: none }
.ad-group-intent { flex: none; color: var(--text-secondary) }
.ad-group-ro { flex: none; color: var(--text-secondary) }

.ad-group-risk {
  flex: none;
  padding: 0 6px;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
}
.ad-group-risk.risk-safe { color: var(--text-secondary) }
.ad-group-risk.risk-moderate {
  color: var(--status-warn-text);
  border-color: color-mix(in srgb, var(--status-warn-text) 45%, transparent);
}
.ad-group-risk.risk-dangerous {
  color: var(--status-error-text);
  border-color: color-mix(in srgb, var(--status-error-text) 45%, transparent);
}

.ad-group-drift {
  flex: none;
  color: var(--status-warn-text);
  font-weight: 600;
}

.ad-group-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
```

> 不改批量按钮语义：全局「全部批准本次」仍由 `batchApproveBlocked(pending.value)` 决定；dangerous 或 prod 存在时按钮保持禁用，不存在组内批量入口。

- [ ] **Step 5: 运行前端测试与类型检查**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npx vitest run src/utils/approval-groups.test.ts src/utils/approval-scopes.test.ts src/stores/agent.test.ts
```

Expected: PASS。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add src/utils/approval-groups.ts src/utils/approval-groups.test.ts \
  src/components/agent/ApprovalDock.vue
git commit -m "feat(agent-ui): 审批队列按运行与主机聚合展示"
```

### Task 11: 审批卡意图行、漂移警告与 always 授权范围文案

**Files:**
- Modify: `src/utils/approval-display.ts`
- Modify: `src/utils/approval-display.test.ts`
- Modify: `src/components/agent/ApprovalCard.vue`

**Interfaces:**
- Consumes: `PermissionRequest.intent/intentDrift/approvalKey/approvalTtlSeconds`（Task 9）。
- Produces: `approvalIntentLabel(intent)`、`approvalGrantScopeCopy(req)`（纯函数，便于测试）。

- [ ] **Step 1: 写失败测试**

在 `src/utils/approval-display.test.ts` 末尾追加：

```ts
describe('approvalIntentLabel', () => {
  it('把 task_type/level/只读预期拼成人读文案', () => {
    expect(approvalIntentLabel({
      task_type: 'query', level: 'trivial', readonly_expected: true,
    })).toBe('查询 · trivial · 预期只读')
  })

  it('变更任务明确标注，不冒充只读', () => {
    expect(approvalIntentLabel({
      task_type: 'modify', level: 'simple', readonly_expected: false, is_change: true,
    })).toBe('修改 · simple')
  })

  it('缺失意图返回空串（调用方不渲染该行）', () => {
    expect(approvalIntentLabel(null)).toBe('')
    expect(approvalIntentLabel({})).toBe('')
  })
})

describe('approvalGrantScopeCopy', () => {
  it('主机 + 等待窗口 + 精确命令哈希三件套', () => {
    expect(approvalGrantScopeCopy({
      host: 'web-01', approvalTtlSeconds: 600, approvalKey: 'shell_exec:deadbeef',
    })).toBe('仅主机 web-01；审批等待窗口 600 秒；精确命令哈希 shell_exec:deadbeef')
  })

  it('无主机时明确说仅本机，不静默扩大范围', () => {
    expect(approvalGrantScopeCopy({ approvalTtlSeconds: 0 }))
      .toBe('仅本机；审批等待窗口按设置；精确命令哈希')
  })

  it('缺字段时不编造具体值', () => {
    expect(approvalGrantScopeCopy({})).toBe('仅本机；审批等待窗口按设置；精确命令哈希')
  })
})
```

并在该文件顶部的 import 中加入 `approvalGrantScopeCopy`、`approvalIntentLabel`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/utils/approval-display.test.ts`

Expected: FAIL —— 两个导出不存在。

- [ ] **Step 3: 实现纯函数**

在 `src/utils/approval-display.ts` 末尾新增：

```ts
const TASK_TYPE_LABELS: Record<string, string> = {
  query: '查询',
  research: '调研',
  debug: '排障',
  conversation: '对话',
  modify: '修改',
  implement: '实现',
  refactor: '重构',
}

/** 审批卡意图行：中文任务类型 + 等级 + 只读预期；缺失时返回空串。 */
export function approvalIntentLabel(
  intent?: {
    task_type?: string
    level?: string
    readonly_expected?: boolean
    is_change?: boolean
  } | null,
): string {
  if (!intent?.task_type) return ''
  const type = TASK_TYPE_LABELS[intent.task_type] || intent.task_type
  const level = intent.level ? ` · ${intent.level}` : ''
  const readonly = intent.readonly_expected ? ' · 预期只读' : ''
  return `${type}${level}${readonly}`
}

/**
 * 「始终允许」的实际作用范围：主机 + 审批等待窗口 + 精确命令哈希。
 * 三个维度缺一不可 —— 只写「始终允许」会让用户以为它是全局、永久、按工具的授权。
 */
export function approvalGrantScopeCopy(req: {
  host?: string
  approvalTtlSeconds?: number
  approvalKey?: string
}): string {
  const host = req.host ? `仅主机 ${req.host}` : '仅本机'
  const ttl = typeof req.approvalTtlSeconds === 'number' && req.approvalTtlSeconds > 0
    ? `审批等待窗口 ${Math.round(req.approvalTtlSeconds)} 秒`
    : '审批等待窗口按设置'
  const hash = req.approvalKey
    ? `精确命令哈希 ${req.approvalKey}`
    : '精确命令哈希'
  return `${host}；${ttl}；${hash}`
}
```

- [ ] **Step 4: ApprovalCard 接入**

导入增加：

```ts
import {
  approvalCommand,
  approvalGrantScopeCopy,
  approvalIntentLabel,
  approvalRiskClass,
  approvalRiskLabel,
  approvalTargetLabel,
  approvalWritePreview,
} from '@/utils/approval-display'
```

computed 增加：

```ts
const intentText = computed(() => approvalIntentLabel(req.value.intent))
const grantScopeCopy = computed(() => approvalGrantScopeCopy(req.value))
```

`alwaysTitle` 改为：

```ts
const alwaysTitle = computed(() => {
  const target = writePreview.value ? '这个路径（之后写什么内容都算）'
    : command.value ? '这行命令' : '这个工具'
  return `把${target}写入本机持久授权清单（${grantScopeCopy.value}）：换一台机器仍需重新确认`
})
```

模板在「你的请求」之后插入：

```vue
      <div v-if="intentText" class="ap-keyval">
        <span class="ap-k">任务意图</span>
        <span class="ap-v">{{ intentText }}</span>
      </div>
      <div v-if="req.intentDrift" class="ap-drift-warn">
        任务预期只读，但这条命令会改变目标机状态。意图提示不构成授权，请确认后再放行。
      </div>
```

底部提示改为包含授权范围：

```vue
    <div class="ap-hint">
      {{ req.host
        ? `「始终允许」只写入主机 ${req.host} 的持久授权清单，换一台机器仍需重新确认；拿不准选「允许本次」。`
        : '「始终允许」会写入本机持久授权清单，之后包括生产环境在内都不再询问 —— 拿不准选「允许本次」。' }}
      <span class="ap-scope-copy">授权范围：{{ grantScopeCopy }}</span>
    </div>
```

样式追加：

```css
.ap-drift-warn {
  margin: 6px 0;
  padding: 6px 8px;
  border-left: 3px solid var(--status-warn-text);
  background: var(--status-warn-bg);
  color: var(--status-warn-text);
  font-size: 11.5px;
  line-height: 1.5;
}

.ap-scope-copy {
  display: block;
  margin-top: 3px;
  color: var(--text-tertiary);
  word-break: break-all;
}
```

> `--status-warn-bg` / `--status-warn-text` 已存在于 `src/styles/variables.css`，直接复用，不要新增硬编码色值。

- [ ] **Step 5: 运行前端测试与类型检查**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npx vitest run src/utils/approval-display.test.ts src/stores/agent.test.ts
```

Expected: PASS。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add src/utils/approval-display.ts src/utils/approval-display.test.ts \
  src/components/agent/ApprovalCard.vue
git commit -m "feat(agent-ui): 审批卡展示意图漂移与 always 授权范围"
```

### Task 12: run 结束摘要（只对账，不给决策入口）

**Files:**
- Modify: `src/stores/agent.ts`（`hasRunNotice` 纳入 `runStats`）
- Modify: `src/components/agent/AgentRunPanel.vue`
- Test: `src/stores/agent.test.ts`
- Test: `src/components/agent/approval-trace-boundary.test.ts`

**Interfaces:**
- Consumes: Task 9 的 `runStats`（`readonlyAllowed/requested/approved/denied/expired`）与 `readonlyAllowances`（`tool/command/ruleId/timestamp`）；`isRunning`、`clearRunNotice`。
- Produces: 数据侧 `hasRunNotice` 在“只有统计、没有其它通知”时也为真；面板底部“本轮运行摘要”块。无新增导出。

**为什么改 `hasRunNotice` 而不是在宿主再加条件：**
`done` 会清空 `permissionRequests`，统计却要留到用户看完摘要为止。若摘要块自己写 `v-if`、宿主仍只看 `hasRunNotice`，那么一轮“全是只读放行、零告警”的 run 结束后整块面板不挂载，摘要永远不出现。该判定按既有约定只能有一份，放回 store。

- [ ] **Step 1: 写失败测试（store）**

在 `src/stores/agent.test.ts` 末尾追加：

```ts
describe('run 摘要的事件源', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('只有统计时通知区也要挂载，done 后仍保留', () => {
    const { store, push } = startRun()
    expect(store.hasRunNotice).toBe(false)

    push({
      type: 'readonly_allowed',
      tool: 'shell_exec',
      command: 'jq . a.json',
      rule_id: 'ro.verb.jq.v1',
      run_id: 'r1',
    })
    expect(store.hasRunNotice).toBe(true)

    push({ type: 'done', state: 'completed', iterations: 1, run_id: 'r1' })
    expect(store.permissionRequests).toHaveLength(0)
    expect(store.hasRunNotice).toBe(true)
    expect(store.runStats.readonlyAllowed).toBe(1)
  })

  it('clearRunNotice 连同摘要计数一起清掉', () => {
    const { store, push } = startRun()
    push({
      type: 'permission_needed',
      tool: 'shell_exec',
      command: 'systemctl restart nginx',
      risk: 'dangerous',
      run_id: 'r1',
    })
    expect(store.hasRunNotice).toBe(true)

    store.clearRunNotice()

    expect(store.runStats.requested).toBe(0)
    expect(store.readonlyAllowances).toEqual([])
    expect(store.hasRunNotice).toBe(false)
  })
})
```

- [ ] **Step 2: 写失败测试（面板边界）**

在 `src/components/agent/approval-trace-boundary.test.ts` 末尾追加：

```ts
describe('run 结束摘要只对账，不提供决策入口', () => {
  it('摘要渲染只读/审批/拒绝计数，并可展开只读事件明细', () => {
    expect(agentRunPanel).toContain('本轮运行摘要')
    expect(agentRunPanel).toContain('自动放行')
    expect(agentRunPanel).toContain('readonlyAllowances')
  })

  it('摘要是纯展示：审批动作仍只在 ApprovalDock / ApprovalCard', () => {
    expect(agentRunPanel).not.toContain('approvePermission(')
    expect(agentRunPanel).not.toContain('denyPermission(')
  })
})
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/stores/agent.test.ts src/components/agent/approval-trace-boundary.test.ts`

Expected: FAIL —— 只有统计时 `hasRunNotice` 为 false；`AgentRunPanel` 不含“本轮运行摘要”。

- [ ] **Step 4: 扩展 `hasRunNotice`**

把 `src/stores/agent.ts` 中现有 computed 改为：

```ts
  // 「运行通知区有东西要显示」的唯一判定：逐项对齐 AgentRunPanel 实际渲染的区块
  // （任务规划 / 警告 / 人工介入 / 错误 / run 摘要）。
  // run 摘要只看本轮是否有只读放行或审批事件：两者其一就值得对账。统计在 done
  // 后仍保留，所以这一条不能在 run 结束时消失；下一次 runAgent 的 _reset 或
  // clearRunNotice 会清掉。
  const hasRunNotice = computed(() =>
    !!intentionInfo.value
    || warnings.value.length > 0
    || hasHumanHelpRequest.value
    || !!errorMessage.value
    || runStats.value.readonlyAllowed > 0
    || runStats.value.requested > 0
  )
```

- [ ] **Step 5: 面板渲染摘要**

在 `AgentRunPanel.vue` 的 `<script setup>`（`latestHelp` 之后）增加：

```ts
// ── 本轮运行摘要（run 结束后对账，不提供任何决策动作） ──
const runStats = computed(() => agentStore.runStats)
const allowanceCount = computed(() => agentStore.readonlyAllowances.length)
const allowancesTruncated = computed(() => runStats.value.readonlyAllowed > allowanceCount.value)
// 运行中不展示：计数还在涨，读起来像最终结论；done 后统计仍在，摘要自然留下
const showRunSummary = computed(() =>
  !agentStore.isRunning
  && (runStats.value.readonlyAllowed + runStats.value.requested) > 0,
)
```

在模板末尾（错误块之后、`</div>` 之前）加入：

```vue
    <!-- 本轮运行摘要：只读放行与审批事件的本地累计，只陈述事实、不提供决策入口。
         统计在 done 后仍保留（审批卡会被清空），直到下一次 runAgent 或
         clearRunNotice；明细来自 readonly_allowed 事件，精确记录在 audit_log。 -->
    <div class="ap-card ap-summary" v-if="showRunSummary">
      <div class="ap-head">
        <svg class="ap-ic ap-summary-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 15l4-5 3 3 5-7"/></svg>
        <span class="ap-ttl">本轮运行摘要</span>
        <span class="ap-sp"></span>
        <span class="ap-summary-counts">
          自动放行 {{ runStats.readonlyAllowed }} 条 / 人工审批 {{ runStats.requested }} 条 / 拒绝 {{ runStats.denied }} 条
        </span>
      </div>
      <div class="ap-body ap-summary-body">
        <div class="ap-summary-line">
          批准 {{ runStats.approved }} 条<template v-if="runStats.expired"> · 超时 {{ runStats.expired }} 条</template>
        </div>
        <details v-if="allowanceCount" class="ap-summary-details">
          <summary>展开只读放行明细（{{ allowanceCount }} 条）</summary>
          <ul class="ap-summary-list">
            <li v-for="(a, i) in agentStore.readonlyAllowances" :key="`${a.ruleId}-${i}`">
              <code class="ap-code">{{ a.command }}</code>
              <span class="ap-summary-rule">{{ a.ruleId }}</span>
            </li>
          </ul>
          <p v-if="allowancesTruncated" class="ap-summary-note">
            仅展示最近 {{ allowanceCount }} 条；完整记录见运维轨迹中的审计。
          </p>
        </details>
      </div>
    </div>
```

在 `<style scoped>` 末尾加入：

```css
/* ── 本轮运行摘要 ── */
.ap-summary { background: var(--bg-elevated); border-color: var(--border-default); }
.ap-summary-accent { color: var(--status-success-text); }
.ap-summary-counts {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.ap-summary-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ap-summary-line { font-size: 11.5px; color: var(--text-secondary); }
.ap-summary-details summary {
  font-size: 11px;
  color: var(--text-tertiary);
  cursor: pointer;
}
.ap-summary-list {
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 140px;
  overflow-y: auto;
}
.ap-summary-list li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.ap-summary-list .ap-code {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ap-summary-rule {
  flex: none;
  font-size: 10.5px;
  color: var(--text-tertiary);
}
.ap-summary-note {
  margin: 6px 0 0;
  font-size: 10.5px;
  color: var(--text-tertiary);
}
```

- [ ] **Step 6: 运行前端测试与类型检查**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npx vitest run src/stores/agent.test.ts src/components/agent/approval-trace-boundary.test.ts src/styles/variables.test.ts
```

Expected: PASS（`variables.test.ts` 守住新样式只用既有令牌、不写裸色值）。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 7: 提交（仅在用户明确授权后）**

```bash
git add src/stores/agent.ts src/stores/agent.test.ts \
  src/components/agent/AgentRunPanel.vue src/components/agent/approval-trace-boundary.test.ts
git commit -m "feat(agent-ui): run 结束摘要展示只读放行与审批对账"
```

### Task 13: 审批审计闭环（grant / deny / expired 带 approval_key）

**Files:**
- Create: `backend/tests/test_approval_audit_detail.py`
- Modify: `backend/routes/agent.py`（grant / deny 的 `append_record_async` 增加 `detail`）
- Modify: `backend/agent/harness/reins/guards.py`（命令级过期 detail）
- Modify: `backend/agent/harness/reins/gate.py`（工具级过期 detail）

**Interfaces:**
- Consumes: Task 6 的 `format_audit_detail(fields) -> str` / `parse_audit_detail(result) -> dict`；Task 8 写入的 `requested` detail（其中 `approval_key` 是配对键）；`PermissionGate.approval_key_for_call(tool, args, bound)`、`grant_key_for(...)`、`deny_command(...)`。
- Produces:
  - grant 审计 detail：`source=approval;approval_key=<裸键>;scope=<once|session|always>;host=<主机或空>`
  - deny 审计 detail：`source=approval;approval_key=<裸键>`
  - 两条过期路径的 detail：`source=approval_timeout;approval_key=<裸键>`

**边界（不要多做）：**
- 不在 grant/deny 记录里写 `intent`。这两个端点没有 `RunContext`，意图只存在于发卡的那次 run 里；硬塞就得再造一份跨请求状态。`intent_drift` 统计只从 `requested` 记录取（spec §8.3 的“含命令级与工具级”指两条发卡路径，不是两条审批端点）。spec §8.1 的 `approved` 示例里带 `intent_drift=1` 只是示意；实际口径以发卡时的 `requested` 记录为准，不要求 grant/deny 复写。
- 不改 `result` 的人类可读前缀：`approval_expired:` 仍是过期记录的识别依据（`test_permission_approval_lifecycle.py` 按前缀筛选）。
- `approval_key` 必须用裸键（`tool:hash`），不是 `target_id` 的 `@host|tool:hash` —— `requested` 里写的是裸键，带前缀就永远配不上对。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_approval_audit_detail.py`：

```python
"""审批审计闭环：requested ↔ approved/denied/expired 按 approval_key 配对。

统计端点按 run_id + approval_key 计算等待时长；键写不进去只会得到一个永久的
null。这里守住三件事：授权、拒绝、兜底到期都把同一把键写进 detail，同时
既有人类可读 result 与 `approval_expired:` 前缀不被改动。
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import agent.harness.reins as reins_mod
import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.audit import parse_audit_detail
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import PermissionGate, tool_call_signature
from agent.harness.reins.guards import FilesystemGuard
from agent.tools import create_registry

CMD = "systemctl restart nginx"


@pytest.fixture(autouse=True)
def _no_file_io(monkeypatch):
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)


class _FakeGate:
    """只回答路由需要的三件事，避免测试碰 allowlist 文件与 run 表。"""

    KEY = "shell_exec:0123456789abcdef"

    def grant_key_for(self, tool, command=None, scope="once", run_id=""):
        return self.KEY

    def grant(self, *args, **kwargs):
        return self.KEY

    def deny_command(self, tool, command, run_id):
        return self.KEY


@pytest.fixture
def route_audits(monkeypatch):
    import routes.agent as agent_mod
    seen = []

    async def _rec(**kw):
        seen.append(kw)

    monkeypatch.setattr(agent_mod, "append_record_async", _rec)
    monkeypatch.setattr(agent_mod.approval_wait, "resolve_for_call",
                        lambda *a, **k: None)
    monkeypatch.setattr(
        "agent.harness.reins._get_global_permission_gate_sync", lambda: _FakeGate())
    return seen


def test_grant_audit_detail_can_pair_with_requested(route_audits):
    import routes.agent as agent_mod
    asyncio.run(agent_mod.grant_tool_permission(agent_mod.PermissionGrantRequest(
        tool="shell_exec", scope="always", command=CMD, run_id="run-pair-1")))

    rec = route_audits[-1]
    assert rec["conclusion"] == "approved"
    assert rec["run_id"] == "run-pair-1"
    assert rec["result"].startswith("scope=always"), "人类可读结果不能被 detail 顶掉"
    detail = parse_audit_detail(rec["detail"])
    assert detail["source"] == "approval"
    assert detail["approval_key"] == _FakeGate.KEY
    assert detail["scope"] == "always"


def test_deny_audit_detail_can_pair_with_requested(route_audits):
    import routes.agent as agent_mod
    asyncio.run(agent_mod.deny_command(agent_mod.DenyCommandRequest(
        tool="shell_exec", command=CMD, run_id="run-pair-2")))

    rec = route_audits[-1]
    assert rec["conclusion"] == "denied"
    detail = parse_audit_detail(rec["detail"])
    assert detail["source"] == "approval"
    assert detail["approval_key"] == _FakeGate.KEY


def _ctx() -> RunContext:
    return RunContext.new(task="测试", config=AgentConfig())


def _log(ctx, tool, args):
    ctx.tool_calls_log.append({
        "tool": tool, "args": args, "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


def _expire_pending(ctx):
    sig = tool_call_signature(ctx.tool_calls_log[-1])
    ctx.metadata["permission_pending"][sig] = (
        time.monotonic() - (reins_mod.approval_fallback_seconds() + 1))


def test_guard_expired_audit_carries_approval_key(monkeypatch):
    captured = []

    async def _rec(**kw):
        captured.append(kw)

    monkeypatch.setattr(_guards_mod, "append_record_async", _rec)
    gate = PermissionGate(saddle=SimpleNamespace(registry=create_registry()))
    guard = FilesystemGuard(permission_gate=gate)
    ctx = _ctx()
    _log(ctx, "shell_exec", {"command": CMD})
    asyncio.run(guard.check(ctx))
    _expire_pending(ctx)
    _log(ctx, "shell_exec", {"command": CMD})
    d = asyncio.run(guard.check(ctx))

    assert d.intercepted and any(e.kind == "permission_expired" for e in d.events)
    rec = [r for r in captured
           if str(r.get("result", "")).startswith("approval_expired")][0]
    detail = parse_audit_detail(rec["detail"])
    assert detail["source"] == "approval_timeout"
    assert detail["approval_key"] == gate._grant_key("shell_exec", CMD)


def test_gate_expired_audit_carries_approval_key(monkeypatch):
    captured = []

    async def _rec(**kw):
        captured.append(kw)

    monkeypatch.setattr(_gate_mod, "append_record_async", _rec)
    gate = PermissionGate(saddle=SimpleNamespace(registry=create_registry()))
    gate._confirmed_permanent = set()
    ctx = _ctx()
    args = {"action": "restart", "service": "nginx"}
    _log(ctx, "service_manage", args)
    asyncio.run(gate.check(ctx))
    _expire_pending(ctx)
    _log(ctx, "service_manage", args)
    d = asyncio.run(gate.check(ctx))

    assert d.intercepted and any(e.kind == "permission_expired" for e in d.events)
    rec = [r for r in captured
           if str(r.get("result", "")).startswith("approval_expired")][0]
    detail = parse_audit_detail(rec["detail"])
    assert detail["source"] == "approval_timeout"
    assert detail["approval_key"] == gate._grant_key("service_manage")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_approval_audit_detail.py -q`

Expected: FAIL —— 三条记录都没有 `detail` 键（grant/deny 是 `KeyError`，expired 是 `parse_audit_detail(None)`）。

- [ ] **Step 3: grant / deny 路由追加 detail**

`backend/routes/agent.py` 顶部导入改为：

```python
from agent.audit import append_record_async, format_audit_detail
```

grant 的 `append_record_async` 增加（保留全部现有参数与 `critical=True`，只加 `detail`）：

```python
        detail=format_audit_detail({
            "source": "approval",
            # 裸键：requested 审计与卡片都是这把键，target_id 的主机前缀会配不上
            "approval_key": grant_key,
            "scope": req.scope,
            "host": grant_host,
        }),
```

deny 的 `append_record_async` 增加：

```python
        detail=format_audit_detail({
            "source": "approval",
            "approval_key": denied_key,
        }),
```

不要动两处现有的 `run_id=`、`environment=`、`result=`；`test_audit_run_id_on_approvals_s204.py` 会逐块检查 `run_id=` 是否仍在同一个调用里。

- [ ] **Step 4: 两条过期路径追加 detail**

`backend/agent/harness/reins/guards.py` 导入增加 `format_audit_detail`（与 `append_record_async` 同一行来源），命令级过期分支：

```python
                            await append_record_async(
                                action=tool, conclusion="denied",
                                run_id=ctx.run_id, iteration=ctx.iteration,
                                target_type=_audit_target_type(ctx),
                                target_id=_audit_target_id(ctx),
                                environment=env,
                                command_raw=str(cmd),
                                result=f"approval_expired: {reason}",
                                detail=format_audit_detail({
                                    "source": "approval_timeout",
                                    "approval_key": self.permission_gate.approval_key_for_call(
                                        tool, args, bound),
                                }),
                            )
```

`backend/agent/harness/reins/gate.py` 同样导入后，工具级过期分支：

```python
            await append_record_async(
                action=tool, conclusion="denied",
                run_id=ctx.run_id, iteration=ctx.iteration,
                target_type=_audit_target_type(ctx),
                target_id=_audit_target_id(ctx),
                environment=environment,
                command_raw=str(bound or ""),
                result=f"approval_expired: {tool}",
                detail=format_audit_detail({
                    "source": "approval_timeout",
                    "approval_key": self.approval_key_for_call(tool, args, bound),
                }),
            )
```

- [ ] **Step 5: 运行新测试与审批审计回归**

Run:
```bash
cd backend && python -m pytest tests/test_approval_audit_detail.py tests/test_permission_approval_lifecycle.py tests/test_audit_run_id_on_approvals_s204.py tests/test_audit_coverage.py -q
```

Expected: PASS。

Run:
```bash
cd backend && python -m pytest tests/test_governance_env_and_audit_failclosed.py tests/test_allowlist_host_scope.py tests/test_grant_dead_run.py -q
```

Expected: PASS（grant 的写前审计 `critical=True` 与主机限域行为不变）。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add backend/routes/agent.py backend/agent/harness/reins/guards.py \
  backend/agent/harness/reins/gate.py backend/tests/test_approval_audit_detail.py
git commit -m "feat(audit): 审批授权/拒绝/超时记录携带 approval_key，可配对 requested"
```

### Task 14: 审批统计聚合与 `/audit/approval-stats`

**Files:**
- Modify: `backend/agent/audit.py`（`approval_stats(days)`）
- Modify: `backend/routes/audit.py`（`GET /audit/approval-stats`）
- Modify: `src/api/audit.ts`（`ApprovalStats` + `approvalStats()`）
- Test: `backend/tests/test_approval_stats.py`

**Interfaces:**
- Consumes: `audit_log` 的 `ts/environment/action/conclusion/command_raw/result/run_id`；Task 6 的 `parse_audit_detail`；Task 13 写入的 `approval_key`。
- Produces:
  - `audit.approval_stats(days: int = 7) -> dict`，结构见下；
  - `GET /api/audit/approval-stats?days=7`，`days` 限 1–90；
  - `src/api/audit.ts` 的 `ApprovalStats` 类型与 `auditApi.approvalStats(days?)`。

**统计口径（与 spec §8.3 逐条对齐）：**
- `requested` = `conclusion=requested`；`approved` = `permission_grant:*` 且 `approved`（手动登记 `permission_allow_manual:*` 不算）；`denied` = `permission_deny_command:*`；`expired` = `denied` 且 `result` 前缀 `approval_expired`；`blocked` = `conclusion=blocked` 或 `denied` 且 `result` 前缀 `prod_deny_no_allowlist:` / `permission_gate_missing:`；`readonly_auto` = `executed/failed` 且 detail `source=readonly_rule`（旧白名单不计）。
- 等待中位数只配 `requested` → `approved/denied`；过期不是“响应”，不参与。
- 无记录返回零值，不抛 500（表还没建与真空同口径）。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_approval_stats.py`：

```python
"""审批统计：计数口径、等待中位数配对、空窗口与参数边界。"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database as db
from agent import audit
from routes.audit import router as audit_router


def _ts(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.fixture
def wired(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    app = FastAPI()
    app.include_router(audit_router, prefix="/api")
    return TestClient(app), conn


def _insert(conn, *, ts, action, conclusion, result="", run_id="",
            command_raw="", environment="test"):
    conn.execute(
        "INSERT INTO audit_log (ts, environment, action, conclusion, command_raw, "
        "result, run_id, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, environment, action, conclusion, command_raw, result, run_id,
         f"h-{conn.total_changes}-{action}-{conclusion}"),
    )


def _detail(**fields) -> str:
    return audit.format_audit_detail(fields)


def test_counts_follow_spec_denominators(wired):
    client, conn = wired
    now = _ts(10)
    _insert(conn, ts=now, action="shell_exec", conclusion="requested",
            command_raw="systemctl restart nginx", run_id="r1",
            result=_detail(source="approval", approval_key="shell_exec:k1"))
    _insert(conn, ts=now, action="permission_grant:shell_exec",
            conclusion="approved", run_id="r1",
            result=_detail(source="approval", approval_key="shell_exec:k1"))
    # 手动登记永久授权不是一次“审批通过”
    _insert(conn, ts=now, action="permission_allow_manual:shell_exec",
            conclusion="approved")
    _insert(conn, ts=now, action="permission_deny_command:shell_exec",
            conclusion="denied", run_id="r2")
    _insert(conn, ts=now, action="shell_exec", conclusion="denied",
            run_id="r3", result="approval_expired: dangerous")
    _insert(conn, ts=now, action="shell_exec", conclusion="blocked",
            result="hardline: rm -rf /")
    _insert(conn, ts=now, action="shell_exec", conclusion="denied",
            result="prod_deny_no_allowlist: 未预授权")
    _insert(conn, ts=now, action="read_file", conclusion="executed",
            result=_detail(source="readonly_rule", rule="ro.verb.jq.v1"))
    _insert(conn, ts=now, action="read_file", conclusion="executed",
            result=_detail(source="legacy_safe_verb"))

    body = client.get("/api/audit/approval-stats?days=7").json()

    assert body["counts"] == {
        "requested": 1, "approved": 1, "denied": 1,
        "expired": 1, "blocked": 2, "readonly_auto": 1,
    }
    assert body["by_environment"] == {"test": 9}
    assert body["window_days"] == 7


def test_median_pairs_requested_with_first_response(wired):
    _, conn = wired
    _insert(conn, ts=_ts(120), action="shell_exec", conclusion="requested",
            run_id="r1", result=_detail(source="approval", approval_key="k1"))
    _insert(conn, ts=_ts(90), action="permission_grant:shell_exec",
            conclusion="approved", run_id="r1",
            result=_detail(source="approval", approval_key="k1"))
    _insert(conn, ts=_ts(600), action="shell_exec", conclusion="requested",
            run_id="r2", result=_detail(source="approval", approval_key="k2"))
    _insert(conn, ts=_ts(450), action="permission_deny_command:shell_exec",
            conclusion="denied", run_id="r2",
            result=_detail(source="approval", approval_key="k2"))
    # 过期不是响应：这对不参与中位数
    _insert(conn, ts=_ts(400), action="shell_exec", conclusion="requested",
            run_id="r3", result=_detail(source="approval", approval_key="k3"))
    _insert(conn, ts=_ts(50), action="shell_exec", conclusion="denied",
            run_id="r3", result="approval_expired: dangerous")

    body = audit.approval_stats(7)

    assert body["median_approval_seconds"] == 90.0


def test_top_commands_and_reasons_come_from_requested(wired):
    _, conn = wired
    for _ in range(2):
        _insert(conn, ts=_ts(10), action="shell_exec", conclusion="requested",
                command_raw="rm -f /tmp/x", run_id="r1",
                result=_detail(source="approval", reason="dangerous verb rm"))
    _insert(conn, ts=_ts(10), action="shell_exec", conclusion="requested",
            command_raw="systemctl restart nginx", run_id="r1",
            result=_detail(source="approval", reason="service restart"))

    body = audit.approval_stats(7)

    assert body["top_commands"][0] == {"command": "rm -f /tmp/x", "count": 2}
    assert body["top_reasons"][0] == {"reason": "dangerous verb rm", "count": 2}


def test_intent_drift_counts_both_guard_paths(wired):
    _, conn = wired
    _insert(conn, ts=_ts(10), action="shell_exec", conclusion="requested",
            run_id="r1", result=_detail(source="approval", intent_drift="1"))
    _insert(conn, ts=_ts(10), action="service_manage", conclusion="requested",
            run_id="r2", result=_detail(source="approval", intent_drift="1"))
    _insert(conn, ts=_ts(10), action="shell_exec", conclusion="requested",
            run_id="r3", result=_detail(source="approval"))

    assert audit.approval_stats(7)["intent_drift"] == 2


def test_empty_window_returns_zeroes(wired):
    body = audit.approval_stats(7)
    assert body["counts"] == {
        "requested": 0, "approved": 0, "denied": 0,
        "expired": 0, "blocked": 0, "readonly_auto": 0,
    }
    assert body["by_environment"] == {}
    assert body["top_commands"] == []
    assert body["top_reasons"] == []
    assert body["median_approval_seconds"] is None
    assert body["intent_drift"] == 0


def test_window_excludes_old_records(wired):
    _, conn = wired
    _insert(conn, ts=_ts(8 * 24 * 3600), action="shell_exec",
            conclusion="requested", result=_detail(source="approval"))
    assert audit.approval_stats(7)["counts"]["requested"] == 0


def test_route_validates_days(wired):
    client, _ = wired
    assert client.get("/api/audit/approval-stats?days=0").status_code == 422
    assert client.get("/api/audit/approval-stats?days=91").status_code == 422
    assert client.get("/api/audit/approval-stats?days=7").status_code == 200
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_approval_stats.py -q`

Expected: FAIL —— `approval_stats` 不存在、路由 404。

- [ ] **Step 3: 实现 `approval_stats`**

`backend/agent/audit.py` 顶部增加 `import statistics`，并把既有的
`from datetime import datetime, timezone` 改为：

```python
from datetime import datetime, timedelta, timezone
```

在 `count` 之后、`verify` 之前加入：

```python
# 与用户拒绝区分开的“系统拦下”：hardline 走 blocked 结论，prod 预登记缺失与
# 权限门缺失走 denied + 固定 result 前缀。
_BLOCKED_RESULT_PREFIXES = (
    "prod_deny_no_allowlist:",
    "permission_gate_missing:",
)


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _top(counter: dict[str, int], key: str) -> list[dict[str, Any]]:
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{key: name, "count": count} for name, count in ordered[:5]]


def approval_stats(days: int = 7) -> dict[str, Any]:
    """审批与只读放行的窗口统计（只读）。

    时间口径统一用 audit_log.ts（UTC ISO 串，字典序即时间序）。审批等待时长
    按 (run_id, approval_key) 配对 requested 与第一次 approved/denied；过期与
    released 都不是“用户响应”，不参与计算。配不上对的记录只计数。
    """
    window_days = max(1, min(90, int(days or 7)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    counts = {
        "requested": 0, "approved": 0, "denied": 0,
        "expired": 0, "blocked": 0, "readonly_auto": 0,
    }
    by_environment: dict[str, int] = {}
    command_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    drift = 0
    requested_at: dict[tuple[str, str], datetime] = {}
    responses: dict[tuple[str, str], list[datetime]] = {}
    try:
        conn = database._get_conn()
        rows = conn.execute(
            f"SELECT ts, environment, action, conclusion, command_raw, result, run_id "
            f"FROM {AUDIT_TABLE} WHERE ts >= ? ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
    except Exception as e:
        if _table_absent(e):
            rows = []
        else:
            logger.warning("审批统计读取失败: %s", e)
            raise
    for row in rows:
        ts_raw = str(row[0] or "")
        environment = str(row[1] or _DEFAULT_ENV)
        action = str(row[2] or "")
        conclusion = str(row[3] or "")
        command_raw = str(row[4] or "")
        result = str(row[5] or "")
        run_id = str(row[6] or "")
        detail = parse_audit_detail(result)

        by_environment[environment] = by_environment.get(environment, 0) + 1
        if detail.get("intent_drift") == "1":
            drift += 1

        if conclusion == "requested":
            counts["requested"] += 1
            if command_raw:
                command_counts[command_raw] = command_counts.get(command_raw, 0) + 1
            reason = detail.get("reason", "")
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

        approved_grant = (
            action.startswith("permission_grant:") and conclusion == "approved")
        user_deny = (
            action.startswith("permission_deny_command:") and conclusion == "denied")
        if approved_grant:
            counts["approved"] += 1
        if user_deny:
            counts["denied"] += 1
        if conclusion == "denied" and result.startswith("approval_expired"):
            counts["expired"] += 1
        if conclusion == "blocked" or (
                conclusion == "denied" and result.startswith(_BLOCKED_RESULT_PREFIXES)):
            counts["blocked"] += 1
        if conclusion in ("executed", "failed") and detail.get("source") == "readonly_rule":
            counts["readonly_auto"] += 1

        approval_key = detail.get("approval_key", "")
        if not run_id or not approval_key:
            continue
        parsed_ts = _parse_ts(ts_raw)
        if parsed_ts is None:
            continue
        pair = (run_id, approval_key)
        if conclusion == "requested":
            requested_at.setdefault(pair, parsed_ts)
        elif approved_grant or user_deny:
            responses.setdefault(pair, []).append(parsed_ts)

    waits: list[float] = []
    for pair, req_ts in requested_at.items():
        later = [t for t in responses.get(pair, []) if t >= req_ts]
        if later:
            waits.append((min(later) - req_ts).total_seconds())
    return {
        "window_days": window_days,
        "counts": counts,
        "by_environment": by_environment,
        "top_commands": _top(command_counts, "command"),
        "top_reasons": _top(reason_counts, "reason"),
        "median_approval_seconds": round(float(statistics.median(waits)), 1) if waits else None,
        "intent_drift": drift,
    }
```

- [ ] **Step 4: 路由**

`backend/routes/audit.py` 导入增加 `approval_stats`，并在 `/audit/records` 后加入：

```python
@router.get("/audit/approval-stats")
async def audit_approval_stats(days: int = Query(default=7, ge=1, le=90)):
    """审批与只读放行的窗口统计。

    参数边界交给 FastAPI（1–90 天）；聚合是同步 sqlite 扫，与 records/verify
    同样移入线程池，避免阻塞聊天 SSE。失败回 500，不返回一份零值冒充“没有审批”。
    """
    try:
        return await asyncio.to_thread(approval_stats, days)
    except Exception as e:
        logger.warning("审批统计读取失败: %s", e)
        raise HTTPException(status_code=500, detail="审批统计读取失败，请稍后重试") from e
```

- [ ] **Step 5: 前端 API 类型**

`src/api/audit.ts` 在 `AuditHealth` 之后增加：

```ts
/** 审批统计计数：分母与后端 approval_stats 一一对应 */
export interface ApprovalStatsCounts {
  requested: number
  approved: number
  denied: number
  expired: number
  blocked: number
  readonly_auto: number
}

export interface ApprovalStats {
  window_days: number
  counts: ApprovalStatsCounts
  by_environment: Record<string, number>
  top_commands: Array<{ command: string; count: number }>
  top_reasons: Array<{ reason: string; count: number }>
  median_approval_seconds: number | null
  intent_drift: number
}
```

`auditApi` 增加：

```ts
  /** 审批与只读放行统计（days 1–90，缺省 7） */
  approvalStats: (days = 7) =>
    request<ApprovalStats>(`/audit/approval-stats?days=${days}`),
```

- [ ] **Step 6: 运行测试与类型检查**

Run: `cd backend && python -m pytest tests/test_approval_stats.py tests/test_audit_detail.py tests/test_audit_read_honesty_s1101.py -q`

Expected: PASS。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 7: 提交（仅在用户明确授权后）**

```bash
git add backend/agent/audit.py backend/routes/audit.py \
  backend/tests/test_approval_stats.py src/api/audit.ts
git commit -m "feat(audit): 审批统计端点聚合请求/批准/拒绝/超时与只读放行"
```

### Task 15: 审计页审批概览

**Files:**
- Modify: `src/views/ssh/AuditView.vue`
- Test: `src/views/ssh/audit-overview.test.ts`（新建，`?raw` 源码守卫）

**Interfaces:**
- Consumes: Task 14 的 `auditApi.approvalStats(7)` 与 `ApprovalStats`；Task 7 已加的 `requested` 下拉选项。
- Produces: 事件流分段顶部的“审批概览”卡片；`refresh()` 同步刷新概览。

**展示口径：**
- 计数直接映射 `counts`，不另造指标。
- “只读占比” = `readonly_auto / (readonly_auto + requested)`，四舍五入到整数；分母为 0 时不显示百分比。它回答的是“命令决策里有多少被只读证明直接消化”，不冒充成功率。
- 概览拉取失败只让卡片缺席，**不写表格的 `error`**：一条附加统计拿不到，不能让整张审计表看起来加载失败（与 `refreshHealth` 同一条既有取舍）。

- [ ] **Step 1: 写失败测试**

创建 `src/views/ssh/audit-overview.test.ts`：

```ts
/**
 * 审计页审批概览的源码守卫。
 *
 * 这里没有 DOM 环境（vitest node），断言的是“卡片存在且字段来自后端统计”，
 * 以及“概览失败不污染表格错误条”这条边界。
 */
import { describe, expect, it } from 'vitest'
import auditView from '@/views/ssh/AuditView.vue?raw'

describe('审计页审批概览', () => {
  it('展示请求/批准/拒绝/超时/拦截计数与只读收益', () => {
    expect(auditView).toContain('审批概览')
    expect(auditView).toContain('readonly_auto')
    expect(auditView).toContain('intent_drift')
    expect(auditView).toContain('median_approval_seconds')
  })

  it('requested 是可选筛选结论', () => {
    expect(auditView).toContain('value="requested"')
  })

  it('概览读取失败不写表格的 error 通道', () => {
    const refreshStats = auditView.match(/async function refreshStats[\s\S]*?\n}/)?.[0] ?? ''
    expect(refreshStats).toContain('catch')
    expect(refreshStats).not.toContain('error.value')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /d/workspace/cKunAgentSSH && npx vitest run src/views/ssh/audit-overview.test.ts`

Expected: FAIL —— 模板里没有“审批概览”，也没有 `refreshStats`。

- [ ] **Step 3: 拉取统计**

`AuditView.vue` 的 import 增加 `type ApprovalStats`：

```ts
import {
  auditApi, toFriendlyMessage,
  type ApprovalStats, type AuditHealth, type AuditRecord, type AuditVerifyResult,
} from '@/api/client'
```

在 `healthState` 之后增加：

```ts
// 审批概览（近 7 天）。读失败只让卡片缺席 —— 与 health 同一取舍：附加统计的
// 故障不该把整张审计表标成“加载失败”。
const statsState = ref<ApprovalStats | null>(null)
const readonlyRate = computed(() => {
  if (!statsState.value) return null
  const auto = statsState.value.counts.readonly_auto
  const manual = statsState.value.counts.requested
  return auto + manual > 0 ? Math.round((auto / (auto + manual)) * 100) : null
})

async function refreshStats() {
  try {
    statsState.value = await auditApi.approvalStats(7)
  } catch {
    statsState.value = null
  }
}
```

`refresh()` 改为：

```ts
async function refresh() {
  await Promise.all([loadFirst(), verify(), refreshHealth(), refreshStats()])
}
```

- [ ] **Step 4: 渲染概览卡**

模板中，`audit-health-warn` 之后、`audit-filters` 之前插入：

```vue
    <!-- 审批概览：近 7 天的请求/结论与只读证明的收益。只读计数只认
         source=readonly_rule 的新规则，旧白名单不计入 —— 否则收益会被历史口径撑大。 -->
    <section v-if="statsState" class="audit-overview" aria-label="审批概览">
      <div class="ao-head">
        <span class="ao-title">审批概览</span>
        <span class="ao-sub">近 {{ statsState.window_days }} 天</span>
      </div>
      <div class="ao-counts">
        <span class="ao-stat"><b>{{ statsState.counts.requested }}</b> 次待审批</span>
        <span class="ao-stat ok"><b>{{ statsState.counts.approved }}</b> 次批准</span>
        <span class="ao-stat deny"><b>{{ statsState.counts.denied }}</b> 次拒绝</span>
        <span class="ao-stat warn"><b>{{ statsState.counts.expired }}</b> 次超时</span>
        <span class="ao-stat warn"><b>{{ statsState.counts.blocked }}</b> 次策略拦截</span>
        <span class="ao-stat ok" title="只读自动放行数 ÷（只读自动放行数 + 待审批数）">
          只读自动放行 <b>{{ statsState.counts.readonly_auto }}</b> 次<template v-if="readonlyRate !== null">（占命令决策 {{ readonlyRate }}%）</template>
        </span>
        <span class="ao-stat"><b>{{ statsState.intent_drift }}</b> 次意图漂移</span>
        <span class="ao-stat">
          等待中位数 <b>{{ statsState.median_approval_seconds === null ? '—' : statsState.median_approval_seconds + 's' }}</b>
        </span>
      </div>
      <div v-if="statsState.top_commands.length || statsState.top_reasons.length" class="ao-lists">
        <div v-if="statsState.top_commands.length" class="ao-list">
          <span class="ao-label">Top 命令</span>
          <span v-for="c in statsState.top_commands.slice(0, 3)" :key="c.command"
            class="ao-item" :title="c.command">{{ c.command }} <em>×{{ c.count }}</em></span>
        </div>
        <div v-if="statsState.top_reasons.length" class="ao-list">
          <span class="ao-label">Top 原因</span>
          <span v-for="r in statsState.top_reasons.slice(0, 3)" :key="r.reason"
            class="ao-item" :title="r.reason">{{ r.reason }} <em>×{{ r.count }}</em></span>
        </div>
      </div>
    </section>
```

样式追加到 `<style scoped>`（放在 `.audit-filters` 之前，便于阅读）：

```css
/* ── 审批概览 ── */
.audit-overview {
  margin: 12px 16px 0;
  padding: 10px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}
.ao-head { display: flex; align-items: baseline; gap: 8px; }
.ao-title { font-size: 12.5px; font-weight: 600; color: var(--text-primary); }
.ao-sub { font-size: 11px; color: var(--text-tertiary); }
.ao-counts { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.ao-stat { font-size: 11.5px; color: var(--text-secondary); }
.ao-stat b { color: var(--text-primary); font-weight: 600; }
.ao-stat.ok b { color: var(--status-success-text); }
.ao-stat.deny b { color: var(--status-error-text); }
.ao-stat.warn b { color: var(--status-warn-text); }
.ao-lists { display: flex; flex-direction: column; gap: 4px; }
.ao-list {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  font-size: 11px;
  color: var(--text-secondary);
}
.ao-label { flex: none; color: var(--text-tertiary); }
.ao-item {
  min-width: 0;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 6px;
  background: var(--bg-hover);
  border-radius: 4px;
}
.ao-item em { color: var(--text-tertiary); font-style: normal; }
```

- [ ] **Step 5: 运行前端测试与类型检查**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npx vitest run src/views/ssh/audit-overview.test.ts src/styles/variables.test.ts
```

Expected: PASS（样式只用既有令牌）。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 6: 提交（仅在用户明确授权后）**

```bash
git add src/views/ssh/AuditView.vue src/views/ssh/audit-overview.test.ts
git commit -m "feat(audit-ui): 审计页展示审批概览与只读放行收益"
```

### Task 16: 全量验证与手工走查

**Files:** 无新增（只跑验证；发现回归时回到对应任务修，不在本任务临时改口径）。

**Interfaces:**
- Consumes: Task 1–15 的全部产物。
- Produces: 一份“哪些自动化过了、哪些手工场景过了”的结论；没有通过项就不算完成。

- [ ] **Step 1: 后端全量测试**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npm run test:backend
```

Expected: PASS。若有失败，先判断是实现回归还是既有测试断言了旧语义（首卡 `denied`、旧白名单 source 等），修在对应任务的代码/测试里；不要用 `-k` 跳过。

- [ ] **Step 2: 前端全量测试与类型检查**

Run:
```bash
cd /d/workspace/cKunAgentSSH && npx vitest run
```

Expected: PASS。

Run: `cd /d/workspace/cKunAgentSSH && npm run typecheck`

Expected: PASS。

- [ ] **Step 3: 启动应用做手工走查**

Run: `cd /d/workspace/cKunAgentSSH && npm run dev`

按下列场景逐条走查（每步都要看到预期结果，不能只看“没报错”）：

1. **只读不再弹卡**：在已连接的测试机上提问“看看现在有哪些容器在跑”。`docker ps` 命中新增只读规则 `ro.sub.docker.v1`，不应出现审批卡；消息流末尾出现“本轮运行摘要”，自动放行计数 ≥ 1，展开能看到命令与 `ro.*` 规则 ID。再问“看看磁盘还有多少空间”（`df -h` 属旧白名单）：同样不弹卡，但**不产生只读摘要**——旧白名单不逐条发事件（spec §7.1），这是预期行为，不是漏记。
2. **写操作仍逐条审批**：提问“重启 nginx”。审批卡必须出现；卡片有“任务意图”行（类型/级别/只读预期），且没有意图漂移警告；批准后命令才执行。
3. **意图不授权（漂移只展示）**：先问“看看服务状态”，再让模型执行 `systemctl restart nginx`。卡片必须出现“任务预期只读，但这条命令会改变目标机状态”警告，且**仍然要手动批准**；不批准时命令不得执行。这是本轮最关键的负向验证。
4. **授权档位**：同一命令在测试机上点“本会话允许”后再次请求应免卡；切到生产主机后同一条命令必须重新弹卡（主机限域）。`always` 的按钮提示里能看到主机、TTL 与审批哈希。
5. **分组与批量**：制造两条不同命令的待批请求，ApprovalDock 出现按 run + 主机分组的标题；“全部批准本次”只在全部为 safe/moderate 且非 prod 时可用。
6. **审计闭环**：打开运维轨迹 → 事件流。筛选结论 `requested` 能看到首次发卡记录；概览卡显示请求/批准/拒绝/超时/拦截、只读自动放行、意图漂移与等待中位数；批准/拒绝后刷新，计数与等待中位数变化。
7. **生产预登记**：把环境切到 prod，危险命令在未预登记时被拒绝并提示“仅接受预授权 allowlist”，不出现可临时批准的卡片。

- [ ] **Step 4: 记录验证结果**

在任务收尾说明里列出：后端全量结果、前端全量结果、上述 7 个场景中哪些通过、哪些因环境（无 prod 主机 / 无 SSH 连接）未验证。未验证项必须显式写出，不能默认为通过。

本任务不产生新提交；若走查中发现需要改代码，回到对应任务补测试与实现，并按该任务的提交步骤处理（仍需用户明确授权后才 commit）。
