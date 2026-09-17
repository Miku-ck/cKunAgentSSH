"""
Saddle — 马鞍

工具执行承载 — 在工具执行前做沙箱检查，执行后生成 Undo 包。

与原 tools/__init__.py 的区别：
- 工具执行前置沙箱检查（原本在 hook 里）
- 每次执行生成 UndoAction（用于失败时回滚）
- 通过 ToolRegistry 协议调用，不直接依赖具体工具实现
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .contracts import NoOpUndo, RunContext, ToolCall, ToolResult, UndoAction

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 沙箱
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SandboxCheck:
    ok: bool
    reason: str = ""


class Sandbox:
    """
    沙箱 — 工具执行前的静态检查

    与 FilesystemGuard 拦截器的区别：
    - FilesystemGuard 是 Reins 策略层（可被用户覆盖/禁用）
    - Sandbox 是 Saddle 内置的安全底线（不可禁用）

    H13 修复 B4：shell 命令检查改用 safety.classify_command（与 FilesystemGuard 同源）
    原实现自带 _IRREVERSIBLE_PATTERNS 字符串匹配，与 safety.py 的正则+NFKC 规范化脱同步
    """

    def validate(self, call: ToolCall, ctx: RunContext) -> SandboxCheck:
        tool = call.name
        args = call.args

        # shell 命令检查 — H13 修复 B4：复用 safety.classify_command（与 FilesystemGuard 同源）
        if tool in ("shell_exec",):
            cmd = args.get("command") or args.get("shell", "")
            if isinstance(cmd, str) and cmd:
                from .safety import classify_command
                level, reason = classify_command(cmd)
                if level == "hardline":
                    return SandboxCheck(
                        ok=False,
                        reason=f"沙箱拦截不可逆命令: {reason}",
                    )
                # dangerous 交给 FilesystemGuard + PermissionGate 处理（已有逻辑）
                # safe → 放行

        return SandboxCheck(ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Undo 实现
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ShellUndo:
    """Shell 命令撤销 — reverse 时经 ElectronBridge 在【同一 SSH 会话】执行反向命令

    C-review C4：记录原命令的执行会话 —— 原实现 reverse 恒在本地
    create_subprocess_shell 执行，SSH 模式下原命令在远程服务器、回滚却在
    后端主机"打错机器"（误删本地同名路径）。

    仅远程（2026-09 收敛）：shell_exec 无 SSH 会话即 fail-closed 拒绝，本机
    不存在被撤销的执行。而 undo 在 PRE_TOOL_CALL 构建、工具 execute 拒绝后
    会触发 apply_reverse — 若保留本地回滚分支，拒绝路径的反向命令（如 rmdir）
    反而会在后端主机真实执行，绕过 shell_exec 的本机执行禁令。因此无
    ssh_session_id 时一律跳过（本机从未执行，也无需回滚）。
    """
    command: str
    reverse_command: Optional[str]  # None 表示无反向命令
    ssh_session_id: str = ""

    async def apply_reverse(self) -> None:
        if not self.reverse_command:
            return
        if not self.ssh_session_id:
            # 原命令从未执行（无 SSH 会话 → shell_exec 已拒绝），无需回滚，
            # 且绝不允许在后端主机执行反向命令。
            logger.info("ShellUndo 跳过：无 SSH 会话，原命令未执行 (command=%s)", self.command)
            return
        try:
            from agent.electron_bridge import ElectronBridge
            bridge = ElectronBridge()
            if not bridge.is_available():
                logger.warning("ShellUndo 跳过：Electron Bridge 不可用 (command=%s)", self.command)
                return
            await bridge.exec_ssh_command(
                ssh_session_id=self.ssh_session_id,
                command=self.reverse_command,
                timeout=10.0,
            )
        except Exception as e:
            logger.warning("ShellUndo 远程反向失败 %s: %s", self.reverse_command, e)


@dataclass
class FileContentUndo:
    """文件写/编辑撤销 — 执行前快照原内容，reverse 时经 SSH 会话还原（或删除新建文件）。

    P3 预变更快照：文件类突变工具（file_write / file_edit）的回滚从 "NoOp" 升级为
    内容级还原，与 shell_exec 的反向命令回滚并列构成 Undo 链。

    仅限远程（2026-09 收敛）：file_write/file_edit 无 SSH 会话即被拒绝，
    不存在本地还原路径；还原经 ElectronBridge 下发到同一 SSH 会话。
    snapshot_b64=None 且 existed=False 表示原文件不存在，reverse 时删除新建的文件。
    """

    path: str
    ssh_session_id: str
    snapshot_b64: Optional[str] = None  # 原内容 base64；None 表示原文件不存在
    existed: bool = True

    async def apply_reverse(self) -> None:
        try:
            if not self.ssh_session_id:
                logger.info("FileContentUndo 跳过：无 SSH 会话，原写入未发生 %s", self.path)
                return
            from agent.electron_bridge import ElectronBridge
            bridge = ElectronBridge()
            if not bridge.is_available():
                logger.warning("FileContentUndo 跳过：Electron Bridge 不可用 %s", self.path)
                return
            from agent.tools.builtin.ops_common import base64_restore_cmd, file_delete_cmd
            if self.existed and self.snapshot_b64 is not None:
                # 模板与 ops_file 写入共用（base64 无需转义；路径 shlex.quote 防护）
                cmd = base64_restore_cmd(self.path, self.snapshot_b64)
            else:
                cmd = file_delete_cmd(self.path)
            await bridge.exec_ssh_command(
                ssh_session_id=self.ssh_session_id,
                command=cmd,
                timeout=10.0,
            )
        except Exception as e:
            logger.warning("FileContentUndo 还原失败 %s: %s", self.path, e)


# ──────────────────────────────────────────────────────────────────────────────
# Undo 构建器
# ──────────────────────────────────────────────────────────────────────────────

class UndoBuilder:
    """根据工具调用构建 Undo 包"""

    @staticmethod
    async def build(call: ToolCall, ctx: RunContext) -> UndoAction:
        tool = call.name
        args = call.args
        run_id = ctx.run_id

        if tool == "shell_exec":
            cmd = args.get("command") or args.get("shell", "")
            reverse = UndoBuilder._shell_reverse(cmd)
            # 仅远程执行（shell_exec 无会话即拒绝）；撤销目标 = 同一 SSH 会话
            return ShellUndo(
                command=cmd,
                reverse_command=reverse,
                ssh_session_id=getattr(ctx, "ssh_session_id", "") or "",
            )

        if tool in ("file_write", "file_edit"):
            # P3 预变更快照：文件突变工具执行前快照原内容，供回滚时还原
            return await UndoBuilder._build_file_undo(tool, args, ctx)

        # 只读工具 → NoOp
        return NoOpUndo(reason="readonly")

    @staticmethod
    async def _build_file_undo(tool: str, args: dict, ctx: RunContext) -> UndoAction:
        """为 file_write / file_edit 构建内容级回滚包（执行前快照，仅限 SSH 会话）。"""
        path = args.get("path") or ""
        if not path:
            return NoOpUndo(reason="file_no_path")
        sess = getattr(ctx, "ssh_session_id", "") or ""
        if not sess:
            # 工具 execute 会 fail-closed 拒绝，本机无副作用可撤销
            return NoOpUndo(reason="file_write_requires_ssh")

        # 执行前快照远端原内容（base64 回传，规避尾部哨兵歧义）
        from agent.tools.builtin.ops_common import run_headless, strip_remote_tail
        ok, out = await run_headless(ctx, f"base64 {shlex.quote(path)}", 30)
        if ok:
            body = strip_remote_tail(out)
            try:
                data = base64.b64decode(body)
            except Exception:
                return NoOpUndo(reason="file_snapshot_decode_fail")
            return FileContentUndo(
                path=path, ssh_session_id=sess,
                snapshot_b64=base64.b64encode(data).decode("ascii"),
                existed=True,
            )
        # 快照失败：区分"桥/会话不可用"（真实写入也会失败 → NoOp）
        # 与其余（如 base64 因文件不存在非 0 退出 → 原文件不存在，回滚=删除）
        if "拒绝执行" in out or "fail-closed" in out or "未就绪" in out:
            return NoOpUndo(reason="file_snapshot_unavailable")
        return FileContentUndo(
            path=path, ssh_session_id=sess,
            snapshot_b64=None, existed=False,
        )

    # B-5 加固：反向命令参数禁止的 shell 元字符（含换行/回车 — 原字符串切片实现
    # 可让 `mkdir 'a; rm -rf /tmp'` 类 payload 混入反向命令）
    _SHELL_META_RE = re.compile(r"[;&|<>`${}\n\r]")

    @classmethod
    def _shell_reverse(cls, cmd: str) -> Optional[str]:
        """尝试推导 shell 命令的反向命令（B-5 加固）

        - shlex 解析原命令，仅当 token 形态精确匹配时才派生：
          mkdir/rmdir/touch → 恰好 [verb, path]；mv/cp → 恰好 [verb, a, b]
        - 路径参数含任何 shell 元字符（含换行）→ 拒绝
        - 派生出的反向命令再过一次 safety.classify_command，非 safe → 返回 None
        """
        try:
            tokens = shlex.split(cmd.strip(), posix=True)
        except ValueError:
            return None
        if not tokens:
            return None
        verb = tokens[0].rsplit("/", 1)[-1]
        args = tokens[1:]

        reverse: Optional[str] = None
        if verb in ("mkdir", "rmdir", "touch") and len(args) == 1:
            path = args[0]
            if not cls._SHELL_META_RE.search(path):
                reverse_verb = {"mkdir": "rmdir", "rmdir": "mkdir", "touch": "rm"}[verb]
                reverse = f"{reverse_verb} {path}"
        elif verb in ("mv", "cp") and len(args) == 2:
            a, b = args
            if not cls._SHELL_META_RE.search(a) and not cls._SHELL_META_RE.search(b):
                if verb == "mv":
                    reverse = f"mv {b} {a}"
                else:
                    # cp 的反向 = 删除目标副本（源文件不受影响）
                    reverse = f"rm {b}"
        else:
            # 其他动词/形态无通用反向
            return None

        if reverse is None:
            return None
        # 派生命令二次分类：非 safe 一律放弃（防止派生出危险反向命令）
        # C-review C1：默认审批策略下 mv 被判操作动词（dangerous），但 mv 反向
        # （两个显式路径互换，token 形态已精确约束 + 无元字符）结构安全 — 豁免；
        # 其余动词（rm/rmdir/mkdir 派生）仍要求分类 safe
        from .safety import classify_command
        level, _ = classify_command(reverse)
        if level == "hardline":
            return None
        if level == "dangerous" and not reverse.startswith("mv "):
            return None
        return reverse


# ──────────────────────────────────────────────────────────────────────────────
# Saddle
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Saddle:
    """
    马鞍 — 工具执行承载

    职责：
    1. 沙箱检查（Sandbox.validate）
    2. 构建 Undo 包（UndoBuilder.build，shell 命令反向推导）
    3. 调用 ToolRegistry.execute（自动从 RunContext 构造 ToolContext）
    4. 工具失败时可选自动回滚
    """

    registry: Any  # ToolRegistry 协议实现（原 tools.ToolRegistry）
    sandbox: Sandbox = field(default_factory=Sandbox)
    undo_builder: UndoBuilder = field(default_factory=UndoBuilder)

    async def execute(
        self,
        call: ToolCall,
        ctx: RunContext,
        auto_rollback: bool = False,
    ) -> tuple[ToolResult, UndoAction]:
        """
        执行工具 — 返回 (result, undo_action)

        auto_rollback=True 时，工具失败自动执行 undo_action.apply_reverse()
        """
        # 1. 沙箱检查
        check = self.sandbox.validate(call, ctx)
        if not check.ok:
            return ToolResult(success=False, error=check.reason), NoOpUndo(reason="sandbox_blocked")

        # 2. 构建 Undo 包（执行前）+ 磁盘备份
        undo = await self.undo_builder.build(call, ctx)
        ctx.push_undo(undo)

        # 3. 执行工具 — 从 RunContext 构造 ToolContext
        tool_ctx = self._build_tool_context(ctx)
        start = time.time()
        # H13 修复 C3：用 retry_with_backoff 包装 registry.execute，瞬时错误自动重试
        # 注意：仅对"抛异常"的瞬时错误生效，工具返回 ToolResult(success=False) 不触发重试
        from .stirrups import retry_with_backoff, ErrorClassifier

        # 审查 MINOR 修复：仅对幂等工具重试。原实现对所有工具
        # （shell_exec / spawn_subagent 等）统一重试 —
        # 非幂等工具在"副作用已发生但结果丢失"的瞬时错误下重跑，会造成
        # 命令双执行、子 Agent 双启动。非幂等工具只执行一次。
        tool_instance = self.registry.get(call.name) if hasattr(self.registry, "get") else None
        idempotent = bool(tool_instance is not None
                          and getattr(tool_instance.meta, "idempotent", False))
        if idempotent:
            raw_result, retry_err = await retry_with_backoff(
                func=lambda: self.registry.execute(call.name, call.args, tool_ctx),
                classifier=ErrorClassifier(),
                max_retries=2,
                base_delay=0.5,
            )
        else:
            try:
                raw_result = await self.registry.execute(call.name, call.args, tool_ctx)
                retry_err = None
            except Exception as e:
                raw_result = None
                retry_err = str(e)
        if raw_result is None:
            # 重试耗尽或非瞬时错误（FATAL/FORBIDDEN 立即返回）
            result = ToolResult(
                success=False,
                error=retry_err or "工具执行失败（重试耗尽）",
                priority="high",
            )
        else:
            # 失败时自动提升优先级（仅当工具未主动声明时）
            priority = raw_result.priority
            if not raw_result.success and priority == "normal":
                priority = "high"
            # 原 ToolResult 输出字段名是 output/error，统一映射
            result = ToolResult(
                success=raw_result.success,
                output=raw_result.output if raw_result.success else "",
                error=raw_result.error or "",
                duration_ms=0,
                priority=priority,
                pending_delivery=raw_result.pending_delivery,
            )
        result.duration_ms = int((time.time() - start) * 1000)

        # 4. 失败时自动回滚
        # 可见终端模式：pending_delivery 非空表示命令尚未执行（等待前端写入可见终端），
        # 不应触发回滚（无副作用可撤销），交由 Driver 挂起等待。
        if not result.success and not result.pending_delivery and auto_rollback:
            try:
                await undo.apply_reverse()
                ctx.undo_stack.pop()  # 已执行，从栈中移除
            except Exception as e:
                logger.warning("自动回滚失败: %s", e)

        return result, undo

    def _build_tool_context(self, ctx: RunContext) -> Any:
        """从 RunContext 构造 ToolContext（原 agent.tools.ToolContext）"""
        from agent.tools import ToolContext
        return ToolContext(
            mode=ctx.mode,
            conversation_id=ctx.conversation_id,
            agent_run_id=ctx.run_id,
            ssh_session_id=ctx.ssh_session_id,
            # 可见终端模式 + 目标环境 — 透传给 shell_exec 决定是否交付到可见终端
            visible_terminal=getattr(ctx, "visible_terminal", False),
            environment=getattr(ctx, "environment", "test"),
            # 审查修复（P2 E6）：透传 cost_tracker — 子 Agent 通过
            # spawn_subagent 调用时需要上报 token 用量。
            # cost_tracker 在 harness.run() 中被写入 ctx.metadata["cost_tracker"]
            cost_tracker=ctx.metadata.get("cost_tracker"),
        )

    def schemas(self) -> list[dict[str, Any]]:
        """代理 registry.schemas()"""
        return self.registry.schemas()
