"""有界日志清理工具 — log_cleanup。

动机：`rm -rf /var/log/app` 原先被判 hardline（/var 在敏感目录表里），既没有
审批出口也无法通过 allowlist 放行。"清理应用日志"因此变成只能人工执行的
任务。本工具把这类需求收敛成一条**参数有界**的能力：

  - 根目录只允许 /tmp、/var/tmp、/var/log 的应用子目录、agent 托管日志目录；
  - 只清理目标目录**第一层**的普通文件（-maxdepth 1 -type f），不递归下钻；
  - 系统/审计日志（audit/journal/private/sssd 及认证、内核、引导日志）拒绝；
  - 默认 dry_run 只打印匹配结果，删除必须显式 dry_run=False。

命令模板与远端白名单（scripts/deploy/agent-exec.py 的 RE_LOG_CLEANUP）一一对齐，
由 backend/tests/test_remote_whitelist_contract.py 跨端校验。远端还会对根目录做
realpath 包含校验，封堵 symlink 指向 /etc 之类的绕过。
"""
import posixpath
import re
import shlex

from agent.tools import BaseTool, ToolMeta, ToolResult
from agent.tools.builtin.ops_common import (
    drop_sentinel_lines,
    parse_int_arg,
    run_headless,
)

_CLEANUP_META = ToolMeta(
    risk_level="dangerous",
    reversible=False,
    idempotent=False,
    timeout=60.0,
)

# 允许在任意层级清理的临时目录（maxdepth 1 已把删除范围限制在第一层普通文件）
_TMP_ROOTS = ("/tmp", "/var/tmp")
# agent 自管的远端日志目录（与 safety._AGENT_MANAGED_LOG_DIRS 同源）
_MANAGED_LOG_ROOT = "/.trae-cn-server/manager-logs"
_VAR_LOG_ROOT = "/var/log"

# /var/log 顶层禁止清理的条目：系统/审计/认证/内核/引导日志。
# 应用自己的子目录（nginx、myapp…）仍允许，但仍需审批。
_PROTECTED_LOG_ENTRIES = frozenset({
    "audit", "journal", "private", "sssd",
    "auth.log", "secure", "syslog", "messages",
    "kern.log", "daemon.log", "user.log",
    "cron", "cron.log", "wtmp", "btmp", "lastlog", "faillog",
})

_PATTERN_RE = re.compile(r"^[A-Za-z0-9*?._-]{1,128}\Z")
_ROOT_FORBIDDEN = frozenset("*?[]{};|&<>$`\"'\\")
_MAX_DAYS = 3650


def _is_managed_log_root(path: str) -> bool:
    """路径是否落在 agent 托管日志目录子树内（目录本身或其下任意层级）。

    实际部署路径是 /home/<user>/.trae-cn-server/manager-logs，不能要求前缀
    恰好从 / 开始。边界匹配与 safety._is_agent_log_path 同源：
    manager-logs-backup 这类兄弟名字不命中。
    """
    idx = path.find(_MANAGED_LOG_ROOT)
    while idx != -1:
        end = idx + len(_MANAGED_LOG_ROOT)
        if end == len(path) or path[end] == "/":
            return True
        idx = path.find(_MANAGED_LOG_ROOT, idx + 1)
    return False


def log_cleanup_cmd(root: str, pattern: str, days: int, delete: bool) -> str:
    """log_cleanup 远端模板（试运行只 -print，删除时 -delete -print）。

    与 agent-exec.py 的 RE_LOG_CLEANUP 精确对齐。`-print` 始终保留，让删除
    结果可核对（不是盲删）。
    """
    action = "-delete -print" if delete else "-print"
    return (
        f"find {shlex.quote(root)} -maxdepth 1 -type f "
        f"-name {shlex.quote(pattern)} -mtime +{int(days)} {action}"
    )


def _root_reject(root) -> str | None:
    if not isinstance(root, str) or not root.strip():
        return "日志目录不能为空"
    if root != root.strip():
        return "日志目录首尾不能包含空白"
    if any(ord(c) < 32 for c in root):
        return "日志目录包含非法控制字符"
    if not root.startswith("/"):
        return "日志目录必须是绝对路径"
    if any(c in _ROOT_FORBIDDEN for c in root):
        return "日志目录必须是字面路径，不能包含通配符或 shell 元字符"
    norm = posixpath.normpath(root)
    if norm != root:
        return "日志目录必须是规范化路径（不能含 . / .. / 重复斜杠 / 尾斜杠）"

    for allowed in _TMP_ROOTS:
        if norm == allowed or norm.startswith(allowed + "/"):
            return None
    if _is_managed_log_root(norm):
        return None
    if norm.startswith(_VAR_LOG_ROOT + "/"):
        first = norm[len(_VAR_LOG_ROOT) + 1:].split("/", 1)[0]
        if first in _PROTECTED_LOG_ENTRIES:
            return f"/var/log/{first} 属于系统或审计日志，不允许通过 log_cleanup 清理"
        return None
    return ("日志目录不在允许范围内：仅支持 /tmp、/var/tmp、/var/log 下的应用子目录，"
            "以及 agent 托管日志目录")


