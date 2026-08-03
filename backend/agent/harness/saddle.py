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
import os
import time
import shutil
import logging
import hashlib
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from .contracts import NoOpUndo, RunContext, ToolCall, ToolResult, UndoAction

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 磁盘备份目录 — Undo 包同时写入磁盘，进程崩溃后可恢复
# ──────────────────────────────────────────────────────────────────────────────

_BACKUP_DIR_ENV = "CKUN_AGENT_BACKUP_DIR"
_DEFAULT_BACKUP_DIRNAME = ".agent-backup"


def _get_backup_root() -> str:
    """获取备份根目录（环境变量优先，否则用系统临时目录）"""
    env_dir = os.environ.get(_BACKUP_DIR_ENV)
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except OSError:
            pass
    # 回退到系统临时目录下的 ckun-agent-backup
    fallback = os.path.join(tempfile.gettempdir(), "ckun-agent-backup")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return fallback


def _backup_file(abs_path: str, run_id: str) -> Optional[str]:
    """
    将文件备份到磁盘，返回备份文件路径。
    - 文件不存在时返回 None（表示"原本不存在"）
    - 备份文件命名: <backup_root>/<run_id>/<sha256(abs_path)>_<basename>
    - 进程崩溃后可通过扫描 <backup_root>/<run_id>/ 恢复
    """
    if not os.path.exists(abs_path):
        return None
    try:
        backup_root = _get_backup_root()
        run_dir = os.path.join(backup_root, run_id)
        os.makedirs(run_dir, exist_ok=True)
        # 用 sha256(abs_path) 避免路径冲突
        path_hash = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
        backup_name = f"{path_hash}_{os.path.basename(abs_path)}"
        backup_path = os.path.join(run_dir, backup_name)
        shutil.copy2(abs_path, backup_path)
        # 同时写入元数据（原路径）
        meta_path = backup_path + ".meta"
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(abs_path)
        return backup_path
    except Exception as e:
        logger.warning("磁盘备份失败 %s: %s", abs_path, e)
        return None


def _restore_from_backup(backup_path: str, abs_path: str) -> bool:
    """从备份恢复文件到 abs_path"""
    try:
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        shutil.copy2(backup_path, abs_path)
        return True
    except Exception as e:
        logger.warning("从备份恢复失败 %s -> %s: %s", backup_path, abs_path, e)
        return False


def cleanup_run_backups(run_id: str) -> None:
    """运行成功完成后清理该 run 的所有备份"""
    try:
        backup_root = _get_backup_root()
        run_dir = os.path.join(backup_root, run_id)
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)
    except Exception as e:
        logger.warning("清理备份失败 %s: %s", run_id, e)


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

    @staticmethod
    def _check_path_safe(path: str, workspace: str) -> SandboxCheck:
        """统一路径安全检查，与 file_ops._resolve_path_safe 使用同一套逻辑"""
        if not workspace:
            return SandboxCheck(ok=True)
        try:
            full_path = os.path.realpath(os.path.join(workspace, path) if not os.path.isabs(path) else path)
            real_workspace = os.path.realpath(workspace)
            common = os.path.commonpath([full_path, real_workspace])
            if common != real_workspace:
                return SandboxCheck(ok=False, reason=f"路径越界: {path}")
            return SandboxCheck(ok=True)
        except Exception as e:
            return SandboxCheck(ok=False, reason=f"路径解析失败: {e}")

    def validate(self, call: ToolCall, ctx: RunContext) -> SandboxCheck:
        tool = call.name
        args = call.args

        # 路径检查 — 使用与 file_ops._resolve_path_safe 一致的 realpath + commonpath
        if tool in ("write_file", "read_file", "list_dir", "patch_file", "delete_file", "move_file"):
            path = args.get("file_path") or args.get("path") or args.get("source", "")
            if not path:
                return SandboxCheck(ok=True)
            check = self._check_path_safe(path, ctx.workspace_path)
            if not check.ok:
                return check
            # move_file 还要检查 destination
            if tool == "move_file":
                dst = args.get("destination", "")
                if dst:
                    dst_check = self._check_path_safe(dst, ctx.workspace_path)
                    if not dst_check.ok:
                        return dst_check
            return check

        # shell 命令检查 — H13 修复 B4：复用 safety.classify_command（与 FilesystemGuard 同源）
        # H14 修复：同时覆盖 local_exec（IDE 模式命令执行工具）
        if tool in ("shell_exec", "local_exec"):
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
class FileWriteUndo:
    """文件写入撤销 — 保存原内容，reverse 时恢复

    同时维护内存备份和磁盘备份：
    - 内存备份：进程正常时快速恢复
    - 磁盘备份：进程崩溃/断电后可手动恢复
    """
    abs_path: str
    original_content: Optional[str]  # None 表示文件原本不存在
    existed: bool
    backup_path: Optional[str] = None  # 磁盘备份路径
    run_id: str = ""

    async def apply_reverse(self) -> None:
        try:
            if not self.existed:
                # 原本不存在 → 删除
                if os.path.exists(self.abs_path):
                    os.remove(self.abs_path)
            else:
                # 原本存在 → 恢复原内容（优先用磁盘备份，确保字节一致）
                if self.backup_path and os.path.exists(self.backup_path):
                    _restore_from_backup(self.backup_path, self.abs_path)
                else:
                    # 回退到内存备份
                    with open(self.abs_path, "w", encoding="utf-8") as f:
                        f.write(self.original_content or "")
        except Exception as e:
            logger.warning("FileWriteUndo 失败 %s: %s", self.abs_path, e)


