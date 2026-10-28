import asyncio
import logging
import threading

import netconfig

from bot.provider import (
    ChannelAlreadyConnectedError,
    ChannelConnectError,
    ChannelConfig,
)
from bot.qq_channel import (
    QQ_FAILURE_REPLY,
    QQ_REPLY_TIMEOUT,
    QQ_TIMEOUT_REPLY,
    QQInboundAdapter,
    split_qq_parts,
    strip_markdown_for_qq,
)

logger = logging.getLogger(__name__)

# botpy 在登录阶段映射 401→AuthenticationFailedError、403→ForbiddenError；
# 这两类重试再多次也不会变好，直接落到 error 状态等用户改配置。
FATAL_EXCEPTIONS = {"AuthenticationFailedError", "ForbiddenError"}
FRIENDLY_DETAILS = {
    "AuthenticationFailedError": "AppID 或 AppSecret 不正确，请到 QQ 开放平台核对后重试",
    "ForbiddenError": "QQ 机器人无权连接，请确认已在 QQ 开放平台启用并开通消息权限",
}
MISSING_DEPENDENCY_DETAIL = (
    "后端缺少 QQ 机器人依赖 qq-botpy，请在运行后端的 Python 中执行 pip install qq-botpy"
)
DEFAULT_RETRY_DELAYS = (5, 10, 30, 60)


