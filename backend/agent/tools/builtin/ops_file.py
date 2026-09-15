"""运维文件操作工具集 — SSH 模式专属。

包含五个 BaseTool 子类：
  - file_read  ：带行号读取文件片段（offset/limit）
  - file_write ：新建或整体覆写文件（base64 传输，避免注入）
  - file_edit  ：精确字符串替换（要求 old_string 恰好出现 1 次）
  - file_glob  ：按 glob 模式在目录下递归查找文件
  - file_grep  ：按正则递归搜索文件内容

安全约束：
  1. 所有路径均校验绝对路径归一化后不得包含 `..`，杜绝越界访问；
  2. 远程命令一律经 shlex.quote 转义（file_write/编辑用 base64，天然不必转义）；
  3. **写/编辑（file_write/file_edit）仅限远程 SSH 会话**：与 shell_exec、
     ops_common.run_headless 同一 fail-closed 口径 — 缺少 SSH 会话即拒绝，
     绝不降级到后端主机本机落盘（本产品 AI 操作的对象是远程服务器，
     在后端主机写文件没有合法场景）；
  4. 读取类（file_read/file_glob/file_grep/read_image）保留本地分支，
     服务本地终端场景的查看/诊断（Python 原生文件 API，只读无突变）；
  5. 大输出经 truncate_output 截断，防刷屏。
"""
import os
import pathlib
import re
import shlex

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import (
    base64_write_cmd,
    drop_sentinel_lines,
    is_remote,
    is_traversal,
    remote_read_b64,
    run_headless,
    truncate_output,
)


_WRITE_REQUIRES_SSH = (
    "拒绝执行（fail-closed）：缺少 SSH 会话，文件写入/编辑仅限远程目标服务器，"
    "不会在后端主机本机落盘。请先建立 SSH 连接，或确认操作目标。"
)


