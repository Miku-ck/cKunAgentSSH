"""机器人渠道的增删改查与上下线（spec 2026-10-28-bot-multi-channel）。

取代旧的 routes/bot_qq.py：那边一个全局单例对应一套 `bot.qq.*` 键，
这里一行 bot_channels 对应一个机器人。
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from bot.provider import (
    ChannelAlreadyConnectedError,
    ChannelConnectError,
)
from bot.wechat_channel import WEBHOOK_BASE_SETTING

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bot-channels"])

# 每个机器人一条线程加一个事件循环和一条长连接；这不是业务上限，
# 只是防手滑堆到几十个把后端线程数打飞。
MAX_BOT_CHANNELS = 8


class ChannelCreateRequest(BaseModel):
    # 不写死 Literal：可选类型由运行时已注册的 provider 决定，
    # 加一个平台是「写实现 ＋ 进注册表」，不该再改这里。
    provider: str = "qq"
    display_name: str = Field(..., min_length=1, max_length=40)
    persona_id: str
    app_id: str = Field(..., min_length=1, max_length=100)
    app_secret: Optional[str] = Field(None, max_length=500)
    # 部分平台的第三个凭据（微信 EncodingAESKey）；QQ 不用。
    extra_secret: Optional[str] = Field(None, max_length=500)


class ChannelUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=40)
    persona_id: Optional[str] = None
    app_id: Optional[str] = Field(None, min_length=1, max_length=100)
    app_secret: Optional[str] = Field(None, max_length=500)
    extra_secret: Optional[str] = Field(None, max_length=500)


class WebhookRequest(BaseModel):
    base_url: str = Field("", max_length=300)


def _runtime():
    # 调用时才取：测试替身是打在 bot.channel_runtime 模块属性上的。
    from bot.channel_runtime import get_channel_runtime

    return get_channel_runtime()


def _require(channel_id: str) -> dict:
    channel = db.get_bot_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="机器人不存在")
    return channel


def _with_status(channel: dict) -> dict:
    return {**channel, "status": _runtime().status(channel["id"])}


def _missing_credentials(runtime, provider: str, values: dict) -> list[str]:
    """按 provider 声明的凭据字段算缺哪几个，返回界面标签供报错。"""
    labels = runtime.credential_labels(provider)
    return [
        label
        for field, label in labels.items()
        if not str(values.get(field) or "").strip()
    ]


@router.get("/bot/webhook")
def get_webhook():
    return {"base_url": db.get_app_setting(WEBHOOK_BASE_SETTING, "") or ""}


@router.put("/bot/webhook")
def set_webhook(req: WebhookRequest):
    value = req.base_url.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="公网地址必须以 http:// 或 https:// 开头")
    db.set_app_setting(WEBHOOK_BASE_SETTING, value)
    return {"base_url": value}


@router.get("/bot/channels")
def list_channels():
    """一次返回所有机器人和它们各自的连接状态，供连接页整页轮询。"""
    return [_with_status(channel) for channel in db.list_bot_channels()]


@router.post("/bot/channels")
def create_channel(req: ChannelCreateRequest):
    if not db.get_bot_persona(req.persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    runtime = _runtime()
    if not runtime.supports(req.provider):
        raise HTTPException(status_code=400, detail=f"暂不支持的渠道类型：{req.provider}")
    missing = _missing_credentials(
        runtime, req.provider, {"app_secret": req.app_secret, "extra_secret": req.extra_secret}
    )
    if missing:
        raise HTTPException(
            status_code=400, detail=f"首次配置需要填写 {'、'.join(missing)}"
        )
    if db.count_bot_channels() >= MAX_BOT_CHANNELS:
        raise HTTPException(
            status_code=400, detail=f"最多同时配置 {MAX_BOT_CHANNELS} 个机器人"
        )
    channel = db.create_bot_channel(
        provider=req.provider,
        display_name=req.display_name.strip(),
        persona_id=req.persona_id,
        app_id=req.app_id.strip(),
        app_secret=db.protect_secret(req.app_secret),
        extra_secret=db.protect_secret(req.extra_secret) if req.extra_secret else "",
    )
    logger.info("新增 %s 机器人渠道：%s", channel["provider"], channel["display_name"])
    return _with_status(channel)


@router.put("/bot/channels/{channel_id}")
def update_channel(channel_id: str, req: ChannelUpdateRequest):
    channel = _require(channel_id)
    if req.persona_id is not None and not db.get_bot_persona(req.persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")

    switches_persona = req.persona_id is not None and req.persona_id != channel["persona_id"]
    if switches_persona and _runtime().status(channel_id)["state"] != "offline":
        # 在线换人格会让正在生成的回复用错人格；先断开再换。
        raise HTTPException(status_code=409, detail="请先断开这个机器人，再切换人格")

    fields = {}
    if req.display_name is not None:
        fields["display_name"] = req.display_name.strip()
    if req.persona_id is not None:
        fields["persona_id"] = req.persona_id
    if req.app_id is not None:
        fields["app_id"] = req.app_id.strip()
    if fields:
        channel = db.update_bot_channel(channel_id, **fields)

    # 空凭据 = 保留原值（前端永远不回显明文）。
    if req.app_secret:
        db.set_bot_channel_secret(channel_id, db.protect_secret(req.app_secret))
        channel = db.get_bot_channel(channel_id)
    if req.extra_secret:
        db.set_bot_channel_extra_secret(channel_id, db.protect_secret(req.extra_secret))
        channel = db.get_bot_channel(channel_id)
    return _with_status(channel)


@router.delete("/bot/channels/{channel_id}")
async def delete_channel(channel_id: str):
    channel = _require(channel_id)
    await _runtime().disconnect(channel_id)
    db.delete_bot_channel(channel_id)
    logger.info(
        "删除机器人渠道 %s（连带 %d 条会话）", channel["display_name"], channel["conversation_count"]
    )
    return {"deleted": True, "conversation_count": channel["conversation_count"]}


@router.post("/bot/channels/{channel_id}/connect")
async def connect(channel_id: str):
    channel = _require(channel_id)
    runtime = _runtime()
    missing = _missing_credentials(
        runtime,
        channel["provider"],
        {
            "app_secret": db.get_bot_channel_secret(channel_id),
            "extra_secret": db.get_bot_channel_extra_secret(channel_id),
        },
    )
    if not channel["app_id"] or missing:
        raise HTTPException(status_code=400, detail="请先完整填写这个机器人的配置")

    try:
        status = await runtime.start(channel)
    except ChannelAlreadyConnectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChannelConnectError as exc:
        # 凭据/权限错了就别留着 enabled，否则每次开机都拿旧凭据重试。
        if exc.fatal:
            db.set_bot_channel_enabled(channel_id, False)
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    db.set_bot_channel_enabled(channel_id, True)
    return status


@router.post("/bot/channels/{channel_id}/disconnect")
async def disconnect(channel_id: str):
    _require(channel_id)
    status = await _runtime().disconnect(channel_id)
    db.set_bot_channel_enabled(channel_id, False)
    return status
