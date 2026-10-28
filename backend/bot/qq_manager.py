import asyncio
import threading

import netconfig

from bot.chat_service import BotChatService
from bot.qq_channel import QQEventProcessor

# botpy 在登录阶段映射 401→AuthenticationFailedError、403→ForbiddenError；
# 这两类重试再多次也不会变好，直接落到 error 状态等用户改配置。
FATAL_EXCEPTIONS = {"AuthenticationFailedError", "ForbiddenError"}
FRIENDLY_DETAILS = {
    "AuthenticationFailedError": "AppID 或 AppSecret 不正确，请到 QQ 开放平台核对后重试",
    "ForbiddenError": "QQ 机器人无权连接，请确认已在 QQ 开放平台启用并开通消息权限",
}
DEFAULT_RETRY_DELAYS = (5, 10, 30, 60)


class QQConnectError(Exception):
    def __init__(self, detail: str, *, fatal: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.fatal = fatal


class QQAlreadyRunningError(RuntimeError):
    pass


class QQChannelManager:
    def __init__(self, client_factory=None, chat_service=None, retry_delays=DEFAULT_RETRY_DELAYS):
        self.client_factory = client_factory or self._create_botpy_client
        self.chat_service = chat_service or BotChatService()
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
        import botpy

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
    def _connect_error(exc: BaseException | None) -> QQConnectError:
        if exc is None:
            return QQConnectError("QQ 连接在握手完成前断开，请稍后重试")
        return QQConnectError(
            QQChannelManager._detail_for(exc),
            fatal=QQChannelManager._is_fatal(exc),
        )

    async def start(
        self,
        appid: str,
        secret: str,
        persona_id: str,
        context_id: str,
        *,
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> None:
        """启动通道；默认等 botpy 登录成功后返回，失败时抛 QQConnectError。

        restore 走 wait_ready=False，避免开机被握手超时拖住。
        """
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                raise QQAlreadyRunningError("QQ 机器人已在运行")
            self._stopping = threading.Event()
            self._connect_ready = threading.Event()
            self._connect_result = (False, "", False)
            self._was_online = False
            self._retry_index = 0
            self._set_status("connecting")
            self._thread = threading.Thread(
                target=self._run,
                args=(appid, secret, persona_id, context_id),
                name="ckunbot-qq",
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
            raise QQConnectError(detail)
        ok, detail, fatal = self._connect_result
        if not ok:
            await self._stop_worker()
            raise QQConnectError(detail or "QQ 连接失败", fatal=fatal)

    def _run(self, appid: str, secret: str, persona_id: str, context_id: str) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._main_task = loop.create_task(
                self._main(appid, secret, persona_id, context_id)
            )
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

    async def _main(self, appid: str, secret: str, persona_id: str, context_id: str) -> None:
        try:
            await self._supervise(appid, secret, persona_id, context_id)
        except asyncio.CancelledError:
            raise
        except QQConnectError as exc:
            self._set_status("error", exc.detail)
            self._finish_connect(False, exc.detail, exc.fatal)
        except Exception as exc:
            detail = self._detail_for(exc)
            self._set_status("error", detail)
            self._finish_connect(False, detail, self._is_fatal(exc))

    async def _supervise(self, appid: str, secret: str, persona_id: str, context_id: str) -> None:
        while True:
            if self._stopping.is_set():
                return
            client = None
            error = None
            try:
                processor = QQEventProcessor(self.chat_service, persona_id, context_id)
                client = self.client_factory(processor, self._on_client_ready)
                connection = await client.start(appid, secret, ret_coro=True)
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


qq_manager = QQChannelManager()


def get_qq_manager() -> QQChannelManager:
    return qq_manager