class FileReadTool(BaseTool):
    name = "file_read"
    description = "带行号读取文件指定片段（offset/limit，offset 从 1 开始）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件绝对路径"},
            "offset": {
                "type": "integer",
                "description": "起始行号（>=1，默认 1）",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "读取行数（1-10000，默认 200）",
                "default": 200,
            },
        },
        "required": ["path"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    def _validate(self, path, offset, limit):
        if is_traversal(path):
            return "非法路径：包含 `..`，禁止越界访问"
        if offset < 1:
            return "offset 必须为 >=1 的整数"
        if limit < 1:
            return "limit 必须为 >=1 的整数"
        return None

    async def execute(self, context, path, offset=1, limit=200):
        offset, limit = int(offset), int(limit)
        limit = min(10000, limit)
        err = self._validate(path, offset, limit)
        if err:
            return ToolResult(False, "", err)

        if is_remote(context):
            qpath = shlex.quote(path)
            start, stop = offset, offset + limit - 1
            cmd = f"nl -ba {qpath} | sed -n '{start},{stop}p'"
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            if not ok:
                return ToolResult(False, output, "读取文件失败")
            body = strip_remote_tail(output)
            if not body.strip():
                return ToolResult(True, f"无内容（offset {offset} 超出文件末尾或文件为空）")
            return ToolResult(True, body)

        # 本地（Windows）：Python 原生文件 API
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            return ToolResult(False, "", f"文件不存在: {path}")
        except PermissionError:
            return ToolResult(False, "", f"无权限访问文件: {path}")
        except IsADirectoryError:
            return ToolResult(False, "", f"路径是目录而非文件: {path}")
        except Exception as e:
            return ToolResult(False, "", f"读取文件失败: {e}")

        begin = offset - 1
        selected = all_lines[begin : begin + limit]
        if not selected:
            return ToolResult(True, f"无内容（offset {offset} 超出文件末尾）")
        out_lines = [
            f"{lineno}: {ln.rstrip()}"
            for lineno, ln in enumerate(selected, start=offset)
        ]
        return ToolResult(True, truncate_output("\n".join(out_lines)))


class FileWriteTool(BaseTool):
    name = "file_write"
    description = "新建或整体覆写文件内容（非追加；覆盖已有内容）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件绝对路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        },
        "required": ["path", "content"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    async def execute(self, context, path, content):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        if not is_remote(context):
            return ToolResult(False, "", _WRITE_REQUIRES_SSH)
        content = content if isinstance(content, str) else str(content)
        data = content.encode("utf-8")

        ok, output = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok:
            return ToolResult(False, output, "远程写入文件失败")
        return ToolResult(True, f"已写入 {len(data)} 字节 -> {path}")


class FileEditTool(BaseTool):
    name = "file_edit"
    description = "精确字符串替换：old_string 必须恰好出现 1 次，替换为新内容后写回。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件绝对路径"},
            "old_string": {"type": "string", "description": "要被替换的旧字符串（须恰好出现 1 次）"},
            "new_string": {"type": "string", "description": "替换后的新字符串"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    meta = ToolMeta(
        risk_level="moderate",
        reversible=False,
        idempotent=False,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    @staticmethod
    def _apply_replace(content, old, new):
        count = content.count(old)
        if count == 0:
            return ToolResult(False, "", f"old_string 在文件中出现 0 次，无法替换")
        if count > 1:
            return ToolResult(
                False, "", f"old_string 在文件中出现 {count} 次，要求恰好 1 次，拒绝替换"
            )
        return content.replace(old, new)

    async def execute(self, context, path, old_string, new_string):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        if not isinstance(old_string, str) or old_string == "":
            return ToolResult(False, "", "old_string 不能为空")
        if not is_remote(context):
            return ToolResult(False, "", _WRITE_REQUIRES_SSH)

        ok, raw = await remote_read_b64(context, path)
        if not ok:
            return ToolResult(False, raw.decode("utf-8", errors="replace"), "远程读取文件失败或文件不存在")
        content = raw.decode("utf-8", errors="replace")
        new_content = self._apply_replace(content, old_string, new_string)
        if isinstance(new_content, ToolResult):
            return new_content
        data = new_content.encode("utf-8")
        ok2, out2 = await run_headless(
            context, base64_write_cmd(path, data), self.meta.timeout
        )
        if not ok2:
            return ToolResult(False, out2, "远程写入文件失败")
        return ToolResult(True, "已替换 1 处")


class FileGlobTool(BaseTool):
    name = "file_glob"
    description = "按 glob 模式在指定目录下递归查找文件，返回匹配文件列表。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录绝对路径"},
            "pattern": {"type": "string", "description": "glob 匹配模式（相对该目录或绝对均可）"},
        },
        "required": ["path", "pattern"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    async def execute(self, context, path, pattern):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")

        if is_remote(context):
            qpath, qpat = shlex.quote(path), shlex.quote(pattern)
            cmd = f"find {qpath} -name {qpat} -type f"
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            if not ok:
                return ToolResult(False, output, "glob 查找失败")
            lines = drop_sentinel_lines(output)
            if not lines:
                return ToolResult(True, f"未匹配到文件：{pattern}")
            return ToolResult(True, "\n".join(lines))

        # 本地：pathlib 递归查找
        base = pathlib.Path(path)
        if not base.is_dir():
            return ToolResult(False, "", f"起始目录不存在或不是目录: {path}")
        pat = pattern.lstrip("/\\")
        try:
            matches = sorted(
                str(p.resolve()) for p in base.rglob(pat) if p.is_file()
            )
        except Exception as e:
            return ToolResult(False, "", f"glob 查找失败: {e}")
        if not matches:
            return ToolResult(True, f"未匹配到文件：{pattern}")
        return ToolResult(True, "\n".join(matches))


class FileGrepTool(BaseTool):
    name = "file_grep"
    description = "按正则（RE2 风格）在文件或目录下递归搜索内容，返回匹配行。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要搜索的文件或目录绝对路径"},
            "pattern": {"type": "string", "description": "正则表达式"},
            "max_lines": {
                "type": "integer",
                "description": "最多返回匹配行数（1-5000，默认 200）",
                "default": 200,
            },
        },
        "required": ["path", "pattern"],
    }
    meta = ToolMeta(
        risk_level="safe",
        reversible=True,
        idempotent=True,
        timeout=30.0,
        mode_scope=["ssh"],
    )

    async def execute(self, context, path, pattern, max_lines=200):
        if is_traversal(path):
            return ToolResult(False, "", "非法路径：包含 `..`，禁止越界访问")
        max_lines = min(5000, max(1, int(max_lines)))

        if is_remote(context):
            qpat, qpath = shlex.quote(pattern), shlex.quote(path)
            cmd = f"grep -rnE --color=never {qpat} {qpath} | head -n {max_lines}"
            ok, output = await run_headless(context, cmd, self.meta.timeout)
            lines = drop_sentinel_lines(output)
            if not lines:
                if not ok and output.strip():
                    return ToolResult(False, output, "grep 查找失败")
                return ToolResult(True, f"未匹配到内容：{pattern}")
            return ToolResult(True, truncate_output("\n".join(lines)))

        # 本地：Python 正则逐行搜索
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(False, "", f"正则表达式无效: {e}")

        target = pathlib.Path(path)
        if target.is_file():
            files = iter([target])
        elif target.is_dir():
            files = (p for p in sorted(target.rglob("*")) if p.is_file())
        else:
            return ToolResult(False, "", f"路径不存在: {path}")
        base = target if target.is_dir() else target.parent

        results = []
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        text = line.rstrip("\n")
                        if regex.search(text):
                            try:
                                rel = str(fp.relative_to(base))
                            except ValueError:
                                rel = str(fp)
                            results.append(f"{rel}:{lineno}:{text}")
                            if len(results) >= max_lines:
                                break
            except (IsADirectoryError, PermissionError, OSError):
                continue
            if len(results) >= max_lines:
                break

        if not results:
            return ToolResult(True, f"未匹配到内容：{pattern}")
        return ToolResult(True, truncate_output("\n".join(results)))