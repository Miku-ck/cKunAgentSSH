"""
文件补丁工具 — 基于 old_string/new_string 的精确字符串替换

安全约束: 路径必须在 workspace_path 内
"""
import os
import asyncio
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.file_ops import (
    _resolve_path_safe,
    _atomic_write,
    _LARGE_FILE_THRESHOLD,
    _LARGE_FILE_HEAD,
    _LARGE_FILE_TAIL,
)
from agent.file_changes import record_file_write


def _maybe_truncate(content: str) -> str:
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


class PatchFileTool(BaseTool):
    """精确字符串替换工具

    通过 old_string/new_string 对文件局部内容进行精确替换。
    - old_string 必须在文件中精确匹配（含空白）
    - 若 old_string 出现多次且 replace_all=false，返回错误（要求明确指定）
    - 替换成功后写回文件
    """

    name = "patch_file"
    description = "基于 old_string/new_string 精确替换编辑文件。适用于局部修改。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要替换的原文（必须精确匹配，含空白）"},
            "new_string": {"type": "string", "description": "替换后的文本"},
            "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false）"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    # C-review M14：本地文件系统工具 — 仅本地模式（work/ide）可用（ssh 模式走 shell_exec）
    meta = ToolMeta(risk_level="moderate", reversible=True, mode_scope=["work", "ide"])

    async def execute(self, context, path, old_string, new_string, replace_all=False):
        # 解析路径并做安全边界检查
        try:
            full_path = _resolve_path_safe(path, context.workspace_path)
        except ValueError as e:
            return ToolResult(False, "", str(e), priority="high")

        if not os.path.isfile(full_path):
            return ToolResult(False, "", f"文件不存在: {path}", priority="high")

        if not old_string:
            return ToolResult(False, "", "old_string 不能为空", priority="high")

        # 读取原文件内容
        try:
            original = await asyncio.to_thread(self._read_file, full_path)
        except Exception as e:
            return ToolResult(False, "", f"读取文件失败: {e}", priority="high")

        # 统计匹配次数
        count = original.count(old_string)
        if count == 0:
            # 未找到匹配，返回文件预览帮助排查
            preview = _maybe_truncate(original)
            return ToolResult(
                False,
                f"未找到匹配的 old_string。文件内容预览:\n{preview}",
                f"old_string 未在 {path} 中找到",
                priority="high",
            )
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                "",
                f"old_string 在文件中出现 {count} 次，请提供更长的上下文使其唯一，或设置 replace_all=true",
                priority="high",
            )

        # 执行替换
        if replace_all:
            new_content = original.replace(old_string, new_string)
            replaced = count
        else:
            new_content = original.replace(old_string, new_string, 1)
            replaced = 1

        # 记录文件变更（在写入前调用，以获取正确的 before/after diff）
        if context.agent_run_id:
            try:
                record_file_write(
                    agent_run_id=context.agent_run_id,
                    abs_path=full_path,
                    workspace_path=context.workspace_path or "",
                    new_content=new_content,
                )
            except Exception:
                pass  # 文件变更追踪失败不影响主流程

        # 写回文件（原子写入）
        try:
            await asyncio.to_thread(_atomic_write, full_path, new_content)
        except Exception as e:
            return ToolResult(False, "", f"写入文件失败: {e}", priority="high")

        return ToolResult(
            True,
            f"已替换 {replaced} 处匹配，文件已更新: {path}",
            priority="normal",
        )

    def _read_file(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
