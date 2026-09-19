"""服务管理工具 — SSH 模式专属

基于固定的 systemctl 模板管理系统服务（status/start/stop/restart）。
安全约束：action 取枚举值，name 严格校验后经 shlex.quote 转义，绝不拼接任意 shell。
"""
import re
import shlex

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import run_headless

_ACTIONS = ("status", "start", "stop", "restart")

# 破坏性动作：stop/restart 会改变/中断服务运行状态
_DESTRUCTIVE_ACTIONS = {"stop", "restart"}
# 严格 name 白名单：字母数字、短横线、下划线、点
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# 非 systemd / systemctl 不可用的提示（不以破坏性命令兜底）
_NO_SYSTEMCTL_MSG = (
    "[!] 目标未提供可用的 systemctl（非 systemd 环境或 systemctl 不存在），"
    "无法执行 systemctl 操作。请确认目标系统是否使用 systemd，或换用其他运维方式。"
)


def service_cmd(action: str, name: str) -> str:
    """service_manage 远端模板：固定 systemctl 动词 + 校验后的服务名。

    契约源：与远端白名单执行器（scripts/deploy/agent-exec.py）对齐，
    由 backend/tests/test_remote_whitelist_contract.py 跨端校验。
    """
    return f"systemctl {action} {shlex.quote(name)}"


class ServiceManageTool(BaseTool):
    name = "service_manage"
    description = "通过 systemctl 管理系统服务（status/start/stop/restart）。仅用于 ssh 模式。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "对服务执行的操作",
            },
            "name": {
                "type": "string",
                "description": "服务名称（仅允许字母数字、-、_、.）",
            },
        },
        "required": ["action", "name"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        timeout=30.0,
        mode_scope=["ssh"],
        # 问题 5：stop/restart 在 prod 环境升级为 dangerous，每次需要显式审批
        # （原 moderate + session 级授权：首次审批后同 session 内免审，生产风险偏高）
        destructive_actions=frozenset(_DESTRUCTIVE_ACTIONS),
    )

    def _validate(self, action: str, name: str) -> str | None:
        """校验参数；合法返回 None，否则返回错误信息。"""
        if action not in _ACTIONS:
            return f"非法 action: {action!r}，仅支持 {', '.join(_ACTIONS)}"
        if not name or not isinstance(name, str):
            return "服务名称不能为空"
        if not _NAME_RE.match(name):
            return f"非法服务名称: {name!r}，仅允许字母数字、-、_、."
        return None

    async def execute(self, context, action: str, name: str) -> ToolResult:
        err = self._validate(action, name)
        if err:
            return ToolResult(False, "", err)

        command = service_cmd(action, name)

        success, output = await run_headless(context, command, 30)

        is_destructive = action in _DESTRUCTIVE_ACTIONS
        # 破坏性标记：在输出开头明确表明该操作为破坏性操作（权限门控另由 harness 处理）
        marker = "[destructive=true, 破坏性操作]"
        if is_destructive and output and not output.startswith(marker):
            output = f"{marker}\n{output}"

        # 非 systemd / systemctl 不可用兜底：给出友好说明，而不凭空执行破坏命令
        if not success:
            low = output.lower()
            if "not found" in low or "no such file" in low:
                notice = _NO_SYSTEMCTL_MSG if not is_destructive else (
                    f"[!] {_NO_SYSTEMCTL_MSG}"
                )
                output = (f"{marker}\n{notice}\n{output}" if is_destructive
                          else f"[!] {notice}\n{output}")
            return ToolResult(False, output, "systemctl 执行失败")

        return ToolResult(True, output, priority="low")