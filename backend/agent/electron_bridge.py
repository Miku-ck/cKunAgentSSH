"""Electron Bridge — Python 后端调用 Electron 主进程能力的客户端"""
from __future__ import annotations

import logging
import os

import httpx

# ════════════════════════════════════════════════
#   桥接错误分类（与 electron 侧 SSHError 遥相呼应，但独立体系）
# ════════════════════════════════════════════════

import enum


class BridgeErrorCode(str, enum.Enum):
    """桥接错误码枚举（str + Enum 双继承，可当字符串比较，也有 enum 的类型安全）"""
    NOT_CONFIGURED = "BRIDGE_NOT_CONFIGURED"     # 未注入端口/token 环境变量
    PORT_CLOSED = "BRIDGE_PORT_CLOSED"          # 端口无人监听（孤儿进程 / 主进程未启动）
    TOKEN_REJECTED = "BRIDGE_TOKEN_REJECTED"    # 401/403：主进程重启后 token 已更新
    HTTP_ERROR = "BRIDGE_HTTP_ERROR"            # 其他 HTTP 非 2xx
    TIMEOUT = "BRIDGE_TIMEOUT"                  # HTTP 层超时
    CONNECT_ERROR = "BRIDGE_CONNECT_ERROR"      # 底层 TCP 连接失败
    UNKNOWN = "BRIDGE_UNKNOWN"


def classify_bridge_error(err: unknown) -> BridgeErrorCode:
    """反向推断错误根因（用于日志 + 结构化异常），返回 BridgeErrorCode 枚举值"""
    if isinstance(err, BridgeError):
        return err.code
    msg = (err.message if hasattr(err, "message") else str(err)).lower()

    if "not configured" in msg or "未配置" in msg:
        return BridgeErrorCode.NOT_CONFIGURED
    if "401" in msg or "403" in msg or "unauthorized" in msg:
        return BridgeErrorCode.TOKEN_REJECTED
    if "timeout" in msg or "超时" in msg:
        return BridgeErrorCode.TIMEOUT
    if any(k in msg for k in ("connection", "all connection", "connect error", "refused")):
        return BridgeErrorCode.PORT_CLOSED
    return BridgeErrorCode.UNKNOWN


class BridgeError(RuntimeError):
    """结构化桥接错误 — 同时携带 code（机器可读） + suggestion（用户指引）"""

    # 类方法：按 code 给出可操作化建议（类级别数据，所有实例共享，不从 self 访问）
    @classmethod
    def suggestion_for(cls, code: BridgeErrorCode) -> str:
        return _BRIDGE_ERROR_SUGGESTIONS.get(code, _BRIDGE_ERROR_SUGGESTIONS[BridgeErrorCode.UNKNOWN])

    def __init__(self, code: BridgeErrorCode, detail: str = "", original: Exception | None = None):
        self.code = code
        self.detail = detail
        self.original = original
        self.suggestion = self.suggestion_for(code)
        super().__init__(f"[{code.value}] {detail}" if detail else f"[{code.value}] {self.suggestion}")

    @classmethod
    def from_exception(cls, err: unknown) -> BridgeError:
        """从底层异常自动构造结构化 BridgeError"""
        code = classify_bridge_error(err)
        if isinstance(err, BridgeError):
            return err
        detail = err.message if hasattr(err, "message") else str(err)
        original = err if isinstance(err, Exception) else None
        return cls(code, detail, original)


# 桥接错误 → 用户建议映射（模块级常量，BridgeError 类外维护便于扩展）
_BRIDGE_ERROR_SUGGESTIONS: dict[BridgeErrorCode, str] = {
    BridgeErrorCode.NOT_CONFIGURED: "Electron 主进程未向后端注入桥接配置。请在 Electron 主进程中启动后，再触发定时任务。",
    BridgeErrorCode.PORT_CLOSED: "Electron 主进程桥接端口无响应。可能主进程未启动，或端口/token 已过期。请完整重启应用，使主进程与后端一起重启。",
    BridgeErrorCode.TOKEN_REJECTED: "Electron 桥接 token 校验失败（主进程已重启但后端仍用旧 token）。请完整重启应用。",
    BridgeErrorCode.HTTP_ERROR: "Electron 桥接服务返回错误。请查看主进程日志（终端输出）了解详情。",
    BridgeErrorCode.TIMEOUT: "Electron 桥接请求超时。主进程可能繁忙或无响应，请检查主进程是否正常运行。",
    BridgeErrorCode.CONNECT_ERROR: "无法连接到 Electron 主进程桥接端口。请完整重启应用。",
    BridgeErrorCode.UNKNOWN: "桥接通信异常。请查看主进程日志并尝试重启应用。",
}

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

    def _raise_unavailable(self) -> None:
        """桥接未配置时抛出结构化 BridgeError（帮助上层区分真正网络错误 vs 配置缺失）"""
        raise BridgeError(
            BridgeErrorCode.NOT_CONFIGURED,
            f"port={self.port} base_url={self.base_url}",
        )

    async def ping(self, timeout: float = 3.0) -> bool:
        """检测 Electron bridge 服务是否可达且 token 有效。

        用于定时任务执行前的快速健康检查，避免带着过期端口/token 去建 SSH 会话。
        对已知路径发一次 POST：任何 HTTP 响应（含 400/500）都证明桥服务存活
        + token 有效；只有 ConnectError / 超时 才返回 False。
        """
        if not self.is_available():
            return False
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=timeout
            ) as client:
                await client.post(
                    "/ssh/exec",
                    json={"sshSessionId": "", "command": ""},
                    headers={"X-Bridge-Token": self.token},
                )
                return True
        except httpx.ConnectError:
            return False
        except httpx.TimeoutException:
            logger.warning("Electron bridge 探活超时 (port=%s)", self.port)
            return False
        except Exception:
            # 其他 HTTP 错误（400/500 等）说明桥服务端正常处理了请求
            return True

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
            self._raise_unavailable()

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
            self._raise_unavailable()

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
            self._raise_unavailable()

        return await self._post(
            "/ssh/disconnect",
            {"sessionId": session_id},
            timeout + _HTTP_TIMEOUT_MARGIN,
        )

    async def _post(self, path: str, payload: dict, http_timeout: float) -> dict:
        """向桥接服务发送 JSON POST 请求

        v2 改进：按失败原因抛出结构化 BridgeError（带 code + suggestion），
        上层 (schedule_engine) 可按 code 区分根因并写入 run summary 给用户明确指引。
        """
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
            raise BridgeError(BridgeErrorCode.TIMEOUT, f"path={path}")
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 优先透传服务端给出的可读错误
            try:
                detail = (e.response.json() or {}).get("error", "")
            except Exception:
                detail = ""
            logger.warning(
                "Electron bridge 返回错误状态 %s (path=%s): %s",
                status, path, e.response.text[:200],
            )
            if status in (401, 403):
                raise BridgeError(
                    BridgeErrorCode.TOKEN_REJECTED,
                    detail or f"HTTP {status}",
                )
            raise BridgeError(
                BridgeErrorCode.HTTP_ERROR,
                detail or f"HTTP {status}",
            )
        except httpx.ConnectError:
            logger.warning("Electron bridge 连接失败 (port=%s)", self.port)
            raise BridgeError(
                BridgeErrorCode.PORT_CLOSED,
                f"127.0.0.1:{self.port}",
            )
