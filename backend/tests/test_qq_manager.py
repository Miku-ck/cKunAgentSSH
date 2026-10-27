import asyncio

import pytest


class FakeClient:
    def __init__(self, processor, on_ready):
        self.processor = processor
        self.on_ready = on_ready
        self.closed = False

    async def start(self, appid, secret, ret_coro=False):
        assert (appid, secret, ret_coro) == ("app-id", "app-secret", True)
        await self.on_ready()

        async def listen():
            await asyncio.Future()

        return listen()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_manager_starts_and_fully_stops_client_thread():
    from bot.qq_manager import QQChannelManager

    clients = []

    def factory(processor, on_ready):
        client = FakeClient(processor, on_ready)
        clients.append(client)
        return client

    manager = QQChannelManager(client_factory=factory)
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")
    for _ in range(100):
        if manager.status["state"] == "online":
            break
        await asyncio.sleep(0.01)

    assert manager.status["state"] == "online"
    await manager.stop()

    assert manager.status["state"] == "offline"
    assert clients[0].closed is True
    assert manager._thread is None or not manager._thread.is_alive()


@pytest.mark.asyncio
async def test_manager_reports_startup_failure():
    from bot.qq_manager import QQChannelManager

    class FailingClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            raise RuntimeError("bad credentials")

    manager = QQChannelManager(client_factory=lambda *_args: FailingClient(None, None))
    await manager.start("bad", "bad", "persona-1", "ctx-1")
    for _ in range(100):
        if manager.status["state"] == "error":
            break
        await asyncio.sleep(0.01)

    assert manager.status["state"] == "error"
    assert manager.status["detail"] == "bad credentials"
    await manager.stop()
