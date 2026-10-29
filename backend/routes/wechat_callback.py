"""微信对话开放平台的公网回调入口（spec 2026-10-29-wechat-channel）。

刻意不挂 `/api` 前缀：平台回调不会带 X-API-Token，挂在 /api 下会被鉴权中间件
挡成 401。这里的鉴权只有一层 —— 能用该渠道 EncodingAESKey 解开、且解出的
appid 与渠道一致；伪造请求在解密阶段就失败。
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from bot.wechat_channel import ACK, WeChatPayloadError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wechat"])

# 平台回调只有一条加密文本；64KB 已远大于任何正常报文。
MAX_BODY_BYTES = 64 * 1024


def _runtime():
    from bot.channel_runtime import get_channel_runtime

    return get_channel_runtime()


@router.post("/bot/wechat/{channel_id}")
async def wechat_callback(channel_id: str, request: Request):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="回调报文过大")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="回调报文过大")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="回调报文不是合法 JSON") from exc

    handle = _runtime().handle(channel_id)
    accept = getattr(handle, "accept", None)
    if accept is None:
        # 渠道离线是正常状态（用户刚断开/还没连）；ack 掉，别让平台重投堆积。
        logger.warning("微信回调落到离线渠道 %s，已丢弃", channel_id[:8])
        return dict(ACK)

    try:
        return await accept(body)
    except WeChatPayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
