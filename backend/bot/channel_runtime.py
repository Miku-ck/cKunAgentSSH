"""N 条机器人连接的注册表（spec 2026-10-28-bot-multi-channel）。

每个渠道一行 `bot_channels` 对应一个 provider 连接句柄；入站管线与
chat_service 全进程共用一份。这里不再有任何「按全局单例判断是否已在运行」的
逻辑 —— 一个机器人坏了不影响另一个，状态各报各的。
"""

import logging
import threading

import database as db
from bot.chat_service import BotChatService
from bot.inbound_pipeline import InboundPipeline
from bot.provider import (
    ChannelAlreadyConnectedError,
    ChannelConfig,
    UnsupportedProviderError,
)
from bot.qq_manager import QQProvider

logger = logging.getLogger(__name__)

OFFLINE_STATUS = {"state": "offline", "detail": ""}
CONNECTING_STATUS = {"state": "connecting", "detail": ""}

# 占位：start 正在握手的渠道。它同时起到「同一渠道不许并发启动」的作用，
# 又不会让一个慢握手把别人的 start 一起堵住。
_STARTING = object()


def default_providers() -> dict:
    # 接入钉钉就是在这个元组里多一项，其余代码不动。
    providers = (QQProvider(),)
    return {provider.name: provider for provider in providers}


class ChannelRuntime:
    def __init__(self, providers: dict | None = None, chat_service=None):
        self._providers = providers if providers is not None else default_providers()
        self._pipeline = InboundPipeline(chat_service or BotChatService())
        self._handles: dict[str, object] = {}
        self._lock = threading.Lock()

    def supports(self, provider_name: str) -> bool:
        return provider_name in self._providers

    @staticmethod
    def _status_of(handle) -> dict:
        if handle is _STARTING:
            return dict(CONNECTING_STATUS)
        return handle.status

    def status(self, channel_id: str) -> dict:
        with self._lock:
            handle = self._handles.get(channel_id)
        if handle is None:
            return dict(OFFLINE_STATUS)
        return self._status_of(handle)

    def statuses(self) -> dict[str, dict]:
        with self._lock:
            entries = list(self._handles.items())
        return {channel_id: self._status_of(handle) for channel_id, handle in entries}

    async def start(self, channel: dict, *, wait_ready: bool = True) -> dict:
        channel_id = channel["id"]
        provider = self._providers.get(channel["provider"])
        if provider is None:
            raise UnsupportedProviderError(f"暂不支持的渠道类型：{channel['provider']}")

        with self._lock:
            if channel_id in self._handles:
                raise ChannelAlreadyConnectedError(f"{channel['display_name']} 已在运行")
            self._handles[channel_id] = _STARTING

        cfg = ChannelConfig(
            channel_id=channel_id,
            app_id=channel["app_id"],
            app_secret=db.get_bot_channel_secret(channel_id),
            persona_id=channel["persona_id"],
        )
        try:
            handle = await provider.start(
                cfg, self._on_inbound(channel), wait_ready=wait_ready
            )
        except BaseException:
            with self._lock:
                self._handles.pop(channel_id, None)
            raise
        with self._lock:
            self._handles[channel_id] = handle
        return handle.status

    async def stop(self, channel_id: str) -> dict:
        with self._lock:
            handle = self._handles.pop(channel_id, None)
        if handle is None or handle is _STARTING:
            return dict(OFFLINE_STATUS)
        await handle.stop()
        return handle.status

    async def stop_all(self) -> None:
        for channel_id in list(self._handles):
            try:
                await self.stop(channel_id)
            except Exception as exc:
                logger.warning("停止渠道 %s 失败: %s", channel_id, exc)

    async def restore(self) -> None:
        """开机自动接回上次开着的机器人；单个失败不影响其余，也不影响后端启动。"""
        for channel in db.list_enabled_bot_channels():
            try:
                # 不阻塞开机：握手成败交给后台线程，状态由 /bot/channels 轮询。
                await self.start(channel, wait_ready=False)
            except Exception as exc:
                logger.warning(
                    "恢复渠道 %s 失败: %s", channel.get("display_name"), exc
                )

    def _on_inbound(self, channel: dict):
        provider = self._providers[channel["provider"]]
        persona_id = channel["persona_id"]

        async def handle(inbound) -> None:
            await self._pipeline.handle(inbound, provider, persona_id)

        return handle


channel_runtime = ChannelRuntime()


def get_channel_runtime() -> ChannelRuntime:
    return channel_runtime
