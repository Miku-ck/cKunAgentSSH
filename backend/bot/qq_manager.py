import asyncio
import threading

from bot.chat_service import BotChatService
from bot.qq_channel import QQEventProcessor


class QQChannelManager:
    def __init__(self, client_factory=None, chat_service=None):
        self.client_factory = client_factory or self._create_botpy_client
        self.chat_service = chat_service or BotChatService()
        self._thread: threading.Thread | None = None
        self._loop = None
        self._main_task = None
        self._status = {"state": "offline", "detail": ""}
        self._status_lock = threading.Lock()

    @property
    def status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, state: str, detail: str = "") -> None:
        with self._status_lock:
            self._status = {"state": state, "detail": detail}

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

    async def start(self, appid: str, secret: str, persona_id: str, context_id: str) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("QQ 机器人已在运行")
        self._set_status("connecting")
        self._thread = threading.Thread(
            target=self._run,
            args=(appid, secret, persona_id, context_id),
            name="ckunbot-qq",
            daemon=True,
        )
        self._thread.start()

    def _run(self, appid: str, secret: str, persona_id: str, context_id: str) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        client = None

        async def ready():
            self._set_status("online")

        async def main():
            nonlocal client
            processor = QQEventProcessor(self.chat_service, persona_id, context_id)
            client = self.client_factory(processor, ready)
            connection = await client.start(appid, secret, ret_coro=True)
            if connection:
                await connection

        try:
            self._main_task = loop.create_task(main())
            loop.run_until_complete(self._main_task)
            if self.status["state"] != "error":
                self._set_status("offline")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            import netconfig

            self._set_status("error", netconfig.redact_urls_in_text(str(exc)))
        finally:
            if client is not None:
                try:
                    loop.run_until_complete(client.close())
                except Exception:
                    pass
            self._loop = None
            self._main_task = None
            if self.status["state"] == "connecting":
                self._set_status("offline")
            loop.close()

    async def stop(self) -> None:
        thread = self._thread
        if not thread:
            self._set_status("offline")
            return
        loop = self._loop
        task = self._main_task
        if loop and task:
            loop.call_soon_threadsafe(task.cancel)
        await asyncio.to_thread(thread.join, 5)
        if thread.is_alive():
            self._set_status("error", "QQ 连接未能在 5 秒内停止")
            return
        self._thread = None
        self._set_status("offline")


qq_manager = QQChannelManager()


def get_qq_manager() -> QQChannelManager:
    return qq_manager
