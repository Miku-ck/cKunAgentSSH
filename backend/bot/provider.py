"""渠道 provider 契约（spec 2026-10-28-bot-multi-channel）。

一个平台一个实现，只有 5 个成员。刻意做窄：接口必须同时容纳 QQ
（回复挂在 botpy 的原始消息对象上）和钉钉（回复只能 POST 到消息里带的临时
sessionWebhook），所以发送能力是**闭包**而不是消息对象 —— 管线永远拿不到
平台原始对象，换平台就不需要改管线。
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

__all__ = [
    "ChannelAlreadyConnectedError",
    "ChannelConnectError",
    "ChannelConfig",
    "ChannelProvider",
    "ConnectionHandle",
    "InboundMessage",
    "UnsupportedProviderError",
]


class ChannelConnectError(Exception):
    """连接/握手失败。fatal=True 表示重试多少次都不会变好（凭据或权限问题）。"""

    def __init__(self, detail: str, *, fatal: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.fatal = fatal


class ChannelAlreadyConnectedError(RuntimeError):
    """同一个机器人重复启动。"""


class UnsupportedProviderError(RuntimeError):
    """渠道类型没有对应的 provider 实现。"""


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    app_id: str
    app_secret: str
    persona_id: str


@dataclass(frozen=True)
class InboundMessage:
    channel_id: str
    conversation_key: str
    event_id: str
    text: str
    send: Callable[[str], Awaitable[None]]


class ConnectionHandle(Protocol):
    @property
    def status(self) -> dict: ...

    async def stop(self) -> None: ...


class ChannelProvider(Protocol):
    name: str
    reply_window_s: float

    async def start(
        self,
        cfg: ChannelConfig,
        on_inbound: Callable[[InboundMessage], Awaitable[None]],
        *,
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> ConnectionHandle: ...

    def outbound_parts(self, text: str) -> list[str]: ...

    def fallback(self, kind: str) -> str: ...
