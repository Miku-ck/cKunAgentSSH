"""
Shell 执行工具 — SSH 模式专属

安全约束: 30 秒超时，非破坏性命令自主执行
"""
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import exec_remote, parse_int_arg
from agent.pending_delivery import PendingDeliveries


def _compute_requires_confirm(command: str, environment: str) -> bool:
    """计算可见终端模式下该命令是否需要用户确认。

    规则：非 safe 命令（dangerous/hardline）或生产环境（prod）都需要确认。
    """
    from agent.harness.safety import classify_command
    level, _reason = classify_command(command)
    return (level != "safe") or (environment == "prod")


class ShellExecTool(BaseTool):
    name = "shell_exec"
    description = (
        "执行 shell 命令并返回输出。超时 1-120 秒（默认 30）。"
        "多段探测/排查命令用 ; 串联而非 &&——du/find 遇无权限路径 exit 非零属预期，"
        "&& 会中断后续段；exit 非零不等于连接异常，先看已捕获的输出再下结论。"
        "注意退出码是整行最后一条命令的：探测/统计类命令链可结尾追加 ; true 收敛为 0，"
        "避免权限噪声误报失败；关键命令不要追加。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数（1-120，默认 30）"},
        },
        "required": ["command"],
    }
    meta = ToolMeta(risk_level="moderate", reversible=False, timeout=30.0, mode_scope=["ssh"])

    async def execute(self, context, command, timeout=30):
        # 超时钳制（1-120 秒）；非数字回可读提示而不是裸 ValueError
        parsed, err = parse_int_arg(timeout, "timeout", 30)
        if err:
            return ToolResult(False, "", err)
        timeout = max(1, min(parsed, 120))
        # 分支顺序：
        #   1. 可见终端模式 + 存在 SSH 会话 → 交付到可见终端（不 headless 执行）
        #   2. SSH 会话 → 原有远程执行（ElectronBridge）
        #   3. 否则 → fail-closed：缺少 SSH 会话，拒绝在后端主机本机执行
        #      （与 ops_common.run_headless 一致的 fail-closed 策略，杜绝静默本机降级；
        #       本工具不再保留任何本地子进程分支）
        visible = getattr(context, "visible_terminal", False)
        if visible and context.ssh_session_id:
            return self._execute_visible(context, command)
        if context.ssh_session_id:
            return await self._execute_remote(context.ssh_session_id, command, timeout)
        return ToolResult(
            False,
            "",
            "拒绝执行（fail-closed）：缺少 SSH 会话，shell_exec 仅在 SSH 会话内执行，"
            "不会降级到后端主机本机执行。请确认已建立 SSH 连接。",
        )

    def _execute_visible(self, context, command: str) -> ToolResult:
        """可见终端模式：登记待交付命令，返回 pending_delivery 让 Driver 挂起等待。

        不在此处执行命令（headless 关闭），命令由前端写入真实 PTY/SSH 会话后，
        经 POST /api/agent/tool-result 回传结果恢复循环。
        """
        environment = getattr(context, "environment", "test")
        requires_confirm = _compute_requires_confirm(command, environment)
        pending = PendingDeliveries.create(
            command=command,
            session_id=context.ssh_session_id,
            requires_confirm=requires_confirm,
            run_id=getattr(context, "agent_run_id", ""),
        )
        return ToolResult(
            success=False,
            output="",
            priority="high",
            pending_delivery={
                "tool_id": pending.tool_id,
                "command": command,
                "session_id": pending.session_id,
                "requires_confirm": requires_confirm,
            },
        )

    async def _execute_remote(
        self, ssh_session_id: str, command: str, timeout: int
    ) -> ToolResult:
        result = await exec_remote(ssh_session_id, command, timeout)
        if result is None:
            return ToolResult(
                False,
                "",
                "远程执行未就绪：Electron Bridge 未配置（ELECTRON_BRIDGE_TOKEN 缺失）",
            )
        success, output, exit_code, err = result
        if err:
            # bridge 调用抛异常：错误信息落在 error 字段（下游 build_error_feedback 呈现）
            return ToolResult(False, "", err)
        if success:
            return ToolResult(True, output, priority="low")
        return ToolResult(False, output, f"exit_code={exit_code}")

