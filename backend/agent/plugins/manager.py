"""MCP stdio 会话池。

设计取舍：会话按 (plugin_id, config_rev) 缓存，配置变更或停用时立刻关闭；
不在 run 之间无条件保活，避免「用户以为停用了、进程还在跑」的权限漂移。
崩溃的会话当次 run 直接失败并销毁，后续 run 才允许重建（不自动重试调用，
插件工具默认非幂等，重试会放大副作用）。

子进程清理走 AsyncExitStack → stdio_client 的 terminate/wait/kill；
Windows Job Object 式的进程树兜底未实现（见 README 已知限制），
单插件进程树里再派生孙进程的场景需要额外加固。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 64 * 1024
_HEAD_CHARS = 40 * 1024
_TAIL_CHARS = 12 * 1024
_DEFAULT_TIMEOUT_S = 30.0
# 只继承这些系统变量：后端进程里的 API Token / SSH 凭据 / 代理设置一律不进插件。
_MIN_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE",
    "LANG", "LC_ALL", "PYTHONIOENCODING",
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def truncate_output(text: str) -> str:
    """头尾保留截断。插件输出是**不可信数据**，截断标记必须显式可见。"""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    return (
        text[:_HEAD_CHARS]
        + f"\n... [插件输出已截断，省略中间 {omitted} 字符；未被展示的内容不得据此推断] ...\n"
        + text[-_TAIL_CHARS:]
    )


def build_plugin_env(row: dict) -> dict[str, str]:
    """最小系统环境 + 用户显式声明项（DPAPI 信封解密）。

    刻意不复制 os.environ：后端持着 LLM api_key、SSH 凭据与代理令牌，
    整份继承等于把插件变成凭据外泄通道。
    """
    env = {k: os.environ[k] for k in _MIN_ENV_KEYS if k in os.environ}
    raw = row.get("env_ciphertext") or ""
    if not raw:
        return env
    import database as db
    try:
        declared = json.loads(db.unprotect_secret(raw))
    except Exception as exc:
        raise RuntimeError(f"插件环境变量解密失败: {exc}") from exc
    if not isinstance(declared, dict):
        raise RuntimeError("插件环境变量格式错误：应为 JSON 对象")
    for key, value in declared.items():
        if isinstance(key, str) and _ENV_KEY_RE.match(key):
            env[key] = str(value)
        else:
            logger.warning("忽略非法插件环境变量名: %r", key)
    return env


@dataclass
class _Session:
    plugin_id: str
    config_rev: int
    session: Any
    stack: AsyncExitStack


class PluginManager:
    """插件会话池（进程级单例，见 get_plugin_manager）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    async def _open_session(self, row: dict) -> _Session:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=row["command"],
            args=list(json.loads(row.get("args_json") or "[]")),
            env=build_plugin_env(row),
            cwd=row.get("cwd") or None,
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        return _Session(row["id"], int(row.get("config_rev") or 1), session, stack)

    async def ensure(self, row: dict) -> _Session:
        """按 config_rev 复用；配置变了先关旧的再开新的。"""
        rev = int(row.get("config_rev") or 1)
        async with self._lock:
            current = self._sessions.get(row["id"])
            if current and current.config_rev == rev:
                return current
            if current:
                await self._close_locked(row["id"])
            opened = await self._open_session(row)
            self._sessions[row["id"]] = opened
            return opened

    async def _close_locked(self, plugin_id: str) -> None:
        session = self._sessions.pop(plugin_id, None)
        if not session:
            return
        # 不能用 asyncio.wait_for 包 aclose：wait_for 会新建 task，而
        # stdio_client 的 anyio cancel scope 要求在进入它的同一个 task 里退出，
        # 跨 task 退出会抛 "Cancelled via cancel scope"。stdio_client 自己负责
        # terminate→wait→kill，直接 await 即可。
        try:
            await session.stack.aclose()
        except Exception as exc:
            logger.warning("关闭插件会话失败 (id=%s): %s", plugin_id, exc)

    async def close(self, plugin_id: str) -> None:
        async with self._lock:
            await self._close_locked(plugin_id)

    async def close_all(self) -> None:
        async with self._lock:
            for plugin_id in list(self._sessions):
                await self._close_locked(plugin_id)

    async def list_tools(self, row: dict, timeout_s: float = 10.0) -> list[dict]:
        """发现工具。返回规范化的 spec，供 McpTool 桥接与 UI 展示。"""
        opened = await self.ensure(row)
        response = await asyncio.wait_for(opened.session.list_tools(), timeout=timeout_s)
        specs: list[dict] = []
        for tool in getattr(response, "tools", None) or []:
            annotations = getattr(tool, "annotations", None)
            # MCP 2.x 的字段名已改成 snake_case（input_schema / read_only_hint），
            # 1.x 是 inputSchema / readOnlyHint。两种都要接：只认 camelCase 会
            # 拿到空 schema，ToolRegistry 的参数校验随后把全部实参判成「多余字段」。
            schema = (getattr(tool, "input_schema", None)
                      or getattr(tool, "inputSchema", None))
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            read_only = bool(
                getattr(annotations, "read_only_hint", None)
                or getattr(annotations, "readOnlyHint", False)
            ) if annotations is not None else False
            specs.append({
                "name": str(getattr(tool, "name", "") or ""),
                "description": str(getattr(tool, "description", "") or "")[:2000],
                "inputSchema": schema,
                # 服务端声明只作建议，实际只读资格必须由用户逐项确认
                "readOnlyHint": read_only,
            })
        return [s for s in specs if s["name"]]

    async def call(
        self,
        plugin_id: str,
        original_name: str,
        args: dict,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> tuple[bool, str, str]:
        """返回 (success, output, error)。任何异常都销毁会话，不重用可疑连接。"""
        async with self._lock:
            opened = self._sessions.get(plugin_id)
        if not opened:
            return False, "", "插件会话不可用（未确认、已停用或配置已变更）"
        try:
            response = await asyncio.wait_for(
                opened.session.call_tool(original_name, arguments=args),
                timeout=max(1.0, timeout_s),
            )
        except asyncio.TimeoutError:
            return False, "", f"插件工具调用超时（{timeout_s:g}s），未自动重试"
        except Exception as exc:
            await self.close(plugin_id)
            return False, "", f"插件调用失败: {type(exc).__name__}: {exc}"

        parts: list[str] = []
        for block in getattr(response, "content", None) or []:
            kind = getattr(block, "type", "unknown")
            if kind == "text":
                parts.append(str(getattr(block, "text", "") or ""))
            else:
                parts.append(f"[{kind} 内容未展示]")
        text = truncate_output("\n".join(parts))
        failed = bool(getattr(response, "isError", False))
        return (not failed), text, ("插件返回 isError" if failed else "")


_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager
