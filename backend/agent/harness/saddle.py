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
import re
import shlex
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
    - 备份文件命名: <backup_root>/<run_id>/<sha256(abs_path)>_<basename>_g<N>
      C-review M13：带代次序号 — 同 run 内同文件二次写入时，旧实现 copy2 同名
      目标静默覆盖第一次备份，undo 恢复到"中间态"而非 run 前原始内容。
      现在每次写前各存一代（g1 = 第一次写前内容 = run 前原始），undo 取第一代。
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
        base_name = f"{path_hash}_{os.path.basename(abs_path)}"
        # C-review M13：扫描该文件已有代次（旧版无代次备份视为 g1）
        gen = 0
        for name in os.listdir(run_dir):
            if name == base_name:
                gen = max(gen, 1)
                continue
            m = re.fullmatch(re.escape(base_name) + r"_g(\d+)", name)
            if m:
                gen = max(gen, int(m.group(1)))
        backup_name = f"{base_name}_g{gen + 1}"
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
    """清理某 run 的所有磁盘备份（keep/undo 后调用）"""
    try:
        backup_root = _get_backup_root()
        run_dir = os.path.join(backup_root, run_id)
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)
    except Exception as e:
        logger.warning("清理备份失败 %s: %s", run_id, e)


def cleanup_stale_backups(max_age_days: int = 7) -> int:
    """C-review C3：按目录 mtime 清理过期备份，返回清理数量。

    run 结束后不再自动清理备份（保留供 /undo 使用，见 routes/agent.py），
    为防止备份目录无限膨胀，启动时按时间兜底清理。
    """
    import time
    removed = 0
    try:
        backup_root = _get_backup_root()
        cutoff = time.time() - max_age_days * 86400
        for entry in os.listdir(backup_root):
            path = os.path.join(backup_root, entry)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except Exception as e:
        logger.warning("清理过期备份失败: %s", e)
    return removed


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
            # C-review NIT：空 workspace fail-closed — 原实现返回 ok=True（fail-open），
            # 安全底线组件在缺失边界时等同无边界
            return SandboxCheck(ok=False, reason="未配置 workspace，无法校验路径边界")
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
    """Shell 命令撤销 — 记录命令，reverse 时尝试执行反向命令

    C-review C4：记录原命令的执行宿主（local/ssh + session_id）——
    原实现 reverse 恒在本地 create_subprocess_shell 执行，SSH 模式下
    原命令在远程服务器、回滚却在后端主机"打错机器"（误删本地同名路径）。
    """
    command: str
    reverse_command: Optional[str]  # None 表示无反向命令
    host: str = "local"  # "local" | "ssh"
    ssh_session_id: str = ""

    async def apply_reverse(self) -> None:
        if not self.reverse_command:
            return
        if self.host == "ssh":
            # 远程命令的反向必须走 ElectronBridge 下发到同一 SSH 会话
            if not self.ssh_session_id:
                logger.warning("ShellUndo 跳过：ssh 反向命令缺 session_id (command=%s)", self.command)
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
            return
        try:
            proc = await asyncio.create_subprocess_shell(
                self.reverse_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # 修复：添加超时防止反向命令挂起导致回滚流程永久阻塞
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("ShellUndo 超时，强制终止: %s", self.reverse_command)
                proc.kill()
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
            # C-review C4：记录执行宿主 — ssh_session_id 存在即远程执行
            return ShellUndo(
                command=cmd,
                reverse_command=reverse,
                host="ssh" if getattr(ctx, "ssh_session_id", "") else "local",
                ssh_session_id=getattr(ctx, "ssh_session_id", "") or "",
            )

        # 只读工具 → NoOp
        return NoOpUndo(reason="readonly")

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

        # 审查 MINOR 修复：仅对幂等工具重试。原实现对所有工具（含 write_file /
        # shell_exec / delete_file / move_file / spawn_subagent）统一重试 —
        # 非幂等工具在"副作用已发生但结果丢失"的瞬时错误下重跑，会造成
        # 命令双执行、文件写两次、子 Agent 双启动。非幂等工具只执行一次。
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
            # 审查修复（P2 E6）：透传 cost_tracker — 子 Agent 通过
            # spawn_subagent 调用时需要上报 token 用量。
            # cost_tracker 在 harness.run() 中被写入 ctx.metadata["cost_tracker"]
            cost_tracker=ctx.metadata.get("cost_tracker"),
        )

    def schemas(self) -> list[dict[str, Any]]:
        """代理 registry.schemas()"""
        return self.registry.schemas()
