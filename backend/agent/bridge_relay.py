"""Bridge WS relay — Electron 主进程反向连接通道（阶段 1）的服务端中枢。

拓扑（与旧 HTTP 端口桥相反）：
    主进程出站拨号 ws://127.0.0.1:<backend>/ws/bridge（出站长连接不受回环入站
    防火墙规则影响）→ hello token 握手 → 后端经该连接下发命令帧 req 并等待
    主进程响应帧 res。旧 HTTP 端口桥保留为兜底通道。

设计文档：docs/superpowers/specs/2026-09-14-bridge-ws-channel-design.md

关键约束：
- send_request 必须与 /ws/bridge 端点在同一事件循环调用。生产上成立：
  schedule_engine / ops_common 与 WS 端点同进程、同在 uvicorn 循环。
- 断线时的在途请求立即失败，**绝不重发** —— 远程命令非幂等，重发即双执行。
- 同一时刻最多一条主进程连接；新连接完成 hello 后替换旧连接（新者胜，
  覆盖主进程重连竞态），旧连接 pending 请求全部失败。
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
import os

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)

# hello 帧必须在连接后该时间内到达，否则视为非桥接连接关闭
HELLO_TIMEOUT_S = 3.0

# 关闭码（应用自定义段 4000-4999）
CLOSE_TOKEN_REJECTED = 4401   # token 不符 / 首帧非 hello
CLOSE_PROTOCOL_ERROR = 1002   # 帧非 JSON 等协议错误
CLOSE_REPLACED = 4000         # 被新连接替换
CLOSE_TRY_LATER = 1013        # relay 未就绪（lifespan 未启动）


class BridgeWsLostError(RuntimeError):
    """桥接 WS 通道不可用：无连接 / 发送时连接丢失 / 等待响应超时。

    调用方（electron_bridge.ElectronBridge）将其映射为
    BridgeErrorCode.WS_LOST 的结构化 BridgeError。
    """


class BridgeRelay:
    """持有至多一条主进程 WS 连接，负责鉴权、请求/响应复用与断线失效。"""

    def __init__(self) -> None:
        self._ws: WebSocket | None = None
        self._hello_ok = False
        self._lock = asyncio.Lock()  # 保护 _ws/_hello_ok 槽位切换
        self._pending: dict[str, asyncio.Future] = {}
        self._id_counter = itertools.count(1)

    # ── 对外查询 ────────────────────────────────────

    @property
    def connected(self) -> bool:
        """WS 通道是否就绪（已连接且 hello 鉴权通过）。"""
        return self._ws is not None and self._hello_ok

    async def send_request(self, op: str, payload: dict, timeout: float) -> tuple[int, dict | None]:
        """下发一条 req 帧并等待对应 res 帧。

        返回 (status, result)：status 与 HTTP 状态码语义对齐（2xx 成功，
        4xx/5xx 业务失败），result 为 res 帧的 result 字段（dict）。
        通道不可用 / 发送时断线 / 等待超时 → BridgeWsLostError。
        """
        ws = self._ws
        if ws is None or not self._hello_ok:
            raise BridgeWsLostError("bridge ws not connected")

        # 锁内注册并复核槽位：若捕获的 socket 恰在注册前被新连接替换，
        # 立即失败而非在将被关闭的旧 socket 上发送、悬挂到超时
        async with self._lock:
            if self._ws is not ws:
                raise BridgeWsLostError("bridge connection replaced before send")
            req_id = str(next(self._id_counter))
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut
        try:
            await ws.send_text(
                json.dumps(
                    {"type": "req", "id": req_id, "op": op, "payload": payload},
                    ensure_ascii=False,
                )
            )
        except (RuntimeError, WebSocketDisconnect) as e:
            # 发送时 socket 已死（对端刚断开）— 归为通道丢失
            raise BridgeWsLostError(f"send failed: {e}") from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise BridgeWsLostError(f"request timeout op={op} after {timeout}s") from None
        finally:
            self._pending.pop(req_id, None)

    # ── 连接生命周期 ────────────────────────────────

    async def handle_bridge_ws(self, ws: WebSocket) -> None:
        """一条 /ws/bridge 连接的全部生命周期：accept → hello → 读循环。"""
        try:
            await ws.accept()
        except WebSocketDisconnect:
            return

        # 1) hello 鉴权：首帧必须为 {type:"hello", token}，3s 内到达
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            logger.warning("[BridgeRelay] hello 超时（%.1fs），关闭连接", HELLO_TIMEOUT_S)
            await self._safe_close(ws, CLOSE_TRY_LATER)
            return
        except WebSocketDisconnect:
            return
        except (json.JSONDecodeError, ValueError):
            logger.warning("[BridgeRelay] hello 帧非 JSON，关闭连接")
            await self._safe_close(ws, CLOSE_PROTOCOL_ERROR)
            return

        if not isinstance(msg, dict) or msg.get("type") != "hello" or not self._token_ok(str(msg.get("token", ""))):
            logger.warning("[BridgeRelay] hello 鉴权失败（token 不符或首帧非 hello）")
            await self._safe_close(ws, CLOSE_TOKEN_REJECTED)
            return

        # 2) 新者胜：替换旧连接（若存在）。旧连接的在途请求不可能再被应答，全部失败。
        #    原子换掉 pending 字典再失败旧字典：替换后新连接注册的请求落在新字典，
        #    不会被误杀。
        async with self._lock:
            old = self._ws
            self._ws = ws
            self._hello_ok = True
            if old is not None and old is not ws:
                stale_pending, self._pending = self._pending, {}
        if old is not None and old is not ws:
            logger.info("[BridgeRelay] 新 bridge 连接替换旧连接")
            for fut in stale_pending.values():
                if not fut.done():
                    fut.set_exception(BridgeWsLostError("bridge connection replaced"))
            await self._safe_close(old, CLOSE_REPLACED)

        await self._safe_send(ws, json.dumps({"type": "hello-ack", "ok": True}))
        logger.info("[BridgeRelay] bridge ws channel ready")

        # 3) 读循环：res 完成 Future / ping 回 pong；主进程不应发 req（忽略并告警）
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("[BridgeRelay] 收到非 JSON 帧，关闭连接")
                    await self._safe_close(ws, CLOSE_PROTOCOL_ERROR)
                    return
                mtype = msg.get("type") if isinstance(msg, dict) else None
                if mtype == "res":
                    fut = self._pending.pop(str(msg.get("id", "")), None)
                    if fut is not None and not fut.done():
                        try:
                            status = int(msg.get("status") or 200)
                        except (TypeError, ValueError):
                            status = 500
                        fut.set_result((status, msg.get("result")))
                elif mtype == "ping":
                    await self._safe_send(ws, json.dumps({"type": "pong"}))
                elif mtype == "pong":
                    pass  # 后端→主进程方向的响应帧，仅消费
                elif mtype == "req":
                    logger.warning("[BridgeRelay] 协议方向错误：主进程发来 req，忽略")
                # 未知帧类型：忽略（前向兼容）
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("[BridgeRelay] bridge ws 读循环异常")
        finally:
            # 仅当本连接仍是当前连接时才清空失败 pending；若已被新连接替换，
            # 替换路径已原子换掉字典并失败旧请求，此处不得触碰新连接的 pending
            async with self._lock:
                if self._ws is ws:
                    self._ws = None
                    self._hello_ok = False
                    self._fail_all_pending("bridge connection closed")
            logger.info("[BridgeRelay] bridge ws channel closed")

    # ── 内部工具 ────────────────────────────────────

    def _token_ok(self, token: str) -> bool:
        """与自身 env 的 ELECTRON_BRIDGE_TOKEN 恒定时间比对。

        env 为空（主进程桥接降级未注入 token）时一律拒绝 —— 此时 WS 通道
        本就不可用（HTTP 兜底也不可用），拒绝是正确行为。
        """
        expected = os.environ.get("ELECTRON_BRIDGE_TOKEN", "")
        if not expected:
            return False
        return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))

    def _fail_all_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(BridgeWsLostError(reason))

    async def _safe_close(self, ws: WebSocket, code: int) -> None:
        try:
            await ws.close(code=code)
        except Exception:
            pass

    async def _safe_send(self, ws: WebSocket, data: str) -> None:
        try:
            await ws.send_text(data)
        except Exception:
            pass


# ── 模块级单例注册（lifespan 创建/销毁；端点经此获取，便于测试注入）────────────

_relay: BridgeRelay | None = None


def get_bridge_relay() -> BridgeRelay | None:
    return _relay


def set_bridge_relay(relay: BridgeRelay | None) -> None:
    global _relay
    _relay = relay


async def bridge_ws_endpoint(websocket: WebSocket) -> None:
    """FastAPI /ws/bridge 端点：取当前 relay 并移交连接生命周期。"""
    relay = get_bridge_relay()
    if relay is None:
        # lifespan 未启动（理论上 uvicorn 先完成 lifespan 再接连接，防御性兜底）
        try:
            await websocket.accept()
            await websocket.close(code=CLOSE_TRY_LATER)
        except WebSocketDisconnect:
            pass
        return
    await relay.handle_bridge_ws(websocket)
