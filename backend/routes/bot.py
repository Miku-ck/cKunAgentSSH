import json
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db
from bot.chat_service import BotChatService

router = APIRouter(tags=["bot"])
logger = logging.getLogger(__name__)
chat_service = BotChatService()


class PersonaRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    avatar: str = Field("", max_length=500)
    personality: str = Field("", max_length=5000)
    background: str = Field("", max_length=10000)
    greeting: str = Field("", max_length=2000)
    model_id: Optional[str] = None


class PersonaUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    avatar: Optional[str] = Field(None, max_length=500)
    personality: Optional[str] = Field(None, max_length=5000)
    background: Optional[str] = Field(None, max_length=10000)
    greeting: Optional[str] = Field(None, max_length=2000)
    model_id: Optional[str] = None


class CreateBotConversationRequest(BaseModel):
    persona_id: str
    channel: Literal["local"] = "local"


class BotChatRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1, max_length=20000)


@router.get("/bot/personas")
def list_personas():
    return db.list_bot_personas()


@router.post("/bot/personas")
def create_persona(req: PersonaRequest):
    return db.create_bot_persona(**req.model_dump())


@router.post("/bot/personas/{persona_id}/copy")
def copy_persona(persona_id: str):
    persona = db.copy_bot_persona(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="人格不存在")
    return persona


@router.patch("/bot/personas/{persona_id}")
def update_persona(persona_id: str, req: PersonaUpdateRequest):
    data = req.model_dump(exclude_unset=True)
    data = {key: value for key, value in data.items() if value is not None or key == "model_id"}
    persona = db.update_bot_persona(persona_id, **data)
    if not persona:
        raise HTTPException(status_code=404, detail="人格不存在")
    return persona


@router.delete("/bot/personas/{persona_id}")
def delete_persona(persona_id: str):
    if not db.delete_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    return {"deleted": True}


@router.get("/bot/conversations")
def list_conversations(
    channel: Optional[Literal["local", "qq"]] = None,
    persona_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    return db.list_bot_conversations(channel=channel, persona_id=persona_id, limit=limit)


@router.post("/bot/conversations")
def create_conversation(req: CreateBotConversationRequest):
    try:
        return db.create_bot_conversation(req.persona_id, req.channel)
    except ValueError:
        raise HTTPException(status_code=404, detail="人格不存在")


@router.get("/bot/conversations/{conversation_id}/messages")
def get_messages(conversation_id: str, limit: int = Query(100, ge=1, le=500)):
    if not db.get_bot_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return db.get_bot_messages(conversation_id, limit)


@router.delete("/bot/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    if not db.delete_bot_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}


@router.post("/bot/chat")
async def chat(req: BotChatRequest):
    if not db.get_bot_conversation(req.conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    async def events():
        try:
            async for content in chat_service.stream_reply(req.conversation_id, req.content):
                yield f"data: {json.dumps({'type': 'content', 'content': content}, ensure_ascii=False)}\n\n"
            message = db.get_bot_messages(req.conversation_id, 1)[-1]
            yield f"data: {json.dumps({'type': 'done', 'message': message}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            import netconfig

            logger.warning("cKunBot 回复失败: %s", exc, exc_info=True)
            detail = netconfig.redact_urls_in_text(str(exc)) or "回复生成失败"
            yield f"data: {json.dumps({'type': 'error', 'detail': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
