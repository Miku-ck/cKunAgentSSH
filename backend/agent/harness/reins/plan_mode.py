"""计划模式（Plan Mode）—— 只读强制 + 纪律注入

## 为什么需要这个模块

主流工具里 plan 不是「一个选项」或「一段提示词」，而是**权限状态 + 可评审制品 +
显式批准门**三者。本仓库的制品与注入已由 `agent/workflow.py` 承担（`spec.md` /
`plan.md` / `goal.md` 落盘 + `hydrate_for_session` 渲染注入段），但那个模块自述
「不做验收机…阶段推进与成功判定交由模型 + 用户决定，本模块不越权」——
也就是说：**plan 阶段没有任何机制阻止 agent 直接动手**。

`plan_write` 的 description 里那句「应等待用户批准计划后再动手执行」是**自觉，不是机制**。
本模块补的就是这一层：把「当前处于 plan 阶段」从一个提示词约定升级成**会被强制执行的
权限状态**。

对齐 Claude Code 的两条官方口径（`code.claude.com/docs/en/permission-modes`）：

- 「Read-only tools run as in default mode. **File edits are never auto-approved in
  plan mode, even when an allow rule matches.**」
- 「Permission prompts still apply the same as default mode — plan mode is about
  deferring edits, **not loosening safety**.」

后一条是本模块的边界：它只**收紧**，从不放松。解除 plan 模式后，既有的
`FilesystemGuard` / `PermissionGate` 照常运行，本模块不参与授权。

## 关键陷阱：`risk_level` 不是只读/可写分类

`ToolMeta.risk_level` 是**危害**分类，不是**可变性**分类。三个实测反例：

| 工具 | risk_level | 真实性质 |
|---|---|---|
| `todo_write` | safe | 写状态（任务清单） |
| `spawn_subagent` | safe | **可间接执行任何东西** → 只读约束的逃逸口 |
| `shell_exec` | moderate | 读/写取决于命令本身 |

所以「plan 模式 = 只放行 risk_level=='safe'」是错的：它放过 `spawn_subagent`
（逃逸），又挡住 `cat /var/log/x`（只读但 shell_exec 是 moderate）。

同理 `safety._SAFE_VERBS` 也不能直接复用 —— 那是「免审批」口径，含 `tee`（写文件）、
`curl`/`wget`（下载落盘）、`find`（`-delete`/`-exec`）、`awk`（`system()`）、
`journalctl --vacuum-files`、`dmesg -C`、`hostname <name>`。

> **plan 模式的承诺（「什么都没改」）比审批门（「用户同意了」）更强，
> 所以它的白名单必须是审批白名单的严格子集，且失败方向是拒绝。**

`test_plan_mode.py` 里有一条断言钉住这个包含关系，防止日后两张表各自漂移。

## 两个注入点，各管一件事

- `PlanModeGuard` @ `PRE_TOOL_CALL`（priority 6，早于三个会弹审批卡的拦截器：
  `ClarificationInterceptor` 7 / `PermissionGate` 8 / `FilesystemGuard` 10）：
  硬拦截。`Reins.check()` 按 priority 升序、首个 `intercepted=True` 即返回，所以
  plan 模式下的写操作应当是**直接拒绝**，而不是先弹一张审批卡让用户点。
- `PlanDisciplineRein` @ `PRE_ITERATION`（priority 12）：每轮注入纪律块。
  选这个点是因为 `driver.py` 每轮先 `messages = bridle.assemble(ctx)` 再
  `messages.extend(decision.inject_messages)`，而 **`messages` 是局部量、不写回 ctx**，
  所以每轮注入不会累积上下文（与「目标每轮注入」用的是同一机制）。

## 为什么纠偏文案写在 `intercept_reason` 而不是 `inject_messages`

读 `driver.py` 的 `_execute_tool_calls` 可知：`PRE_TOOL_CALL` 的**拦截分支不消费
`inject_messages`**（它只做 `result_text = f"工具调用被拦截: {intercept_reason}"` 然后
回灌给模型）；`inject_messages` 只在 `PRE_ITERATION` / `POST_ITERATION` /
`POST_TOOL_CALL` 三个点被消费。所以拒绝时要说的话必须自带在 `intercept_reason` 里，
且要写全「为什么不行 + 现在该做什么」。

## 未启用时零影响

两个拦截器都先看 `ctx.metadata["permission_mode"]`；不是 `plan` 就返回
**全默认的 `ReinDecision()`**（逐字段零差异）。`test_plan_mode.py` 有一条回归专门断言
这一点 —— 没开计划模式时，行为必须与引入本模块之前完全一致。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..contracts import ReinDecision, ReinPoint, RunContext, TraceEvent
from ..safety import classify_command, classify_constructed_command
from .core import MODE_KEY, _COMMAND_EXEC_TOOLS, command_from_args, extract_bound_command

logger = logging.getLogger(__name__)

# MODE_KEY（ctx.metadata 权限模式键）单一来源见 core；值等于 MODE_PLAN 时启用本模块
MODE_PLAN = "plan"

# ── 工具级白名单：本身不改变目标机状态 ──────────────────────────────────────
# 只放「读」与「记录计划本身」两类。刻意**不含**：
#   spawn_subagent  —— risk_level=safe 但能间接执行任意命令，是只读约束的逃逸口
#   memory_add / memory_invalidate / block_update —— 写长期记忆（durable state）
#   file_write / file_edit / job_run / job_kill / schedule_op / shell_exec —— 会改状态
#   service_manage  —— 按 action 分流，status 只读而 stop/restart 是写；
#                      为不引入工具内部知识，此处整只拒掉，改由 shell_exec 的
#                      `systemctl status <name>` 走命令级白名单（文案里会告诉模型）
#
# todo_write 是唯一被放行的「写」工具：它写的是**计划自身的表示**（任务清单，
# 也就是 Claude Code 在 plan 模式下允许的 TodoWrite），不是目标机的状态。
PLAN_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # 文件/日志读取
    "file_read", "file_glob", "file_grep", "read_image", "log_read",
    # 远程观测
    "sys_overview", "process_inspect", "network_probe",
    # 作业观测
    "job_list", "job_output",
    # 外部只读
    "web_search", "web_fetch",
    # 自身只读 / 计划表示
    "load_skill", "memory_search", "todo_write",
    # 计划工件本身：与 todo_write 同一类，写的是"计划怎么表达"，不是目标机的状态。
    # 这三条必须在白名单里 —— PLAN_DISCIPLINE 每轮都命令模型「调研完成后用 plan_write
    # 输出有序步骤」，而本表原本不含它们：实测 classify_plan_action("plan_write", …)
    # 直接回「在计划模式下不可用」，于是计划模式永远产不出计划（模型照命令再调一次
    # 就是重复调用，第三次被 LoopDetector terminate 掉整个 run）。spec/goal 同一条路径。
    "plan_write", "spec_write", "goal_write",
})

# ── 命令级白名单：严格子集，逐项都是「读了不会改」 ──────────────────────────
# 判定口径不是「够不够用」，而是「有没有反例」。剔除的都是有写侧路的动词。
PLAN_READ_ONLY_VERBS: frozenset[str] = frozenset({
    # 文件/目录读取
    "ls", "ll", "dir", "cat", "head", "tail", "wc", "file", "stat", "tree", "du", "df",
    "grep", "egrep", "fgrep", "rg", "cut", "sort", "uniq", "tr", "diff", "column",
    "strings", "od", "xxd", "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha1sum", "sha256sum", "cksum", "test",
    # 系统状态读取
    "ps", "uptime", "who", "whoami", "w", "id", "uname", "which", "whereis", "type",
    "pwd", "printenv", "free", "vmstat", "iostat", "lsof", "netstat", "ss",
    "pgrep", "pidof", "echo", "printf", "true", "false", "sleep",
})
# 刻意剔除（附反例，改动前先想清楚这一列）：
#   tee                       写文件
#   curl / wget               下载落盘 / 外发数据
#   awk                       system()、print > file
#   sed                       -i 原地改、e 执行
#   journalctl                --vacuum-* 删日志、--rotate、--flush
#   dmesg                     -C / -c 清缓冲区
#   find                      -delete / -exec / -fprint（下方按 flag 单独放行）
#   top / less / more         交互式，挂了终端也拿不到有用输出
#   date                      -s 改系统时间
#   ifconfig / hostname       带参即改（下方按「有无额外参数」单独放行）
#   env / export / set        环境变量洗白（env X=y cmd 可执行任意命令）
#   systemctl / service / ... 按子命令分流（下方子命令表白名单）
#   sudo / xargs / sh / bash / python / ssh   —— 一律拒，见 _FORBIDDEN_VERBS

# 结构性禁止：出现即拒，不看作「动词」
_FORBIDDEN_VERBS: frozenset[str] = frozenset({
    "sudo", "su", "doas", "xargs", "env", "eval", "exec", "source",
    "sh", "bash", "zsh", "dash", "ksh", "csh", "fish",
    "python", "python3", "perl", "ruby", "node", "php",
    "ssh", "scp", "sftp", "rsync", "nc", "ncat", "socat", "telnet",
})

# 子命令白名单：仅当第二个 token 在集合内才算只读。
# 与 safety._SAFE_VERB_SUBCOMMANDS 的差别：剔除 apt / apt-get / dpkg —— `apt update`
# 会改写包索引（是写操作），不该出现在「保证无副作用」的口径里。
PLAN_VERB_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "systemctl": frozenset({"status", "show", "is-active", "is-enabled", "is-failed",
                            "list-units", "list-unit-files", "list-sockets",
                            "list-timers", "cat", "version"}),
    "service": frozenset({"status", "--status-all"}),
    "ip": frozenset({"addr", "address", "link", "route", "neigh", "saddr", "maddr", "-V"}),
    "kubectl": frozenset({"get", "describe", "logs", "explain", "top", "version",
                          "cluster-info", "api-resources"}),
    "git": frozenset({"status", "log", "diff", "show", "branch", "tag", "remote",
                      "ls-files", "rev-parse", "shortlog", "blame", "describe"}),
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info"}),
    "helm": frozenset({"list", "get", "status", "history", "template"}),
}

# 子命令白名单之下仍需挡掉的深动词（safety._DANGEROUS_SUBACTIONS 同义）
_DANGEROUS_SUBACTIONS: dict[str, frozenset[str]] = {
    "ip": frozenset({"set", "del", "flush", "down", "add"}),
    "git": frozenset({"checkout", "reset"}),
}

# 带写副作用的 flag：命中即拒
_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                       "-fprint", "-fprintf", "-fls"}),
    "sort": frozenset({"-o", "--output"}),
    "journalctl": frozenset({"--vacuum-time", "--vacuum-size", "--vacuum-files",
                             "--rotate", "--flush", "--sync", "--relinquish-var"}),
    "dmesg": frozenset({"-C", "--clear", "-c", "--read-clear"}),
    "date": frozenset({"-s", "--set"}),
}
# 仅「无额外参数」时只读的动词
_NO_ARGS_ONLY_VERBS: frozenset[str] = frozenset({"hostname", "ifconfig"})

# 段分隔符：管道 / 逻辑连接 / 分号 / 换行。逐段取首 token 判动词。
_RE_SEGMENT_SPLIT = re.compile(r"\|\||&&|[|;\n]")
_RE_CMD_SUBST = re.compile(r"\$\(|`")
_RE_REDIRECT = re.compile(r"(?<![&>])>>?")

PLAN_DISCIPLINE = (
    "[计划模式] 当前处于只读阶段：可以读文件、查状态、跑只读命令，但不能改任何东西。\n"
    "· 禁止：写文件、改配置、重启/启停服务、安装卸载、改权限、删改数据；"
    "这些调用会被直接拒绝，写「始终允许」也没用。\n"
    "· 被拒时不要重试同一条命令或换等价写法，改用只读手段取证"
    "（status / show / 查日志 / 读配置）。\n"
    "· 调研完成后，把方案写成计划：用 plan_write 输出有序步骤"
    "（每步写清改什么、用什么命令、怎么验证），可配合 todo_write 建任务清单。\n"
    "· 计划要等用户批准后才动手。批准前不要宣称任务已完成。"
)


# ── 判定 ────────────────────────────────────────────────────────────────────


def plan_mode_active(ctx: RunContext) -> bool:
    """当前 run 是否处于计划模式。未设置该键 = 非计划模式（默认路径零影响）。"""
    meta = getattr(ctx, "metadata", None) or {}
    return str(meta.get(MODE_KEY) or "").strip().lower() == MODE_PLAN


def _scrub_quoted(command: str) -> str:
    """把引号内的内容替换成等长占位，避免把 `echo 'a>b'` 的重定向符号当操作符。"""
    out: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            out.append(" " if ch != quote else ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    # 占位后引号本身也一并清掉，省得干扰 token 切分
    return re.sub(r"['\"]", " ", "".join(out))


def _find_write_redirect(command: str) -> str:
    """返回第一个「会写文件」的重定向原文；没有则空串。

    `> /dev/null` 与 `2>&1`（fd 复制）不算写 —— 前者丢弃输出，后者只在 fd 间搬运。
    其余 `>` / `>>` 一律算写：目标文件是新建还是覆盖都要落盘。
    """
    scrubbed = _scrub_quoted(command)
    for m in _RE_REDIRECT.finditer(scrubbed):
        rest = scrubbed[m.end():].lstrip()
        if rest.startswith("&"):
            continue  # 2>&1 之类的 fd 复制
        token = rest.split()[0] if rest.split() else ""
        token = token.strip("'\"")
        if token == "/dev/null":
            continue
        return m.group() + (rest[:len(token)] if token else "")
    return ""


def _segments(command: str) -> list[list[str]]:
    """按段切分并 tokenize；切不出 token 的段丢给调用方按空段处理。"""
    import shlex

    segs: list[list[str]] = []
    for raw in _RE_SEGMENT_SPLIT.split(command):
        raw = raw.strip()
        if not raw:
            continue
        try:
            segs.append(shlex.split(raw, posix=True))
        except ValueError:
            segs.append(raw.split())
    return segs


def _check_verb(tokens: list[str]) -> str:
    """检查单个段的动词与参数；返回拒绝原因，空串表示这一段只读。"""
    if not tokens:
        return ""
    verb = tokens[0].rsplit("/", 1)[-1]  # 允许 /bin/ls 这类绝对路径写法

    if verb in _FORBIDDEN_VERBS:
        return f"`{verb}` 在计划模式下禁用（可能执行任意命令或提权）"

    if verb in _NO_ARGS_ONLY_VERBS:
        if len(tokens) > 2 or (len(tokens) == 2 and not tokens[1].startswith("-")):
            return f"`{verb}` 带参数即可能修改配置，计划模式下只允许无参查询"
        return ""

    if verb in PLAN_VERB_SUBCOMMANDS:
        sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if not sub or sub not in PLAN_VERB_SUBCOMMANDS[verb]:
            return (f"`{verb}` 只允许只读子命令"
                    f"（{'/'.join(sorted(PLAN_VERB_SUBCOMMANDS[verb]))}），`{sub or '∅'}` 不在其中")
        if sub in _DANGEROUS_SUBACTIONS.get(verb, frozenset()):
            return f"`{verb} {sub}` 会改变状态"
        return ""

    write_flags = _WRITE_FLAGS.get(verb)
    if write_flags:
        hit = next((t for t in tokens[1:] if t.split("=")[0] in write_flags), "")
        if hit:
            return f"`{verb} {hit}` 带写副作用"
        # find/journalctl/dmesg/sort/date 未命中写 flag 即视为只读，继续放行

    if verb in PLAN_READ_ONLY_VERBS or write_flags:
        return ""

    return f"`{verb}` 不在计划模式的只读白名单里"


def classify_plan_action(
    tool: str,
    args: Any,
    registry: Any = None,
) -> tuple[bool, str]:
    """判断一次工具调用在计划模式下是否放行。返回 `(allowed, reason)`。

    **fail-closed**：无法确认只读的一律拒绝。新增工具默认在拒绝侧，必须先显式
    进白名单才会被放行 —— 这是刻意的，反过来会随工具增加而静默漏放。
    """
    if not tool:
        return False, "工具名为空，无法判定，按只读约束拒绝"

    if tool in _COMMAND_EXEC_TOOLS:
        cmd = command_from_args(args)
        if not cmd or not cmd.strip():
            return False, "命令为空，无法判定，按只读约束拒绝"
        return classify_plan_command(cmd)

    if tool not in PLAN_READ_ONLY_TOOLS:
        return False, (
            f"`{tool}` 在计划模式下不可用：它可能改变目标机状态。"
            "若只是查询，改用只读工具（file_read / log_read / sys_overview 等）"
            "或 shell_exec 跑只读命令（如 systemctl status <name>）。"
        )

    # 白名单工具若自带"本次真实执行命令"（BaseTool.bound_command），一样要过内容级判定
    bound, _free_form = extract_bound_command(registry, tool, args)
    if bound:
        return classify_plan_bound_command(bound)
    return True, ""


def classify_plan_bound_command(command: str) -> tuple[bool, str]:
    """白名单工具自带的客户端模板：动词由代码写死，不套「未知动词默认审批」，也不查计划动词表。

    实测原实现对模板走了 `classify_plan_command`：`file_read`（明确在白名单里）的模板是
    `base64 <path>`，`base64` 不在 `PLAN_READ_ONLY_VERBS`、也不在 safety 的只读动词表
    ⇒ 每一次计划模式读文件都回「命令被判为 dangerous（unknown command, default approval
    (base64)）」—— 只读阶段连读都读不了。默认审批那条规则的动机是"模型手敲的未知动词
    不得免审"，而模板的动词由不得模型选，safety 自己对这个场景也有现成口径
    （`classify_constructed_command`）。

    放松的只有"动词由谁选"这一层：路径级判定一条不少（凭据读取、写落点、命令替换、
    输出重定向），所以 `base64 /etc/shadow` 仍然拒。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False, "命令为空，按只读约束拒绝"

    if _RE_CMD_SUBST.search(cmd):
        return False, "命令含命令替换（$(...) 或反引号），无法确认只读"

    redirect = _find_write_redirect(cmd)
    if redirect:
        return False, f"输出重定向 `{redirect}` 会写文件，计划模式下不允许落盘"

    level, reason = classify_constructed_command(cmd)
    if level != "safe":
        return False, f"命令被判为 {level}（{reason or '未说明'}），计划模式只允许只读排查"

    return True, ""