def _pattern_reject(pattern) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "文件名模式不能为空"
    if pattern.startswith("-"):
        return "文件名模式不能以 - 开头"
    if not _PATTERN_RE.fullmatch(pattern):
        return "文件名模式只允许字母、数字、* ? . _ -，且不超过 128 个字符"
    return None


def _days_value(raw) -> tuple[int | None, str]:
    days, err = parse_int_arg(raw, "days")
    if err:
        return None, err
    if not 1 <= days <= _MAX_DAYS:
        return None, f"days 必须在 1-{_MAX_DAYS} 之间（收到 {days}）"
    return days, ""


def _dry_run_value(raw) -> tuple[bool | None, str]:
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True, ""
        if low in ("false", "0", "no"):
            return False, ""
    return None, f"dry_run 需为布尔值，收到 {raw!r}"


def _validated(args: dict) -> tuple[dict | None, str]:
    """校验参数并返回规范化视图；bound_command 与 execute 共用（防漂移）。"""
    if not isinstance(args, dict):
        return None, "参数必须是对象"
    root = args.get("root")
    bad = _root_reject(root)
    if bad:
        return None, bad
    pattern = args["pattern"] if "pattern" in args else "*.log"
    bad = _pattern_reject(pattern)
    if bad:
        return None, bad
    days_raw = args["days"] if "days" in args else 30
    days, err = _days_value(days_raw)
    if err:
        return None, err
    dry_raw = args["dry_run"] if "dry_run" in args else True
    dry_run, err = _dry_run_value(dry_raw)
    if err:
        return None, err
    return {"root": root, "pattern": pattern, "days": days, "dry_run": dry_run}, ""


class LogCleanupTool(BaseTool):
    name = "log_cleanup"
    description = (
        "按目录清理旧日志文件：只清理指定目录第一层的普通文件，支持保留天数和"
        "文件名模式；默认仅试运行预览，确认后再显式删除。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": (
                    "要清理的日志目录绝对路径（允许 /tmp、/var/tmp、"
                    "/var/log 下的应用子目录、agent 托管日志目录）"
                ),
            },
            "pattern": {
                "type": "string",
                "description": "文件名模式（如 *.log，默认 *.log；不含路径分隔符）",
                "default": "*.log",
            },
            "days": {
                "type": "integer",
                "description": "只清理最后修改时间超过 N 天的文件（1-3650，默认 30）",
                "default": 30,
            },
            "dry_run": {
                "type": "boolean",
                "description": "true=只列出匹配文件（默认）；false=实际删除",
                "default": True,
            },
        },
        "required": ["root"],
        "additionalProperties": False,
    }
    meta = _CLEANUP_META

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        """按真实模板参与内容判定与授权键：删除形态必须与手敲 find -delete 同罪。"""
        spec, _err = _validated(args if isinstance(args, dict) else {})
        if spec is None:
            return None
        return log_cleanup_cmd(
            spec["root"], spec["pattern"], spec["days"], not spec["dry_run"])

    @classmethod
    def is_preview_call(cls, args: dict) -> bool:
        """本次调用是否为只读试运行（dry_run=true 且参数通过校验）。

        与 bound_command 共用 _validated：参数口径只此一份，预览判定不会在
        「模板合法但 dry_run 解析失败」这类边界上和真实执行分叉。参数非法时
        返回 False —— 按工具级 dangerous 照常审批（fail-closed）。
        """
        spec, _err = _validated(args if isinstance(args, dict) else {})
        return bool(spec and spec["dry_run"])

    async def execute(self, context, root="", pattern="*.log", days=30, dry_run=True):
        spec, err = _validated({
            "root": root, "pattern": pattern, "days": days, "dry_run": dry_run,
        })
        if spec is None:
            return ToolResult(False, "", err)

        delete = not spec["dry_run"]
        command = log_cleanup_cmd(
            spec["root"], spec["pattern"], spec["days"], delete)
        ok, output = await run_headless(context, command, timeout=60)
        if not ok:
            return ToolResult(False, output, f"日志清理命令执行失败: {spec['root']}")

        paths = drop_sentinel_lines(output)
        body = "\n".join(paths) if paths else "（没有匹配的文件）"
        if delete:
            return ToolResult(
                True,
                f"已删除 {len(paths)} 个文件（目录: {spec['root']}）\n{body}",
            )
        return ToolResult(
            True,
            f"试运行：匹配到 {len(paths)} 个文件（尚未删除）\n{body}",
            priority="low",
        )
