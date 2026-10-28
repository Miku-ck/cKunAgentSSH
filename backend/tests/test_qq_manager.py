import asyncio

import pytest

from bot.provider import ChannelAlreadyConnectedError, ChannelConnectError, ChannelConfig


class AuthenticationFailedError(RuntimeError):
    """与 botpy.errors.AuthenticationFailedError 同名，连接按类名识别 401。"""


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


def cfg(channel_id="channel-1"):
    return ChannelConfig(
        channel_id=channel_id,
        app_id="app-id",
        app_secret="app-secret",
        persona_id="persona-1",
    )


async def noop_inbound(_message):
    return None


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_connection_starts_and_fully_stops_client_thread():
    from bot.qq_manager import QQConnection

    clients = []
    connection = QQConnection(client_factory=streaming_factory(clients))
    await connection.start(cfg(), noop_inbound)

    assert connection.status["state"] == "online"
    await connection.stop()

    assert connection.status["state"] == "offline"
    assert clients[0].closed is True
    assert connection._thread is None or not connection._thread.is_alive()


@pytest.mark.asyncio
async def test_start_raises_with_detail_when_botpy_login_fails():
    from bot.qq_manager import QQConnection

    class FailingClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            raise RuntimeError("bad credentials")

    connection = QQConnection(client_factory=lambda *_args: FailingClient(None, None))
    with pytest.raises(ChannelConnectError) as info:
        await connection.start(cfg(), noop_inbound, ready_timeout=2)

    assert info.value.detail == "bad credentials"
    assert info.value.fatal is False
    assert connection.status["state"] == "error"
    assert connection._thread is None


@pytest.mark.asyncio
async def test_auth_failure_maps_to_friendly_fatal_message():
    from bot.qq_manager import QQConnection

    class RejectingClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            raise AuthenticationFailedError("401 Unauthorized")

    connection = QQConnection(client_factory=lambda *_args: RejectingClient(None, None))
    with pytest.raises(ChannelConnectError) as info:
        await connection.start(cfg(), noop_inbound, ready_timeout=2)

    assert info.value.fatal is True
    assert "AppID 或 AppSecret 不正确" in info.value.detail
    assert connection.status["state"] == "error"
    assert "AppID 或 AppSecret 不正确" in connection.status["detail"]


@pytest.mark.asyncio
async def test_missing_botpy_reports_actionable_fatal_error(monkeypatch):
    """解释器缺 qq-botpy 时给可行动文案，且按 fatal 处理（别让它无限重试）。"""
    import sys

    from bot.qq_manager import MISSING_DEPENDENCY_DETAIL, QQConnection

    # sys.modules 置 None：`import botpy` 直接 ImportError，不碰真实安装
    monkeypatch.setitem(sys.modules, "botpy", None)

    connection = QQConnection()
    with pytest.raises(ChannelConnectError) as info:
        await connection.start(cfg(), noop_inbound, ready_timeout=2)

    assert info.value.fatal is True
    assert info.value.detail == MISSING_DEPENDENCY_DETAIL
    assert connection.status == {"state": "error", "detail": MISSING_DEPENDENCY_DETAIL}
    assert connection._thread is None


@pytest.mark.asyncio
async def test_start_times_out_and_cleans_up_when_botpy_never_gets_ready():
    from bot.qq_manager import QQConnection

    class SilentClient(FakeClient):
        async def start(self, appid, secret, ret_coro=False):
            async def listen():
                await asyncio.Future()

            return listen()

    connection = QQConnection(client_factory=lambda *_args: SilentClient(None, None))
    with pytest.raises(ChannelConnectError) as info:
        await connection.start(cfg(), noop_inbound, ready_timeout=0.05)

    assert "超时" in info.value.detail
    assert connection.status["state"] == "error"
    assert connection._thread is None


@pytest.mark.asyncio
async def test_connection_reconnects_after_connection_drops():
    from bot.qq_manager import QQConnection

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

    connection = QQConnection(client_factory=factory, retry_delays=(0.15,))
    await connection.start(cfg(), noop_inbound)

    seen = set()
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        seen.add(connection.status["state"])
        if len(clients) == 2 and connection.status["state"] == "online":
            break
        await asyncio.sleep(0.01)

    assert "reconnecting" in seen
    assert clients[0].closed is True
    assert len(clients) == 2
    assert connection.status["state"] == "online"

    await connection.stop()
    assert clients[1].closed is True


@pytest.mark.asyncio
async def test_stop_during_reconnect_backoff_does_not_restart():
    from bot.qq_manager import QQConnection

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

    connection = QQConnection(client_factory=factory, retry_delays=(5.0,))
    await connection.start(cfg(), noop_inbound)
    assert await wait_for(lambda: connection.status["state"] == "reconnecting")

    await connection.stop()

    assert connection.status["state"] == "offline"
    assert connection._thread is None
    await asyncio.sleep(0.1)
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_reconnect_stops_after_auth_failure():
    from bot.qq_manager import QQConnection

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

    connection = QQConnection(client_factory=factory, retry_delays=(0.05,))
    await connection.start(cfg(), noop_inbound)

    assert await wait_for(lambda: connection.status["state"] == "error")
    assert "AppID 或 AppSecret 不正确" in connection.status["detail"]
    await asyncio.sleep(0.15)
    assert len(clients) == 2

    await connection.stop()
    assert connection.status["state"] == "offline"


@pytest.mark.asyncio
async def test_second_start_is_rejected_on_the_same_connection():
    from bot.qq_manager import QQConnection

    clients = []
    connection = QQConnection(client_factory=streaming_factory(clients))
    await connection.start(cfg(), noop_inbound)

    with pytest.raises(ChannelAlreadyConnectedError):
        await connection.start(cfg(), noop_inbound)

    await connection.stop()


@pytest.mark.asyncio
async def test_two_connections_run_independently():
    """多机器人的实质：两个实例各自的线程、各自的状态、各自地停。"""
    from bot.qq_manager import QQConnection

    first_clients, second_clients = [], []
    first = QQConnection(client_factory=streaming_factory(first_clients))
    second = QQConnection(client_factory=streaming_factory(second_clients))

    await first.start(cfg("aaaa1111"), noop_inbound)
    await second.start(cfg("bbbb2222"), noop_inbound)

    assert first.status["state"] == "online"
    assert second.status["state"] == "online"
    assert len(first_clients) == 1 and len(second_clients) == 1

    await first.stop()

    # A 下线不影响 B，也不影响 A 自己的句柄判断
    assert first.status["state"] == "offline"
    assert second.status["state"] == "online"
    assert second_clients[0].closed is False
    await second.stop()
    assert second_clients[0].closed is True


@pytest.mark.asyncio
async def test_thread_name_carries_the_channel_id():
    from bot.qq_manager import QQConnection

    clients = []
    connection = QQConnection(client_factory=streaming_factory(clients))
    await connection.start(cfg("deadbeefcafe"), noop_inbound)

    assert connection._thread.name == "ckunbot-qq-deadbeef"
    await connection.stop()
