import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db

router = APIRouter(tags=["bot-qq"])


class QQConfigRequest(BaseModel):
    app_id: str = Field(..., min_length=1, max_length=100)
    app_secret: Optional[str] = Field(None, max_length=500)
    persona_id: str


def _config() -> dict:
    persona_id = db.get_app_setting("bot.qq.persona_id", "") or ""
    context_id = db.get_app_setting("bot.qq.context_id", "") or ""
    return {
        "app_id": db.get_app_setting("bot.qq.app_id", "") or "",
        "has_secret": bool(db.get_app_setting("bot.qq.app_secret", "")),
        "persona_id": persona_id,
        "context_id": context_id,
    }


@router.get("/bot/qq/config")
def get_config():
    return _config()


@router.put("/bot/qq/config")
def save_config(req: QQConfigRequest):
    if not db.get_bot_persona(req.persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    from bot.qq_manager import get_qq_manager

    previous_persona = db.get_app_setting("bot.qq.persona_id", "")
    if (get_qq_manager().status["state"] != "offline"
            and previous_persona != req.persona_id):
        raise HTTPException(status_code=409, detail="请先断开 QQ，再切换人格")
    db.set_app_setting("bot.qq.app_id", req.app_id)
    if req.app_secret:
        db.set_app_setting("bot.qq.app_secret", db.protect_secret(req.app_secret))
    if not db.get_app_setting("bot.qq.app_secret", ""):
        raise HTTPException(status_code=400, detail="首次配置需要填写 AppSecret")
    db.set_app_setting("bot.qq.persona_id", req.persona_id)
    if previous_persona != req.persona_id or not db.get_app_setting("bot.qq.context_id", ""):
        db.set_app_setting("bot.qq.context_id", uuid.uuid4().hex)
    return _config()


@router.get("/bot/qq/status")
def get_status():
    from bot.qq_manager import get_qq_manager

    return get_qq_manager().status


@router.post("/bot/qq/connect")
async def connect():
    config = _config()
    secret = db.get_app_setting("bot.qq.app_secret", "") or ""
    if not config["app_id"] or not secret or not config["persona_id"]:
        raise HTTPException(status_code=400, detail="请先完整填写 QQ 配置")
    from bot.qq_manager import get_qq_manager

    await get_qq_manager().start(
        config["app_id"], secret, config["persona_id"], config["context_id"]
    )
    db.set_app_setting("bot.qq.enabled", "1")
    return get_qq_manager().status


@router.post("/bot/qq/disconnect")
async def disconnect():
    from bot.qq_manager import get_qq_manager

    await get_qq_manager().stop()
    db.set_app_setting("bot.qq.enabled", "0")
    return get_qq_manager().status


async def restore_qq_channel():
    if db.get_app_setting("bot.qq.enabled", "0") != "1":
        return
    config = _config()
    secret = db.get_app_setting("bot.qq.app_secret", "") or ""
    if config["app_id"] and secret and config["persona_id"]:
        from bot.qq_manager import get_qq_manager

        await get_qq_manager().start(
            config["app_id"], secret, config["persona_id"], config["context_id"]
        )