@dataclass
class FileDeleteUndo:
    """文件删除撤销 — 保存被删除文件的内容，reverse 时恢复"""
    abs_path: str
    original_content: str
    backup_path: Optional[str] = None

    async def apply_reverse(self) -> None:
        try:
            # 优先用磁盘备份恢复（保留原始字节和权限）
            if self.backup_path and os.path.exists(self.backup_path):
                _restore_from_backup(self.backup_path, self.abs_path)
            else:
                os.makedirs(os.path.dirname(self.abs_path) or ".", exist_ok=True)
                with open(self.abs_path, "w", encoding="utf-8") as f:
                    f.write(self.original_content)
        except Exception as e:
            logger.warning("FileDeleteUndo 失败 %s: %s", self.abs_path, e)


@dataclass
class FileMoveUndo:
    """文件移动撤销 — 保存源路径和目标路径，reverse 时反向移动

    注意：move_file 工具内部已经记录了 source 的原始内容，
    这里只需把目标路径的文件移回源路径即可。
    若 dest 原本就存在（被覆盖），还需从 dest_backup_path 恢复 dest 原内容。
    """
    source_path: str
    dest_path: str
    source_existed: bool
    dest_existed_before: bool  # 目标路径原本是否存在（被覆盖）
    source_backup_path: Optional[str] = None
    dest_backup_path: Optional[str] = None  # 若 dest 原本存在，备份其原内容

    async def apply_reverse(self) -> None:
        try:
            # 1. 如果目标路径现在存在（move 成功了），先把它移回源路径
            if os.path.exists(self.dest_path):
                os.makedirs(os.path.dirname(self.source_path) or ".", exist_ok=True)
                shutil.move(self.dest_path, self.source_path)
            # 2. 如果目标路径原本就存在（被覆盖了），从备份恢复目标原内容
            if self.dest_existed_before and self.dest_backup_path and os.path.exists(self.dest_backup_path):
                _restore_from_backup(self.dest_backup_path, self.dest_path)
        except Exception as e:
            logger.warning("FileMoveUndo 失败 %s -> %s: %s", self.dest_path, self.source_path, e)


@dataclass
class ShellUndo:
    """Shell 命令撤销 — 记录命令，reverse 时尝试执行反向命令"""
    command: str
    reverse_command: Optional[str]  # None 表示无反向命令

    async def apply_reverse(self) -> None:
        if not self.reverse_command:
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                self.reverse_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
        except Exception as e:
            logger.warning("ShellUndo 失败 %s: %s", self.reverse_command, e)


# ──────────────────────────────────────────────────────────────────────────────
# Undo 构建器
# ──────────────────────────────────────────────────────────────────────────────