def classify_plan_command(command: str) -> tuple[bool, str]:
    """命令级判定：只读白名单 + 无输出重定向 + 无命令替换，且通过 safety 既有分类。

    要求 `safety.classify_command` 返回 safe 是**必要不充分**：它负责敏感读取
    （`cat /etc/shadow`）、管道内危险段等既有规则，而本次读的承诺比它更强，
    所以还要再叠一层严格白名单。两层都必须过。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False, "命令为空，按只读约束拒绝"

    if _RE_CMD_SUBST.search(cmd):
        return False, "命令含命令替换（$(...) 或反引号），无法确认只读"

    redirect = _find_write_redirect(cmd)
    if redirect:
        return False, f"输出重定向 `{redirect}` 会写文件，计划模式下不允许落盘"

    # 先过 safety：敏感读取 / 硬阻断 / 已知危险模式都在那里
    level, reason = classify_command(cmd)
    if level != "safe":
        return False, f"命令被判为 {level}（{reason or '未说明'}），计划模式只允许只读排查"

    for tokens in _segments(cmd):
        why = _check_verb(tokens)
        if why:
            return False, f"{why}，计划模式只允许只读排查"

    return True, ""


# ── 拦截器 ──────────────────────────────────────────────────────────────────


class PlanModeGuard:
    """计划模式硬拦截：只读之外一律拒绝（含写文件、改服务、装包、改权限）。

    priority 选 6 是为了**早于所有会弹审批卡的拦截器**（`ClarificationInterceptor` 7、
    `PermissionGate` 8、`FilesystemGuard` 10）：`Reins.check()` 首个
    `intercepted=True` 即返回，plan 模式下的写操作应当直接拒绝，而不是先弹一张
    审批卡让用户点 —— 用户点了「始终允许」也不该在计划阶段生效
    （对齐 Claude Code「even when an allow rule matches」）。

    `safety_critical = True` 复用既有的异常 fail-closed 语义：本拦截器抛异常时
    按拦截处理，而不是跳过检查放行。
    """

    point = ReinPoint.PRE_TOOL_CALL
    # 取 6 而不是 5：`LoopDetector` 也是 5，同值时顺序由 `defaults.py` 里
    # `interceptors` 列表的 append 顺序隐式决定（`list.sort` 是稳定排序）。
    # 显式错开一位，把「先防死循环、再判只读」变成契约，而不是依赖注册顺序。
    priority = 6
    safety_critical = True

    def __init__(self, saddle: Optional[Any] = None) -> None:
        self.saddle = saddle

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not plan_mode_active(ctx):
            # 未启用计划模式：逐字段零差异的默认决策，行为与引入本模块之前一致
            return ReinDecision()
        if not ctx.tool_calls_log:
            return ReinDecision()

        last = ctx.tool_calls_log[-1]
        tool = str(last.get("tool") or "")
        args = last.get("args") or {}
        registry = _registry(ctx, self.saddle)

        try:
            allowed, reason = classify_plan_action(tool, args, registry)
        except Exception as e:  # 判定本身出问题不能变成放行
            logger.warning("plan_mode 判定异常，按拒绝处理: %s", e, exc_info=True)
            allowed, reason = False, f"只读判定异常（{type(e).__name__}），按只读约束拒绝"

        if allowed:
            return ReinDecision()

        # 纠偏文案必须自带在 intercept_reason 里：Driver 在 PRE_TOOL_CALL 的拦截分支
        # 只把它拼成 `工具调用被拦截: {reason}` 回灌给模型，不消费 inject_messages。
        full = (f"{reason}\n{PLAN_DISCIPLINE.splitlines()[0]}\n"
                f"{chr(10).join(PLAN_DISCIPLINE.splitlines()[1:])}")
        return ReinDecision(
            intercepted=True,
            intercept_reason=full,
            events=[TraceEvent(
                kind="plan_mode_block",
                source="reins.plan_mode",
                run_id=ctx.run_id,
                iteration=ctx.iteration,
                payload={"tool": tool, "reason": reason},
            )],
        )


class PlanDisciplineRein:
    """计划模式纪律：每轮注入一次纪律块。

    `PRE_ITERATION` 的注入不会累积上下文 —— Driver 每轮先
    `messages = bridle.assemble(ctx)` 再 `extend(inject_messages)`，而 `messages`
    是局部量、不写回 `ctx`。与「目标每轮注入」用的是同一机制。
    """

    point = ReinPoint.PRE_ITERATION
    priority = 12

    async def check(self, ctx: RunContext) -> ReinDecision:
        if not plan_mode_active(ctx):
            return ReinDecision()
        return ReinDecision(inject_messages=[{"role": "user", "content": PLAN_DISCIPLINE}])


def _registry(ctx: RunContext, saddle: Any) -> Any:
    """取 per-run registry：ctx.metadata 优先，回落构造函数注入的 saddle。

    与 `gate.PermissionGate._lookup_meta` 同口径 —— 进程级单例的 saddle 字段会被
    并发 run 覆盖，所以 per-run 的引用要从 ctx 里拿。
    """
    per_run = (getattr(ctx, "metadata", None) or {}).get("saddle")
    owner = per_run or saddle
    return getattr(owner, "registry", None)
