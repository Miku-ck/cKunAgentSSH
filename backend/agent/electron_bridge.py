"""Electron Bridge — Python 后端调用 Electron 主进程能力的客户端

传输层（阶段 1，见 docs/superpowers/specs/2026-09-14-bridge-ws-channel-design.md）：
- WS 优先：主进程反向连接本进程 /ws/bridge（bridge_relay 持有），命令经 WS 下发
- HTTP 兜底：原有 127.0.0.1:<port> 端口桥，WS 不可用时的回退通道
- 公开 API（is_available/probe/ping/exec_ssh_command/connect_ssh/disconnect_ssh）不变，
  4 个调用方（schedule_engine / ops_common / shell_exec / saddle）零改动
- 断线时在途请求 fail-closed：绝不回 HTTP 重发同一请求（远程命令非幂等）
"""
from __future__ import annotations

import asyncio
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
    TIMEOUT = "BRIDGE_TIMEOUT"                  # 连接/读取超时（端口假监听、主进程繁忙）
    WS_LOST = "BRIDGE_WS_LOST"                  # WS 通道丢失（无连接 / 在途断线 / 等待响应超时）
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


# 桥接错误 → 用户建议映射（模块级常量，BridgeError 类外维护便于扩展）
_BRIDGE_ERROR_SUGGESTIONS: dict[BridgeErrorCode, str] = {
    BridgeErrorCode.NOT_CONFIGURED: "Electron 主进程未向后端注入桥接配置。请在 Electron 主进程中启动后，再触发定时任务。",
    BridgeErrorCode.PORT_CLOSED: "Electron 主进程桥接端口无响应。可能主进程未启动，或端口/token 已过期。请完整重启应用，使主进程与后端一起重启。",
    BridgeErrorCode.TOKEN_REJECTED: "Electron 桥接 token 校验失败（主进程已重启但后端仍用旧 token）。请完整重启应用。",
    BridgeErrorCode.HTTP_ERROR: "Electron 桥接服务返回错误。请查看主进程日志（终端输出）了解详情。",
    BridgeErrorCode.TIMEOUT: (
        "Electron 桥接端口连接超时（端口可能在监听但无响应，即\"假监听\"）。"
        "请查看 electron-bridge.log 中是否出现该端口的 REQ 记录："
        "无 REQ 说明请求根本未到达主进程，请重启应用以重建桥接端口。"
    ),
    BridgeErrorCode.WS_LOST: (
        "Electron 桥接反向连接（WS）不可用或已断开（主进程可能在重启）。"
        "请重试该任务；若持续出现请完整重启应用。"
    ),
    BridgeErrorCode.UNKNOWN: "桥接通信异常。请查看主进程日志并尝试重启应用。",
}

logger = logging.getLogger(__name__)

# httpx 超时比 SSH 超时多出的余量（秒），确保 SSH 侧超时先触发，
# Electron 返回错误响应后 httpx 正常收到结果，避免两侧同时超时导致竞争。
_HTTP_TIMEOUT_MARGIN = 5.0