class UndoBuilder:
    """根据工具调用构建 Undo 包"""

    @staticmethod
    def _resolve_abs(path: str, workspace: str) -> str:
        return os.path.abspath(os.path.join(workspace, path) if not os.path.isabs(path) else path)

    @staticmethod
    def _read_file_safe(abs_path: str) -> Optional[str]:
        """安全读取文件内容，不存在返回 None"""
        if not os.path.exists(abs_path):
            return None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.warning("读取文件失败 %s: %s", abs_path, e)
            return None

    @staticmethod
    async def build(call: ToolCall, ctx: RunContext) -> UndoAction:
        tool = call.name
        args = call.args
        run_id = ctx.run_id

        if tool == "write_file":
            path = args.get("file_path") or args.get("path", "")
            if not path or not ctx.workspace_path:
                return NoOpUndo(reason="no_path")
            try:
                abs_path = UndoBuilder._resolve_abs(path, ctx.workspace_path)
                if os.path.exists(abs_path):
                    content = UndoBuilder._read_file_safe(abs_path) or ""
                    backup_path = _backup_file(abs_path, run_id)
                    return FileWriteUndo(
                        abs_path=abs_path,
                        original_content=content,
                        existed=True,
                        backup_path=backup_path,
                        run_id=run_id,
                    )
                else:
                    return FileWriteUndo(
                        abs_path=abs_path,
                        original_content=None,
                        existed=False,
                        backup_path=None,
                        run_id=run_id,
                    )
            except Exception as e:
                logger.warning("构建 FileWriteUndo 失败: %s", e)
                return NoOpUndo(reason=f"build_failed: {e}")

        if tool == "patch_file":
            path = args.get("path", "")
            if not path or not ctx.workspace_path:
                return NoOpUndo(reason="no_path")
            try:
                abs_path = UndoBuilder._resolve_abs(path, ctx.workspace_path)
                if not os.path.exists(abs_path):
                    # 文件不存在，工具会返回错误，NoOp 即可
                    return NoOpUndo(reason="file_not_exist")
                content = UndoBuilder._read_file_safe(abs_path) or ""
                backup_path = _backup_file(abs_path, run_id)
                # patch_file 的 Undo 等同于 write_file：恢复整个文件原内容
                return FileWriteUndo(
                    abs_path=abs_path,
                    original_content=content,
                    existed=True,
                    backup_path=backup_path,
                    run_id=run_id,
                )
            except Exception as e:
                logger.warning("构建 PatchFileUndo 失败: %s", e)
                return NoOpUndo(reason=f"build_failed: {e}")

        if tool == "delete_file":
            path = args.get("path", "")
            if not path or not ctx.workspace_path:
                return NoOpUndo(reason="no_path")
            try:
                abs_path = UndoBuilder._resolve_abs(path, ctx.workspace_path)
                if not os.path.exists(abs_path):
                    return NoOpUndo(reason="file_not_exist")
                content = UndoBuilder._read_file_safe(abs_path) or ""
                backup_path = _backup_file(abs_path, run_id)
                return FileDeleteUndo(
                    abs_path=abs_path,
                    original_content=content,
                    backup_path=backup_path,
                )
            except Exception as e:
                logger.warning("构建 FileDeleteUndo 失败: %s", e)
                return NoOpUndo(reason=f"build_failed: {e}")

        if tool == "move_file":
            source = args.get("source", "")
            dest = args.get("destination", "")
            if not source or not dest or not ctx.workspace_path:
                return NoOpUndo(reason="no_path")
            try:
                src_abs = UndoBuilder._resolve_abs(source, ctx.workspace_path)
                dst_abs = UndoBuilder._resolve_abs(dest, ctx.workspace_path)
                if not os.path.exists(src_abs):
                    return NoOpUndo(reason="source_not_exist")
                # 备份源文件（reverse 时用于恢复到 source）
                src_backup = _backup_file(src_abs, run_id)
                dest_existed = os.path.exists(dst_abs)
                # 如果目标已存在，也备份目标（reverse 时恢复 dest 原内容）
                dst_backup = _backup_file(dst_abs, run_id) if dest_existed else None
                return FileMoveUndo(
                    source_path=src_abs,
                    dest_path=dst_abs,
                    source_existed=True,
                    dest_existed_before=dest_existed,
                    source_backup_path=src_backup,
                    dest_backup_path=dst_backup,
                )
            except Exception as e:
                logger.warning("构建 FileMoveUndo 失败: %s", e)
                return NoOpUndo(reason=f"build_failed: {e}")

        if tool == "shell_exec":
            cmd = args.get("command") or args.get("shell", "")
            reverse = UndoBuilder._shell_reverse(cmd)
            return ShellUndo(command=cmd, reverse_command=reverse)

        # 只读工具 → NoOp
        return NoOpUndo(reason="readonly")

    @staticmethod
    def _shell_reverse(cmd: str) -> Optional[str]:
        """尝试推导 shell 命令的反向命令"""
        cmd = cmd.strip()
        # mkdir → rmdir
        if cmd.startswith("mkdir "):
            path = cmd[6:].strip().strip('"\'')
            return f"rmdir {path}" if path else None
        # touch → rm
        if cmd.startswith("touch "):
            path = cmd[6:].strip().strip('"\'')
            return f"rm {path}" if path else None
        # 其他命令无通用反向
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Saddle
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Saddle:
    """
    马鞍 — 工具执行承载

    职责：
    1. 沙箱检查（Sandbox.validate）
    2. 构建 Undo 包（UndoBuilder.build）+ 磁盘备份
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
        raw_result, retry_err = await retry_with_backoff(
            func=lambda: self.registry.execute(call.name, call.args, tool_ctx),
            classifier=ErrorClassifier(),
            max_retries=2,
            base_delay=0.5,
        )
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
            )
        result.duration_ms = int((time.time() - start) * 1000)

        # 4. 失败时自动回滚
        if not result.success and auto_rollback:
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
            workspace_path=ctx.workspace_path,
            mode=ctx.mode,
            conversation_id=ctx.conversation_id,
            agent_run_id=ctx.run_id,
            ssh_session_id=ctx.ssh_session_id,
        )

    def schemas(self) -> list[dict[str, Any]]:
        """代理 registry.schemas()"""
        return self.registry.schemas()
