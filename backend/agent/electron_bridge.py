"""Electron Bridge — Python 后端调用 Electron 主进程能力的客户端

传输层（阶段 2 收尾，历史设计文档已退役，口径以本文件为准）：
- WS-only：主进程反向连接本进程 /ws/bridge（bridge_relay 持有），命令经 WS 下发
- HTTP 端口桥已移除（出站拨号不受防火墙入站规则影响，无需兜底）
- 公开 API（is_available/probe/ping/exec_ssh_command/connect_ssh/disconnect_ssh）不变，
  4 个调用方（schedule_engine / ops_common / shell_exec / saddle）零改动
- 断线时在途请求 fail-closed：抛 WS_LOST，绝不重发（远程命令非幂等）
"""
from __future__ import annotations

import logging
import os

# ════════════════════════════════════════════════
#   桥接错误分类（与 electron 侧 SSHError 遥相呼应，但独立体系）
# ════════════════════════════════════════════════

import enum


class BridgeErrorCode(str, enum.Enum):
    """桥接错误码枚举（str + Enum 双继承，可当字符串比较，也有 enum 的类型安全）"""
    NOT_CONFIGURED = "BRIDGE_NOT_CONFIGURED"     # 未注入 token 环境变量
    HTTP_ERROR = "BRIDGE_HTTP_ERROR"            # WS 业务失败（主进程返回 4xx/5xx）
    WS_LOST = "BRIDGE_WS_LOST"                  # WS 通道丢失（无连接 / 在途断线 / 等待响应超时）
    UNKNOWN = "BRIDGE_UNKNOWN"


def classify_bridge_error(err: object) -> BridgeErrorCode:
    """反向推断错误根因（用于日志 + 结构化异常），返回 BridgeErrorCode 枚举值"""
    if isinstance(err, BridgeError):
        return err.code
    msg = (err.message if hasattr(err, "message") else str(err)).lower()

    if "not configured" in msg or "未配置" in msg:
        return BridgeErrorCode.NOT_CONFIGURED
    if "401" in msg or "403" in msg or "unauthorized" in msg:
        return BridgeErrorCode.HTTP_ERROR
    if "timeout" in msg or "超时" in msg:
        return BridgeErrorCode.WS_LOST
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
        # S7-14：原实现是「detail 非空就只报 detail」，于是同一种错误有四条文案 ——
        # 只有 probe 那一条（不带 detail）能把「请查看主进程日志」这类可操作指引带出来。
        # 三条带 detail 的 raise（未注入 token / 空 sessionId / 反向推断的原始异常）
        # 恰好是最需要指引的那几种，用户只看到一段技术串。
        # 两段都要：detail 是给排查者的根因，suggestion 是给用户的下一步。
        # 不会干扰反向推断 —— classify_bridge_error 对 BridgeError 实例直接取 code，
        # 不去嗅探文本（这条由测试钉住，见 test_bridge_error_suggestion_s714.py）。
        message = f"[{code.value}] {self.suggestion}"
        if detail:
            message = f"[{code.value}] {detail}｜{self.suggestion}"
        super().__init__(message)


# 桥接错误 → 用户建议映射（模块级常量，BridgeError 类外维护便于扩展）
_BRIDGE_ERROR_SUGGESTIONS: dict[BridgeErrorCode, str] = {
    BridgeErrorCode.NOT_CONFIGURED: "Electron 主进程未向后端注入桥接 token。请在 Electron 主进程中启动后，再触发定时任务。",
    BridgeErrorCode.HTTP_ERROR: "Electron 桥接服务返回错误。请查看主进程日志（终端输出）了解详情。",
    BridgeErrorCode.WS_LOST: (
        "Electron 桥接反向连接（WS）不可用或已断开（主进程可能在重启）。"
        "请重试该任务；若持续出现请完整重启应用。"
    ),
    BridgeErrorCode.UNKNOWN: "桥接通信异常。请查看主进程日志并尝试重启应用。",
}

logger = logging.getLogger(__name__)

# WS 等待响应比 SSH 超时多出的余量（秒），确保 SSH 侧超时先触发，
# Electron 返回错误响应后 WS 正常收到结果，避免两侧同时超时导致竞争。
_HTTP_TIMEOUT_MARGIN = 5.0

# WS 通道"短等待"预算（秒）：任务触发时 WS 未连接（典型场景：后端刚被
# BackendManager 自动重启，主进程重连在途）最多等这么久。
_WS_CONNECT_WAIT_S = 5.0
# 探活专用预算：健康检查高频且非关键路径，等通道的时间应远小于任务触发路径
# （事件驱动后等待零轮询延迟，1s 足以覆盖重连在途窗口）。
_WS_PROBE_WAIT_S = 1.0


