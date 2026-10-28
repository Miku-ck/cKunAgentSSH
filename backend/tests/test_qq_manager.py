import asyncio

import pytest


class AuthenticationFailedError(RuntimeError):
    """与 botpy.errors.AuthenticationFailedError 同名，管理器按类名识别 401。"""


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


def streaming_factory(clients):
    def factory(processor, on_ready):
        client = FakeClient(processor, on_ready)
        clients.append(client)
        return client

    return factory


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_manager_starts_and_fully_stops_client_thread():
    from bot.qq_manager import QQChannelManager

    clients = []
    manager = QQChannelManager(client_factory=streaming_factory(clients))
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")

    assert manager.status["state"] == "online"
    await manager.stop()

    assert manager.status["state"] == "offline"
    assert clients[0].closed is True
    assert manager._thread is None or not manager._thread.is_alive()


@pytest.mark.asyncio
async def test_start_raises_with_detail_when_botpy_login_fails():
    from bot.qq_manager import QQChannelManager, QQConnectError

    class FailingClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            raise RuntimeError("bad credentials")

    manager = QQChannelManager(client_factory=lambda *_args: FailingClient(None, None))
    with pytest.raises(QQConnectError) as info:
        await manager.start("bad", "bad", "persona-1", "ctx-1", ready_timeout=2)

    assert info.value.detail == "bad credentials"
    assert info.value.fatal is False
    assert manager.status["state"] == "error"
    assert manager._thread is None


@pytest.mark.asyncio
async def test_auth_failure_maps_to_friendly_fatal_message():
    from bot.qq_manager import QQChannelManager, QQConnectError

    class RejectingClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            raise AuthenticationFailedError("401 Unauthorized")

    manager = QQChannelManager(client_factory=lambda *_args: RejectingClient(None, None))
    with pytest.raises(QQConnectError) as info:
        await manager.start("bad", "bad", "persona-1", "ctx-1", ready_timeout=2)

    assert info.value.fatal is True
    assert "AppID 或 AppSecret 不正确" in info.value.detail
    assert manager.status["state"] == "error"
    assert "AppID 或 AppSecret 不正确" in manager.status["detail"]


@pytest.mark.asyncio
async def test_start_times_out_and_cleans_up_when_botpy_never_gets_ready():
    from bot.qq_manager import QQChannelManager, QQConnectError

    class SilentClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            async def listen():
                await asyncio.Future()

            return listen()

    manager = QQChannelManager(client_factory=lambda *_args: SilentClient(None, None))
    with pytest.raises(QQConnectError) as info:
        await manager.start("app-id", "app-secret", "persona-1", "ctx-1", ready_timeout=0.05)

    assert "超时" in info.value.detail
    assert manager.status["state"] == "error"
    assert manager._thread is None


@pytest.mark.asyncio
async def test_manager_reconnects_after_connection_drops():
    from bot.qq_manager import QQChannelManager

    clients = []

    class DropFirstClient(FakeClient):
        def __init__(self, processor, on_ready):
            super().__init__(processor, on_ready)
            self.first = not clients

        async def start(self, appid, secret, ret_coro=False):
            assert (appid, secret, ret_coro) == ("app-id", "app-secret", True)
            await self.on_ready()
            if self.first:
                async def dropped():
                    await asyncio.sleep(0.01)

                return dropped()

            async def listen():
                await asyncio.Future()

            return listen()

    def factory(processor, on_ready):
        client = DropFirstClient(processor, on_ready)
        clients.append(client)
        return client

    manager = QQChannelManager(client_factory=factory, retry_delays=(0.15,))
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")

    seen = set()
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        seen.add(manager.status["state"])
        if len(clients) == 2 and manager.status["state"] == "online":
            break
        await asyncio.sleep(0.01)

    assert "reconnecting" in seen
    assert clients[0].closed is True
    assert len(clients) == 2
    assert manager.status["state"] == "online"

    await manager.stop()
    assert clients[1].closed is True


@pytest.mark.asyncio
async def test_stop_during_reconnect_backoff_does_not_restart():
    from bot.qq_manager import QQChannelManager

    clients = []

    class DropAlwaysClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            await self.on_ready()

            async def dropped():
                await asyncio.sleep(0.01)

            return dropped()

    def factory(processor, on_ready):
        client = DropAlwaysClient(processor, on_ready)
        clients.append(client)
        return client

    manager = QQChannelManager(client_factory=factory, retry_delays=(5.0,))
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")
    assert await wait_for(lambda: manager.status["state"] == "reconnecting")

    await manager.stop()

    assert manager.status["state"] == "offline"
    assert manager._thread is None
    await asyncio.sleep(0.1)
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_reconnect_stops_after_auth_failure():
    from bot.qq_manager import QQChannelManager

    clients = []

    class AuthFailsOnRetryClient(FakeClient):
        def __init__(self, processor, on_ready):
            super().__init__(processor, on_ready)
            self.first = not clients

        async def start(self, appid, secret, ret_coro=False):
            await self.on_ready()
            if self.first:
                async def dropped():
                    await asyncio.sleep(0.01)

                return dropped()
            raise AuthenticationFailedError("401 Unauthorized")

    def factory(processor, on_ready):
        client = AuthFailsOnRetryClient(processor, on_ready)
        clients.append(client)
        return client

    manager = QQChannelManager(client_factory=factory, retry_delays=(0.05,))
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")

    assert await wait_for(lambda: manager.status["state"] == "error")
    assert "AppID 或 AppSecret 不正确" in manager.status["detail"]
    await asyncio.sleep(0.15)
    assert len(clients) == 2

    await manager.stop()
    assert manager.status["state"] == "offline"


@pytest.mark.asyncio
async def test_second_start_is_rejected_while_running():
    from bot.qq_manager import QQChannelManager, QQAlreadyRunningError

    clients = []
    manager = QQChannelManager(client_factory=streaming_factory(clients))
    await manager.start("app-id", "app-secret", "persona-1", "ctx-1")

    with pytest.raises(QQAlreadyRunningError):
        await manager.start("app-id", "app-secret", "persona-1", "ctx-1")

    await manager.stop()
