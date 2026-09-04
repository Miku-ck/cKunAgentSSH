"""Electron Bridge — Python 后端调用 Electron 主进程能力的客户端"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# httpx 超时比 SSH 超时多出的余量（秒），确保 SSH 侧超时先触发，
# Electron 返回错误响应后 httpx 正常收到结果，避免两侧同时超时导致竞争。
_HTTP_TIMEOUT_MARGIN = 5.0


class ElectronBridge:
    """调用 Electron 主进程桥接服务的客户端"""

    def __init__(self) -> None:
        port = os.environ.get("ELECTRON_BRIDGE_PORT", "0") or "0"
        self.port = int(port)
        self.token = os.environ.get("ELECTRON_BRIDGE_TOKEN", "")
        self.base_url = f"http://127.0.0.1:{self.port}" if self.port else None

    def is_available(self) -> bool:
        return bool(self.port > 0 and self.base_url and self.token)

    async def exec_ssh_command(
        self,
        ssh_session_id: str,
        command: str,
        timeout: float = 30.0,
    ) -> dict:
        """在远程 SSH 会话上执行命令

        返回结构: {success: bool, output: str, error: str, exit_code: int | None}
        """
        if not self.is_available():
            raise RuntimeError("Electron bridge not configured")

        # httpx 超时 > SSH 超时，确保 SSH 侧先超时并返回错误响应
        http_timeout = timeout + _HTTP_TIMEOUT_MARGIN

        return await self._post(
            "/ssh/exec",
            {
                "sshSessionId": ssh_session_id,
                "command": command,
                "timeout": timeout,
            },
            http_timeout,
        )

    async def connect_ssh(self, connection_id: str, timeout: float = 60.0) -> dict:
        """按连接 ID 自动建立一条轻量 SSH 会话（供定时任务无头执行）。

        返回结构: {success: bool, sessionId: str, name: str}
        """
        if not self.is_available():
            raise RuntimeError("Electron bridge not configured")

        # 建连可能耗时（readyTimeout 默认 15s），放宽 HTTP 超时余量
        http_timeout = timeout + _HTTP_TIMEOUT_MARGIN
        return await self._post(
            "/ssh/connect",
            {"connectionId": connection_id},
            http_timeout,
        )

    async def disconnect_ssh(self, session_id: str, timeout: float = 15.0) -> dict:
        """关闭由 connect_ssh 建立的 SSH 会话，释放连接资源。"""
        if not self.is_available():
            raise RuntimeError("Electron bridge not configured")

        return await self._post(
            "/ssh/disconnect",
            {"sessionId": session_id},
            timeout + _HTTP_TIMEOUT_MARGIN,
        )

    async def _post(self, path: str, payload: dict, http_timeout: float) -> dict:
        """向桥接服务发送 JSON POST 请求。"""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=http_timeout
            ) as client:
                resp = await client.post(
                    path,
                    json=payload,
                    headers={"X-Bridge-Token": self.token},
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            logger.warning("Electron bridge 请求超时 (path=%s)", path)
            raise RuntimeError("Electron bridge 请求超时")
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Electron bridge 返回错误状态 %s (path=%s): %s",
                e.response.status_code, path, e.response.text[:200],
            )
            # 优先透传服务端给出的可读错误
            try:
                detail = (e.response.json() or {}).get("error", "")
            except Exception:
                detail = ""
            raise RuntimeError(detail or f"Electron bridge 错误 ({e.response.status_code})")
        except httpx.ConnectError:
            logger.warning("Electron bridge 连接失败 (port=%s)", self.port)
            raise RuntimeError(
                "Electron bridge 连接失败，主进程未启动，或桥接端口/token 已过期"
                "（请完整重启应用，使主进程与后端一起重启）"
            )