class QQConnection:
    """一个 QQ 机器人的一条长连接。

    旧版这个类同时是全进程唯一的 manager，所以「已在运行」是全局的；
    现在它只管自己，多个机器人由 ChannelRuntime 各起一个实例。
    """

    def __init__(self, client_factory=None, retry_delays=DEFAULT_RETRY_DELAYS):
        self.client_factory = client_factory or self._create_botpy_client
        self.retry_delays = tuple(retry_delays) or DEFAULT_RETRY_DELAYS
        self._thread: threading.Thread | None = None
        self._loop = None
        self._main_task = None
        self._status = {"state": "offline", "detail": ""}
        self._status_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()
        self._connect_ready = threading.Event()
        self._connect_result = (False, "", False)
        self._was_online = False
        self._retry_index = 0

    @property
    def status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, state: str, detail: str = "") -> None:
        with self._status_lock:
            self._status = {"state": state, "detail": detail}

    def _finish_connect(self, ok: bool, detail: str, fatal: bool) -> None:
        if self._connect_ready.is_set():
            return
        self._connect_result = (ok, detail, fatal)
        self._connect_ready.set()

    def _create_botpy_client(self, processor, on_ready):
        try:
            import botpy
        except ImportError as exc:
            # 解释器由 Electron 按 PATH 探测，换一个 Python 启动就可能缺依赖；
            # 裸 ModuleNotFoundError 文案在界面上没法行动。
            raise ChannelConnectError(MISSING_DEPENDENCY_DETAIL, fatal=True) from exc

        class Client(botpy.Client):
            async def on_ready(self):
                await on_ready()

            async def on_group_at_message_create(self, message):
                await processor.handle_group(message)

            async def on_c2c_message_create(self, message):
                await processor.handle_c2c(message)

        return Client(intents=botpy.Intents(public_messages=True), timeout=30)

    @staticmethod
    def _is_fatal(exc: BaseException) -> bool:
        return type(exc).__name__ in FATAL_EXCEPTIONS

    @staticmethod
    def _detail_for(exc: BaseException) -> str:
        friendly = FRIENDLY_DETAILS.get(type(exc).__name__)
        if friendly:
            return friendly
        text = netconfig.redact_urls_in_text(str(exc)).strip()
        return text or type(exc).__name__

    @staticmethod
    def _connect_error(exc: BaseException | None) -> ChannelConnectError:
        if exc is None:
            return ChannelConnectError("QQ 连接在握手完成前断开，请稍后重试")
        # 工厂内部已经判定过 fatal 的错误原样上抛，别被按类名重新判成可重试。
        if isinstance(exc, ChannelConnectError):
            return exc
        return ChannelConnectError(
            QQConnection._detail_for(exc),
            fatal=QQConnection._is_fatal(exc),
        )

    async def start(
        self,
        cfg: ChannelConfig,
        on_inbound,
        *,
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> None:
        """启动连接；默认等 botpy 登录成功后返回，失败时抛 ChannelConnectError。

        开机恢复走 wait_ready=False，避免握手超时把整个启动流程拖住。
        """
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                raise ChannelAlreadyConnectedError("QQ 机器人已在运行")
            self._stopping = threading.Event()
            self._connect_ready = threading.Event()
            self._connect_result = (False, "", False)
            self._was_online = False
            self._retry_index = 0
            self._set_status("connecting")
            self._thread = threading.Thread(
                target=self._run,
                args=(cfg, on_inbound),
                # 线程名带渠道 id：多个机器人同时挂着时，线程 dump 里要分得出是谁。
                name=f"ckunbot-qq-{cfg.channel_id[:8]}",
                daemon=True,
            )
            self._thread.start()

        if wait_ready:
            await self._await_connect(ready_timeout)

    async def _await_connect(self, timeout: float) -> None:
        ready = await asyncio.to_thread(self._connect_ready.wait, timeout)
        if not ready:
            detail = "连接 QQ 服务器超时，请检查网络后重试"
            self._set_status("error", detail)
            await self._stop_worker()
            raise ChannelConnectError(detail)
        ok, detail, fatal = self._connect_result
        if not ok:
            await self._stop_worker()
            raise ChannelConnectError(detail or "QQ 连接失败", fatal=fatal)

    def _run(self, cfg: ChannelConfig, on_inbound) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._main_task = loop.create_task(self._main(cfg, on_inbound))
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._finish_connect(False, "连接已停止", False)
            self._loop = None
            self._main_task = None
            if self.status["state"] in ("connecting", "reconnecting"):
                self._set_status("offline")
            loop.close()

    async def _main(self, cfg: ChannelConfig, on_inbound) -> None:
        try:
            await self._supervise(cfg, on_inbound)
        except asyncio.CancelledError:
            raise
        except ChannelConnectError as exc:
            logger.warning("QQ 通道连接失败: %s", exc.detail)
            self._set_status("error", exc.detail)
            self._finish_connect(False, exc.detail, exc.fatal)
        except Exception as exc:
            detail = self._detail_for(exc)
            logger.warning("QQ 通道异常退出: %s", detail, exc_info=True)
            self._set_status("error", detail)
            self._finish_connect(False, detail, self._is_fatal(exc))

    async def _supervise(self, cfg: ChannelConfig, on_inbound) -> None:
        while True:
            if self._stopping.is_set():
                return
            client = None
            error = None
            try:
                adapter = QQInboundAdapter(cfg.channel_id, on_inbound)
                client = self.client_factory(adapter, self._on_client_ready)
                connection = await client.start(cfg.app_id, cfg.app_secret, ret_coro=True)
                if connection:
                    await connection
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass

            if self._stopping.is_set():
                return
            # 首次握手失败不自动重试：配置错就立刻报错，别把凭据错误刷成无限重连。
            if not self._was_online:
                raise self._connect_error(error)
            if error is not None and self._is_fatal(error):
                raise self._connect_error(error)
            delay = self.retry_delays[min(self._retry_index, len(self.retry_delays) - 1)]
            self._retry_index += 1
            self._set_status(
                "reconnecting",
                f"{delay:g} 秒后进行第 {self._retry_index} 次重连",
            )
            await asyncio.sleep(delay)

    async def _on_client_ready(self) -> None:
        self._was_online = True
        self._retry_index = 0
        self._set_status("online")
        self._finish_connect(True, "", False)

    async def _stop_worker(self) -> bool:
        with self._lifecycle_lock:
            self._stopping.set()
            thread = self._thread
            loop = self._loop
            task = self._main_task
        if not thread:
            return True
        if loop and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        await asyncio.to_thread(thread.join, 5)
        with self._lifecycle_lock:
            if thread.is_alive():
                self._set_status("error", "QQ 连接未能在 5 秒内停止")
                return False
            if self._thread is thread:
                self._thread = None
        return True

    async def stop(self) -> None:
        if await self._stop_worker():
            self._set_status("offline")


class QQProvider:
    """QQ 渠道的 provider 实现（契约见 bot/provider.py）。"""

    name = "qq"
    reply_window_s = QQ_REPLY_TIMEOUT

    def __init__(self, client_factory=None, retry_delays=DEFAULT_RETRY_DELAYS):
        self.client_factory = client_factory
        self.retry_delays = retry_delays

    async def start(
        self,
        cfg: ChannelConfig,
        on_inbound,
        *,
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> QQConnection:
        connection = QQConnection(
            client_factory=self.client_factory, retry_delays=self.retry_delays
        )
        await connection.start(
            cfg, on_inbound, wait_ready=wait_ready, ready_timeout=ready_timeout
        )
        return connection

    def outbound_parts(self, text: str) -> list[str]:
        cleaned = strip_markdown_for_qq(text).strip() or text
        return split_qq_parts(cleaned)

    def fallback(self, kind: str) -> str:
        return QQ_TIMEOUT_REPLY if kind == "timeout" else QQ_FAILURE_REPLY