class ElectronBridge:
    """调用 Electron 主进程桥接服务的客户端（WS-only）"""

    def __init__(self) -> None:
        self.token = os.environ.get("ELECTRON_BRIDGE_TOKEN", "")

    def is_available(self) -> bool:
        return bool(self.token)

    def _raise_unavailable(self) -> None:
        """桥接未配置时抛出结构化 BridgeError（帮助上层区分真正网络错误 vs 配置缺失）"""
        raise BridgeError(BridgeErrorCode.NOT_CONFIGURED, "ELECTRON_BRIDGE_TOKEN 缺失")

    # ── WS 传输层 ────────────────────────────────────

    async def _wait_ws(self, budget: float | None = None) -> bool:
        """最多等 budget 秒让 WS 通道就绪（事件驱动，无轮询）。

        budget 默认取调用时的模块常量（保持可被测试 monkeypatch），
        就绪事件由 bridge_relay 在 hello 鉴权通过时置位。
        """
        from agent.bridge_relay import get_bridge_relay

        if budget is None:
            budget = _WS_CONNECT_WAIT_S
        relay = get_bridge_relay()
        if relay is None:
            return False
        return await relay.wait_ready(budget)

    async def _ws_request(self, op: str, payload: dict, timeout: float) -> dict:
        """经 WS 通道下发一条请求并等待结果。

        超时语义：timeout + 余量，让主进程侧 SSH 超时先触发。
        4xx/5xx 业务失败 → BridgeError(HTTP_ERROR)；通道丢失 → BridgeWsLostError
        （由调用方归一为 WS_LOST）。
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

    async def _send(self, op: str, payload: dict, timeout: float) -> dict:
        """WS 通道下发（fail-closed）：在途断线抛 WS_LOST，绝不重发同一请求。"""
        from agent.bridge_relay import BridgeWsLostError

        try:
            return await self._ws_request(op, payload, timeout)
        except BridgeWsLostError as e:
            raise BridgeError(BridgeErrorCode.WS_LOST, str(e), original=e)

    async def probe(self) -> BridgeErrorCode | None:
        """探测桥接可达性：返回 None 表示可达，否则返回具体失败原因码。

        阶段 2 WS-only：反向连接已建立即视为可达（通道即健康）；
        未建立时最多等 _WS_PROBE_WAIT_S 秒（事件驱动，覆盖后端刚重启、
        主进程重连在途的窗口），仍不通返回 WS_LOST。
        等待预算恒为 _WS_PROBE_WAIT_S：探活高频且非关键路径，调用方
        （schedule_engine 等）不应也不能放大它。
        """
        if not self.is_available():
            return BridgeErrorCode.NOT_CONFIGURED
        if await self._wait_ws(_WS_PROBE_WAIT_S):
            return None
        return BridgeErrorCode.WS_LOST

    async def ping(self) -> bool:
        """桥接是否可达（布尔薄封装）。需要具体失败原因时请直接调用 probe()。"""
        return await self.probe() is None

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

        return await self._send(
            "ssh_exec",
            {
                "sshSessionId": ssh_session_id,
                "command": command,
                "timeout": timeout,
            },
            timeout,
        )

    async def connect_ssh(self, connection_id: str, timeout: float = 60.0) -> dict:
        """按连接 ID 自动建立一条轻量 SSH 会话（供定时任务无头执行）。

        返回结构: {success: bool, sessionId: str, name: str}
        """
        if not self.is_available():
            self._raise_unavailable()

        return await self._send("ssh_connect", {"connectionId": connection_id}, timeout)

    async def list_connections(self, timeout: float = 15.0) -> list[dict]:
        """列出主进程保存的 SSH 连接配置（S7-09：定时任务需要目标主机 id）。

        返回 [{id, name, host, port, username, authType, environment}] —— 主进程侧
        已把凭据字段剥掉；这里只回读，不碰凭据。桥不可用时抛 BridgeError，由调用方
        决定降级文案（定时任务工具据此把「不知道有哪些主机」如实告诉模型）。
        """
        if not self.is_available():
            self._raise_unavailable()

        result = await self._send("ssh_list_connections", {}, timeout)
        connections = result.get("connections") if isinstance(result, dict) else None
        return connections if isinstance(connections, list) else []

    async def disconnect_ssh(self, session_id: str, timeout: float = 15.0) -> dict:
        """关闭由 connect_ssh 建立的 SSH 会话，释放连接资源。

        S0-06：这句限制现在有机制承载了 —— 主进程按「connect_ssh 建连时登记的归属
        集合」判，拿着别的会话 id 过来回 403 而不会拆掉用户的终端。会话 id 另带一次
        启动随机的段，因此 `agent_jobs` 里跨重启恢复的旧 id 也只会得到「会话不存在」。
        """
        if not self.is_available():
            self._raise_unavailable()

        return await self._send("ssh_disconnect", {"sessionId": session_id}, timeout)
