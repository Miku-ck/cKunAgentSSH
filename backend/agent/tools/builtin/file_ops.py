"""
文件操作工具 — IDE 模式专属

安全约束: 所有路径必须在 workspace_path 内
"""
import os
import asyncio
import tempfile
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.file_changes import record_file_write

# 大文件保护阈值（与 bridle.tool_result_max_chars 对齐）
_LARGE_FILE_THRESHOLD = 4000
_LARGE_FILE_HEAD = 1500
_LARGE_FILE_TAIL = 1000


def _resolve_path_safe(path: str, workspace: str) -> str:
    """解析路径并做安全边界检查（防路径遍历）"""
    if not workspace:
        raise ValueError("未配置 workspace_path")
    full_path = os.path.realpath(os.path.join(workspace, path) if not os.path.isabs(path) else path)
    real_workspace = os.path.realpath(workspace)
    # 使用 commonpath 严格检查路径包含
    try:
        common = os.path.commonpath([full_path, real_workspace])
        if common != real_workspace:
            raise ValueError(f"路径越界: {path} 不在 workspace 内")
    except ValueError:
        raise ValueError(f"路径越界: {path} 不在 workspace 内")
    return full_path


def _atomic_write(path: str, content: str) -> None:
    """原子写入文件：先写临时文件，再 rename 到目标路径

    保证：
    - 写入中途崩溃 → 目标文件保持原内容
    - 写入完成后 → 目标文件立即是新内容（rename 原子）
    - 保留原文件的权限（通过 os.replace 覆盖时权限不变）
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    # 在目标目录创建临时文件（确保同文件系统，rename 才原子）
    fd, tmp_path = tempfile.mkstemp(
        prefix=".agent_write_",
        suffix=".tmp",
        dir=target_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # os.replace 原子覆盖（跨平台）
        os.replace(tmp_path, path)
    except Exception:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取指定路径的文件内容。可指定行范围。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对于工作区或绝对路径）"},
            "start_line": {"type": "integer", "description": "起始行号（从 1 开始，可选）"},
            "end_line": {"type": "integer", "description": "结束行号（可选）"},
        },
        "required": ["path"],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用。
    # ssh 模式的目标在远程服务器，本地文件工具会"打错机器"（改到后端主机），
    # 远程文件操作一律走 shell_exec（cat/ls/sed 等）。
    meta = ToolMeta(risk_level="safe", reversible=True, idempotent=True,
                    mode_scope=["work", "ide"])

    async def execute(self, context, path, start_line=None, end_line=None):
        # 解析路径
        full_path = self._resolve_path(path, context.workspace_path)
        if not full_path:
            return ToolResult(False, "", f"路径 {path} 超出工作区范围", priority="high")

        if not os.path.isfile(full_path):
            return ToolResult(False, "", f"文件不存在: {path}", priority="high")

        try:
            # 在线程中读取，避免阻塞事件循环
            content = await asyncio.to_thread(self._read_file, full_path, start_line, end_line)
            # 大文件保护：超过阈值时主动应用头尾保留
            content = self._maybe_truncate(content)
            return ToolResult(True, content, "", priority="normal")
        except Exception as e:
            return ToolResult(False, "", str(e), priority="high")

    def _resolve_path(self, path: str, workspace: str) -> str | None:
        try:
            return _resolve_path_safe(path, workspace)
        except ValueError:
            return None

    def _read_file(self, path, start, end):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if start or end:
            s = (start or 1) - 1
            e = end or len(lines)
            lines = lines[s:e]
        return "".join(lines)

    def _maybe_truncate(self, content: str) -> str:
        """大文件保护：超过阈值时保留头尾，中间省略"""
        if len(content) <= _LARGE_FILE_THRESHOLD:
            return content
        head = content[:_LARGE_FILE_HEAD]
        tail = content[-_LARGE_FILE_TAIL:]
        omitted = len(content) - _LARGE_FILE_HEAD - _LARGE_FILE_TAIL
        return (
            head
            + f"\n... [文件较大，已省略中间 {omitted} 字符，保留头部 {_LARGE_FILE_HEAD} + 尾部 {_LARGE_FILE_TAIL}]\n"
            + tail
        )


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "写入内容到指定文件。如果文件已存在则覆盖。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的内容"},
        },
        "required": ["path", "content"],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec）
    meta = ToolMeta(risk_level="moderate", reversible=True, mode_scope=["work", "ide"])

    async def execute(self, context, path, content):
        full_path = self._resolve_path(path, context.workspace_path)
        if not full_path:
            return ToolResult(False, "", f"路径 {path} 超出工作区范围")

        try:
            # E 修复：先记录变更再写入（与 delete_file/move_file 一致）。
            # record_file_write 读取磁盘当前内容作为 before — 原实现先写后记录，
            # before 永远等于 new_content，diff 恒空，文件变更记录形同虚设
            if context.agent_run_id:
                try:
                    record_file_write(
                        agent_run_id=context.agent_run_id,
                        abs_path=full_path,
                        workspace_path=context.workspace_path or "",
                        new_content=content,
                    )
                except Exception:
                    pass  # 文件变更追踪失败不影响主流程
            await asyncio.to_thread(_atomic_write, full_path, content)
            # 写入结果反馈：常规优先级
            return ToolResult(True, f"文件已写入: {path} ({len(content)} 字符)", priority="normal")
        except Exception as e:
            return ToolResult(False, "", str(e))

    def _resolve_path(self, path, workspace):
        try:
            return _resolve_path_safe(path, workspace)
        except ValueError:
            return None


class ListDirTool(BaseTool):
    name = "list_dir"
    description = "列出指定目录下的文件和子目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径（默认工作区根目录）"},
        },
        "required": [],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec）
    meta = ToolMeta(risk_level="safe", reversible=True, idempotent=True,
                    mode_scope=["work", "ide"])

    async def execute(self, context, path=None):
        target = path or context.workspace_path
        if not target:
            return ToolResult(False, "", "未指定路径，且无工作区路径")

        full_path = self._resolve_path(target, context.workspace_path)
        if not full_path:
            return ToolResult(False, "", f"路径 {target} 超出工作区范围")

        if not os.path.isdir(full_path):
            return ToolResult(False, "", f"目录不存在: {target}")

        try:
            entries = await asyncio.to_thread(self._list_dir, full_path)
            # 目录列表：噪声信息，设为 low
            return ToolResult(True, "\n".join(entries), priority="low")
        except Exception as e:
            return ToolResult(False, "", str(e))

    def _resolve_path(self, path, workspace):
        try:
            return _resolve_path_safe(path, workspace)
        except ValueError:
            return None

    def _list_dir(self, path):
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append(f"[DIR]  {name}/")
            else:
                size = os.path.getsize(full)
                entries.append(f"[FILE] {name} ({size} bytes)")
        return entries


class DeleteFileTool(BaseTool):
    """删除文件工具

    将指定文件从磁盘删除。
    - 路径必须在 workspace 内
    - 文件不存在时返回错误
    - 通过 record_file_write(new_content="") 记录删除变更
    """

    name = "delete_file"
    description = "删除指定路径的文件。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要删除的文件路径"},
        },
        "required": ["path"],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec）
    meta = ToolMeta(risk_level="moderate", reversible=True, mode_scope=["work", "ide"])

    async def execute(self, context, path):
        full_path = self._resolve_path(path, context.workspace_path)
        if not full_path:
            return ToolResult(False, "", f"路径 {path} 超出工作区范围", priority="high")

        if not os.path.isfile(full_path):
            return ToolResult(False, "", f"文件不存在: {path}", priority="high")

        try:
            # 记录文件变更（在删除前调用，以获取正确的 before 状态）
            if context.agent_run_id:
                try:
                    record_file_write(
                        agent_run_id=context.agent_run_id,
                        abs_path=full_path,
                        workspace_path=context.workspace_path or "",
                        new_content="",  # 空内容表示删除
                    )
                except Exception:
                    pass  # 文件变更追踪失败不影响主流程
            await asyncio.to_thread(os.remove, full_path)
            return ToolResult(True, f"文件已删除: {path}", priority="normal")
        except Exception as e:
            return ToolResult(False, "", str(e), priority="high")

    def _resolve_path(self, path, workspace):
        try:
            return _resolve_path_safe(path, workspace)
        except ValueError:
            return None


class MoveFileTool(BaseTool):
    """移动/重命名文件工具

    将文件从源路径移动到目标路径。
    - 两个路径都必须在 workspace 内
    - 自动创建目标目录
    - 通过 record_file_write 记录源文件删除和目标文件创建
    """

    name = "move_file"
    description = "移动或重命名文件。"
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "源文件路径"},
            "destination": {"type": "string", "description": "目标路径"},
        },
        "required": ["source", "destination"],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec）
    meta = ToolMeta(risk_level="moderate", reversible=True, mode_scope=["work", "ide"])

    async def execute(self, context, source, destination):
        src_full = self._resolve_path(source, context.workspace_path)
        if not src_full:
            return ToolResult(False, "", f"源路径 {source} 超出工作区范围", priority="high")

        dst_full = self._resolve_path(destination, context.workspace_path)
        if not dst_full:
            return ToolResult(False, "", f"目标路径 {destination} 超出工作区范围", priority="high")

        if not os.path.isfile(src_full):
            return ToolResult(False, "", f"源文件不存在: {source}", priority="high")

        try:
            import shutil
            # 读取源文件内容（用于记录目标文件的创建）
            src_content = await asyncio.to_thread(self._read_file_content, src_full)

            # 记录文件变更（在移动前调用，以获取正确的 before/after 状态）
            if context.agent_run_id:
                try:
                    # 源文件 → 删除（new_content 为空）
                    record_file_write(
                        agent_run_id=context.agent_run_id,
                        abs_path=src_full,
                        workspace_path=context.workspace_path or "",
                        new_content="",
                    )
                    # 目标文件 → 创建（new_content 为源文件内容）
                    record_file_write(
                        agent_run_id=context.agent_run_id,
                        abs_path=dst_full,
                        workspace_path=context.workspace_path or "",
                        new_content=src_content,
                    )
                except Exception:
                    pass  # 文件变更追踪失败不影响主流程

            # 确保目标目录存在
            dst_dir = os.path.dirname(dst_full)
            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)

            await asyncio.to_thread(shutil.move, src_full, dst_full)
            return ToolResult(True, f"文件已移动: {source} → {destination}", priority="normal")
        except Exception as e:
            return ToolResult(False, "", str(e), priority="high")

    def _resolve_path(self, path, workspace):
        try:
            return _resolve_path_safe(path, workspace)
        except ValueError:
            return None

    def _read_file_content(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