# WS 通道"短等待"预算（秒）：任务触发时 WS 未连接（典型场景：后端刚被
# BackendManager 自动重启，主进程重连在途）最多等这么久；仍不通再走 HTTP 兜底。
_WS_CONNECT_WAIT_S = 5.0
_WS_POLL_S = 0.2


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

    # ── WS 传输层（阶段 1 主链路）────────────────────

    def _ws_connected(self) -> bool:
        from agent.bridge_relay import get_bridge_relay
        relay = get_bridge_relay()
        return relay is not None and relay.connected

    async def _wait_ws(self, budget: float = _WS_CONNECT_WAIT_S) -> bool:
        """最多等 budget 秒让 WS 通道就绪（每 0.2s 轮询一次）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while True:
            if self._ws_connected():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_WS_POLL_S)

    async def _ws_request(self, op: str, payload: dict, timeout: float) -> dict:
        """经 WS 通道下发一条请求并等待结果。

        超时语义与 HTTP 路径对齐：timeout + 余量，让主进程侧 SSH 超时先触发。
        4xx/5xx 业务失败 → BridgeError(HTTP_ERROR)；通道丢失 → BridgeWsLostError
        （由调用方 _post_ws_or_http 归一为 WS_LOST）。
        """
        from agent.bridge_relay import BridgeWsLostError, get_bridge_relay

        relay = get_bridge_relay()
        if relay is None or not relay.connected:
            raise BridgeWsLostError("bridge ws not connected")
        try:
            status, result = await relay.send_request(op, payload, timeout + _HTTP_TIMEOUT_MARGIN)
        except BridgeWsLostError:
            raise
        except Exception as e:
            raise BridgeWsLostError(str(e)) from e
        if status >= 400:
            detail = (result.get("error", "") if isinstance(result, dict) else "") or f"WS status {status}"
            raise BridgeError(BridgeErrorCode.HTTP_ERROR, detail)
        return result if isinstance(result, dict) else {}

    async def _post_ws_or_http(
        self, op: str, path: str, payload: dict, timeout: float, http_timeout: float
    ) -> dict:
        """传输选择：调用瞬间 WS 已连接 → WS；否则 → 现有 HTTP 兜底路径。

        fail-closed：WS 在途断线只抛 WS_LOST，**不回落 HTTP 重发同一请求**
        （远程命令可能已部分执行，重发 = 双执行）。
        """
        from agent.bridge_relay import BridgeWsLostError

        if self._ws_connected():
            try:
                return await self._ws_request(op, payload, timeout)
            except BridgeWsLostError as e:
                raise BridgeError(BridgeErrorCode.WS_LOST, str(e), original=e)
        return await self._post(path, payload, http_timeout)

    def _new_client(self, timeout: float) -> httpx.AsyncClient:
        """构造桥接专用 HTTP 客户端。

        trust_env=False 是硬性要求，不能省：
        桥接是纯回环（127.0.0.1）进程间通信，绝不应受 HTTP_PROXY / HTTPS_PROXY /
        ALL_PROXY / NO_PROXY 等环境变量影响。httpx 默认 trust_env=True，若宿主
        环境注入了代理（实测本机存在 HTTP_PROXY=http://127.0.0.1:<port> 这类值），
        发往 127.0.0.1:30000 的请求会被交给代理转发；代理不转发回环地址或未运行时
        表现为 ConnectTimeout —— 症状与"桥接端口不可达"几乎无法区分，极易误导排查。
        """
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, trust_env=False
        )

    async def probe(self, timeout: float = 3.0) -> BridgeErrorCode | None:
        """探测桥接可达性：返回 None 表示可达，否则返回具体失败原因码。

        相比旧实现（用 /ssh/exec 发空参数反推）的修正：
          1. 改用专用 /ping 端点 —— 探活不再依赖 SSH 业务参数校验。旧实现靠
             "400 响应 ⇒ 桥存活"间接判断，语义错位且与业务逻辑耦合。
          2. 不再把所有失败一律归类为 PORT_CLOSED，而是区分超时/连接拒绝/token
             失效 —— 之前"端口假监听导致的超时"被报成"端口关闭"，把排查方向
             指向了错误的结论。
          3. 阶段 1 起 WS 优先：反向连接已建立即视为可达（通道即健康）；
             未建立时最多等 _WS_CONNECT_WAIT_S 秒（覆盖后端刚重启、主进程
             重连在途的窗口），仍不通再对 HTTP 兜底通道探活。
        """
        if not self.is_available():
            return BridgeErrorCode.NOT_CONFIGURED
        if await self._wait_ws(_WS_CONNECT_WAIT_S):
            return None
        return await self._probe_http(timeout)

    async def _probe_http(self, timeout: float) -> BridgeErrorCode | None:
        """HTTP 兜底通道的 /ping 探活（WS 不可用时调用）。"""
        try:
            async with self._new_client(timeout) as client:
                resp = await client.post(
                    "/ping", headers={"X-Bridge-Token": self.token}
                )
                if resp.status_code in (401, 403):
                    return BridgeErrorCode.TOKEN_REJECTED
                return None
        except httpx.ConnectError as e:
            logger.warning("Electron bridge 连接被拒绝 (port=%s): %s", self.port, e)
            return BridgeErrorCode.PORT_CLOSED
        except httpx.TimeoutException as e:
            # ConnectTimeout 与 ReadTimeout 都归此处。前者意味着 TCP 握手都未完成
            # （端口在 LISTEN 但 SYN 无应答 —— 典型"假监听"），后者意味着已连上但
            # 主进程未回包。两者都应提示"重启应用重建桥接"，而非"端口未监听"。
            logger.warning(
                "Electron bridge 探活超时 (port=%s, %s): 端口无响应",
                self.port,
                type(e).__name__,
            )
            return BridgeErrorCode.TIMEOUT
        except Exception as e:
            logger.warning("Electron bridge 探活异常 (port=%s): %s", self.port, e)
            return BridgeErrorCode.UNKNOWN

    async def ping(self, timeout: float = 3.0) -> bool:
        """桥接是否可达（布尔薄封装）。需要具体失败原因时请直接调用 probe()。"""
        return await self.probe(timeout) is None

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

        # 超时 > SSH 超时，确保 SSH 侧先超时并返回错误响应（WS/HTTP 两通道同语义）
        http_timeout = timeout + _HTTP_TIMEOUT_MARGIN

        return await self._post_ws_or_http(
            "ssh_exec",
            "/ssh/exec",
            {
                "sshSessionId": ssh_session_id,
                "command": command,
                "timeout": timeout,
            },
            timeout,
            http_timeout,
        )

    async def connect_ssh(self, connection_id: str, timeout: float = 60.0) -> dict:
        """按连接 ID 自动建立一条轻量 SSH 会话（供定时任务无头执行）。

        返回结构: {success: bool, sessionId: str, name: str}
        """
        if not self.is_available():
            self._raise_unavailable()

        # 建连可能耗时（readyTimeout 默认 15s），放宽超时余量
        http_timeout = timeout + _HTTP_TIMEOUT_MARGIN
        return await self._post_ws_or_http(
            "ssh_connect",
            "/ssh/connect",
            {"connectionId": connection_id},
            timeout,
            http_timeout,
        )

    async def disconnect_ssh(self, session_id: str, timeout: float = 15.0) -> dict:
        """关闭由 connect_ssh 建立的 SSH 会话，释放连接资源。"""
        if not self.is_available():
            self._raise_unavailable()

        return await self._post_ws_or_http(
            "ssh_disconnect",
            "/ssh/disconnect",
            {"sessionId": session_id},
            timeout,
            timeout + _HTTP_TIMEOUT_MARGIN,
        )

    async def _post(self, path: str, payload: dict, http_timeout: float) -> dict:
        """向桥接服务发送 JSON POST 请求

        v2 改进：按失败原因抛出结构化 BridgeError（带 code + suggestion），
        上层 (schedule_engine) 可按 code 区分根因并写入 run summary 给用户明确指引。
        """
        try:
            async with self._new_client(http_timeout) as client:
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
