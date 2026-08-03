"""
内容搜索工具 — 在文件中搜索匹配的行（支持正则表达式）

安全约束: 路径必须在 workspace_path 内
"""
import os
import re
import asyncio
import fnmatch
from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.file_ops import _resolve_path_safe

# 输出截断阈值
_GREP_OUTPUT_THRESHOLD = 4000
_GREP_OUTPUT_HEAD = 1500
_GREP_OUTPUT_TAIL = 1000

# 递归搜索时跳过这些目录
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", ".pytest_cache",
}


def _maybe_truncate_output(output: str) -> str:
    """大输出保护：超过阈值时保留头尾，中间省略"""
    if len(output) <= _GREP_OUTPUT_THRESHOLD:
        return output
    head = output[:_GREP_OUTPUT_HEAD]
    tail = output[-_GREP_OUTPUT_TAIL:]
    omitted = len(output) - _GREP_OUTPUT_HEAD - _GREP_OUTPUT_TAIL
    return head + f"\n... [输出已截断，省略中间 {omitted} 字符]\n" + tail


def _is_binary(path: str) -> bool:
    """检测文件是否为二进制（包含 \\0 字节）"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except Exception:
        return True


class GrepTool(BaseTool):
    """内容搜索工具

    在指定文件或目录中搜索匹配正则表达式的行。
    - 支持递归目录搜索
    - 支持 include glob 过滤文件名
    - 返回 file:line: content 格式（类似 ripgrep）
    - 自动跳过二进制文件和常见噪声目录
    """

    name = "grep"
    description = "在文件或目录中搜索匹配的行（支持正则表达式）。返回 file:line: content 格式。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索路径（文件或目录，默认工作区根目录）"},
            "include": {"type": "string", "description": "文件名 glob 过滤（如 *.py）"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写（默认 false）"},
            "max_results": {"type": "integer", "description": "最大返回匹配数（默认 50）"},
            "context_lines": {"type": "integer", "description": "匹配行前后显示的上下文行数（默认 0）"},
        },
        "required": ["pattern"],
    }
    meta = ToolMeta(risk_level="safe", reversible=False)

    async def execute(self, context, pattern, path=None, include=None,
                      ignore_case=False, max_results=50, context_lines=0):
        # 编译正则表达式
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(False, "", f"无效的正则表达式: {e}", priority="high")

        # 解析搜索路径
        search_path = path or context.workspace_path or "."
        try:
            full_path = _resolve_path_safe(search_path, context.workspace_path)
        except ValueError as e:
            return ToolResult(False, "", str(e), priority="high")

        if not os.path.exists(full_path):
            return ToolResult(False, "", f"路径不存在: {search_path}", priority="high")

        # 收集要搜索的文件列表
        try:
            files = await asyncio.to_thread(self._collect_files, full_path, include)
        except Exception as e:
            return ToolResult(False, "", f"收集文件失败: {e}", priority="high")

        # 执行搜索
        try:
            results, total_matches = await asyncio.to_thread(
                self._search_files, files, regex, max_results, context_lines,
                context.workspace_path,
            )
        except Exception as e:
            return ToolResult(False, "", f"搜索失败: {e}", priority="high")

        if not results:
            return ToolResult(True, "未找到匹配", priority="normal")

        output = "\n".join(results)
        output = _maybe_truncate_output(output)
        # 超过上限的匹配提示
        if total_matches > max_results:
            output += f"\n... [还有 {total_matches - max_results} 个匹配未显示]"
        return ToolResult(True, output, priority="normal")

    def _collect_files(self, root, include):
        """收集要搜索的文件列表"""
        files = []
        if os.path.isfile(root):
            files.append(root)
            return files
        if not os.path.isdir(root):
            return files
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过噪声目录（原地修改 dirnames 以剪枝）
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if include and not fnmatch.fnmatch(name, include):
                    continue
                files.append(os.path.join(dirpath, name))
        return files

    def _search_files(self, files, regex, max_results, context_lines, workspace):
        """在文件列表中搜索匹配行"""
        results = []
        total = 0
        for filepath in files:
            # 跳过二进制文件
            if _is_binary(filepath):
                continue
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue
            rel = self._rel_path(filepath, workspace)
            for i, line in enumerate(lines):
                if regex.search(line):
                    total += 1
                    if total <= max_results:
                        # 添加上下文行（若有）
                        if context_lines > 0:
                            start = max(0, i - context_lines)
                            end = min(len(lines), i + context_lines + 1)
                            for j in range(start, end):
                                if j == i:
                                    results.append(f"{rel}:{j + 1}: {lines[j].rstrip()}")
                                else:
                                    results.append(f"{rel}:{j + 1}- {lines[j].rstrip()}")
                        else:
                            results.append(f"{rel}:{i + 1}: {line.rstrip()}")
        return results, total

    def _rel_path(self, filepath, workspace):
        """计算相对路径（用于输出展示）"""
        if workspace:
            try:
                return os.path.relpath(filepath, workspace)
            except ValueError:
                pass
        return filepath
