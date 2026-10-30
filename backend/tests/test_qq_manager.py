import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace

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


# ── 主动消息：没有入站消息时开口 ──


class FakeHttp:
    def __init__(self, result=None, error=None, delay=0.0):
        self.calls = []
        self.result = {"id": "msg-1"} if result is None else result
        self.error = error
        self.delay = delay

    async def request(self, route, json=None):
        self.calls.append((route, json))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class BotThread:
    """模拟 ckunbot-qq-* 线程：botpy 的 aiohttp 会话绑在这个 loop 上。"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self._shutdown)
        self.thread.join(5)
        self.loop.close()

    def _shutdown(self):
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
        self.loop.stop()


def online_connection(http):
    from bot.qq_manager import QQConnection

    bot = BotThread()
    connection = QQConnection()
    connection._client = SimpleNamespace(api=SimpleNamespace(_http=http))
    connection._loop = bot.loop
    connection._set_status("online")
    return connection, bot


@pytest.mark.asyncio
async def test_send_proactive_crosses_into_the_botpy_thread():
    http = FakeHttp()
    connection, bot = online_connection(http)
    try:
        assert await connection.send_proactive("c2c:user-1", "  在忙吗  ") == ("ok", "")
    finally:
        bot.stop()

    route, payload = http.calls[0]
    assert route.path == "/v2/users/{openid}/messages"
    assert route.parameters == {"openid": "user-1"}
    assert payload == {"msg_type": 0, "content": "在忙吗"}


@pytest.mark.asyncio
async def test_send_proactive_offline_connection_never_touches_the_network():
    from bot.qq_manager import QQConnection

    connection = QQConnection()
    assert await connection.send_proactive("c2c:user-1", "在吗") == (
        "offline", "QQ 连接不在线",
    )


@pytest.mark.asyncio
async def test_send_proactive_rejects_dirty_conversation_keys():
    connection, bot = online_connection(FakeHttp())
    try:
        kind, detail = await connection.send_proactive("user-1", "在吗")
    finally:
        bot.stop()

    assert kind == "invalid" and "user-1" in detail


@pytest.mark.asyncio
async def test_send_proactive_rejects_empty_content():
    connection, bot = online_connection(FakeHttp())
    try:
        assert await connection.send_proactive("c2c:user-1", "   ") == (
            "invalid", "主动消息内容为空",
        )
    finally:
        bot.stop()


@pytest.mark.asyncio
async def test_send_proactive_truncates_instead_of_sending_two_messages():
    """每段都算一条主动消息：超长只能截断，不能像被动回复那样分段重发。"""
    from bot.qq_channel import QQ_TEXT_LIMIT

    http = FakeHttp()
    connection, bot = online_connection(http)
    try:
        assert await connection.send_proactive("c2c:user-1", "啊" * (QQ_TEXT_LIMIT + 100)) == (
            "ok", "",
        )
    finally:
        bot.stop()

    content = http.calls[0][1]["content"]
    assert len(content) == QQ_TEXT_LIMIT
    assert content.endswith("…")


@pytest.mark.asyncio
async def test_send_proactive_classifies_platform_errors_across_threads():
    """异常从 botpy 线程回传到主 loop 后仍然要能定性，否则降级决策就失灵了。"""
    http = FakeHttp(error=RuntimeError("40034100 主动消息超频控"))
    connection, bot = online_connection(http)
    try:
        kind, detail = await connection.send_proactive("c2c:user-1", "在吗")
    finally:
        bot.stop()

    assert kind == "quota"
    assert "40034100" in detail


@pytest.mark.asyncio
async def test_send_proactive_timeout_is_reported_as_network(monkeypatch):
    from bot import qq_manager

    monkeypatch.setattr(qq_manager, "PROACTIVE_SEND_TIMEOUT_S", 0.05)

    def never_finishes(coro, _loop):
        coro.close()
        return concurrent.futures.Future()

    monkeypatch.setattr(qq_manager.asyncio, "run_coroutine_threadsafe", never_finishes)
    connection, bot = online_connection(FakeHttp())
    try:
        assert await connection.send_proactive("c2c:user-1", "在吗") == (
            "network", "主动消息发送超时",
        )
    finally:
        bot.stop()


# ── GROUP_MESSAGE_CREATE：botpy 1.2.1 没有这个解析器，运行期补进分发表 ──


def test_group_full_message_parser_is_injected_into_the_botpy_table():
    """群消息全量模式：往 state.parsers 里补一项就能收到事件，不必改依赖。"""
    from bot.qq_channel import AmbientGroupMessage
    from bot.qq_manager import _install_group_message_parser

    dispatched = []

    class Client:
        api = SimpleNamespace()
        _connection = SimpleNamespace(
            state=SimpleNamespace(parsers={"group_at_message_create": lambda p: None})
        )

        def ws_dispatch(self, event, *args):
            dispatched.append((event, args))

    client = Client()
    assert _install_group_message_parser(client) is True

    parsers = client._connection.state.parsers
    parsers["group_message_create"]({
        "id": "event-1",
        "d": {"content": "今晚吃啥", "group_openid": "g1",
              "author": {"member_openid": "m1", "username": "小明"}},
    })

    event, args = dispatched[0]
    assert event == "group_message_create"
    message = args[0]
    assert isinstance(message, AmbientGroupMessage)
    assert (message.content, message.group_openid, message.author.username) == (
        "今晚吃啥", "g1", "小明",
    )


def test_group_full_message_parser_reports_missing_table():
    """连接还没就绪时不能默默吞掉：日志要能查为什么群消息没进来。"""
    from bot.qq_manager import _install_group_message_parser

    class Client:
        _connection = None

    assert _install_group_message_parser(Client()) is False
