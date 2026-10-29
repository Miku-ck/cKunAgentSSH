import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

import database as db
from bot import memory as memory_mod
from bot import relation as relation_mod
from bot.character import persona_options
from bot.chat_service import BotChatService
from bot.voice import DNA_OPTIONS, normalize_dna
from sse_utils import sse_event

router = APIRouter(tags=["bot"])
logger = logging.getLogger(__name__)
chat_service = BotChatService()


# 头像只收上传图片压缩后的 data URL（256px webp 约 20k 字符，上限留足余量）；
# 表情/任意文本不再当头像 —— 与前端 isImageAvatar 同口径。
_AVATAR_DATA_URL = re.compile(r"^data:image/(?:png|jpe?g|webp|gif);base64,", re.IGNORECASE)


def _validate_avatar(value: str) -> str:
    if value and not _AVATAR_DATA_URL.match(value):
        raise ValueError("头像仅支持上传的图片")
    return value


class PersonaRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    avatar: str = Field("", max_length=400_000)
    personality: str = Field("", max_length=5000)
    background: str = Field("", max_length=10000)
    greeting: str = Field("", max_length=2000)
    model_id: Optional[str] = None
    reply_style: Literal["concise", "moderate", "long"] = "moderate"
    enable_thinking: bool = False
    # 活人感内核：表达 DNA 是对象，其余三项决定反应、拒绝方式与关系升温。
    expression_dna: dict = Field(default_factory=dict)
    reaction_style: Literal["reserved", "warm", "playful", "blunt"] = "warm"
    refusal_style: Literal["soft", "direct", "playful", "concise"] = "soft"
    address_user: str = Field("", max_length=20)
    relationship_speed: Literal["slow", "normal", "fast"] = "normal"
    # 整页编辑的四个自由文本分区：空串即「这一节不进提示词」，不做默认值兜底。
    likes_landmines: str = Field("", max_length=2000)
    memory_directive: str = Field("", max_length=2000)
    dialogue_samples: str = Field("", max_length=4000)
    advanced_play: str = Field("", max_length=2000)

    _check_avatar = field_validator("avatar")(_validate_avatar)

    @field_validator("expression_dna")
    @classmethod
    def _check_dna(cls, value: dict) -> dict:
        # 统一在这里归一：脏值（未知档位、超长口头禅）只会让说话方式退回默认，
        # 不该让保存人格失败——用户看不到自己的选项为什么存不进去。
        return normalize_dna(value)


class PersonaUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    avatar: Optional[str] = Field(None, max_length=400_000)
    personality: Optional[str] = Field(None, max_length=5000)
    background: Optional[str] = Field(None, max_length=10000)
    greeting: Optional[str] = Field(None, max_length=2000)
    model_id: Optional[str] = None
    reply_style: Optional[Literal["concise", "moderate", "long"]] = None
    enable_thinking: Optional[bool] = None
    expression_dna: Optional[dict] = None
    reaction_style: Optional[Literal["reserved", "warm", "playful", "blunt"]] = None
    refusal_style: Optional[Literal["soft", "direct", "playful", "concise"]] = None
    address_user: Optional[str] = Field(None, max_length=20)
    relationship_speed: Optional[Literal["slow", "normal", "fast"]] = None
    likes_landmines: Optional[str] = Field(None, max_length=2000)
    memory_directive: Optional[str] = Field(None, max_length=2000)
    dialogue_samples: Optional[str] = Field(None, max_length=4000)
    advanced_play: Optional[str] = Field(None, max_length=2000)
    # P3 主动性开关：她会主动找你说话（调度器 + 四重抑制器守着分寸）
    proactive_enabled: Optional[bool] = None

    _check_avatar = field_validator("avatar")(_validate_avatar)

    @field_validator("expression_dna")
    @classmethod
    def _check_dna(cls, value: Optional[dict]) -> Optional[dict]:
        return None if value is None else normalize_dna(value)


class CreateBotConversationRequest(BaseModel):
    persona_id: str
    channel: Literal["local"] = "local"


class MemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200)


class BotChatRequest(BaseModel):
    conversation_id: str
    content: str = Field(..., min_length=1, max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("images")
    @classmethod
    def _check_images(cls, value: list[str]) -> list[str]:
        # 与头像同口径：只收上传图片的 data URL；上限对齐前端（≤4 张，单张 base64 约 1.5M 字符）。
        for url in value:
            if not _AVATAR_DATA_URL.match(url):
                raise ValueError("图片仅支持上传的图片")
            if len(url) > 1_500_000:
                raise ValueError("单张图片过大，请换一张更小或更简单的")
        return value


@router.get("/bot/persona-options")
def get_persona_options():
    """人格编辑器的取值字典。

    档位文案只在服务端存一份：前端拿到 value + label 渲染控件即可，
    不需要把「短句为主」这类措辞在 .vue 里再抄一遍——抄了就会和提示词漂移。
    """
    return {**DNA_OPTIONS, **persona_options()}


@router.get("/bot/relations")
def list_relations():
    """全部人格的关系快照。一次查表算完，避免前端按人格逐个请求。"""
    rows = db.list_bot_relations()
    out: dict[str, dict] = {}
    for persona in db.list_bot_personas():
        persona_id = persona["id"]
        speed = persona.get("relationship_speed") or "normal"
        out[persona_id] = relation_mod.stage_from_row(
            rows.get(persona_id)
            or {"persona_id": persona_id, "score": 0.0, "turns": 0, "last_at": None},
            speed,
        )
    return out


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
    # 人格被机器人绑定时删不掉（FK RESTRICT）：先给可读的 409，别抛 500。
    if db.count_bot_channels(persona_id=persona_id):
        raise HTTPException(status_code=409, detail="这个人格已绑定机器人，请先解绑或删除机器人")
    if not db.delete_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    return {"deleted": True}


@router.get("/bot/personas/{persona_id}/proactive-log")
def list_proactive_log(persona_id: str, limit: int = 50):
    """主动性记录（P3）：她什么时候想找你、过没过闸、说了什么。调 θ 用。"""
    if not db.get_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT id, conversation_id, desire, action, reason, text, created_at, "
            "expires_at, delivered_at FROM bot_proactive_log "
            "WHERE persona_id = ? ORDER BY created_at DESC LIMIT ?",
            (persona_id, max(1, min(limit, 200))),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@router.get("/bot/personas/{persona_id}/memories")
def list_persona_memories(persona_id: str):
    """这个人格记住的事：面板上逐条列出，可单条忘记。"""
    if not db.get_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in memory_mod.list_memories(persona_id)
    ]


@router.post("/bot/personas/{persona_id}/memories")
def add_persona_memory(persona_id: str, req: MemoryRequest):
    """手动补一条记忆（「记住这个」）。"""
    if not db.get_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    memory_id = memory_mod.add_manual(persona_id, req.content)
    if not memory_id:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    return {"id": memory_id}


@router.delete("/bot/memories/{memory_id}")
def delete_memory(memory_id: str):
    """单条忘记。已失效的记忆再删一次是 404，不静默成功。"""
    if not memory_mod.forget_by_id(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True}


@router.post("/bot/personas/{persona_id}/memories/clear")
def clear_persona_memories(persona_id: str):
    """全部忘记。"""
    if not db.get_bot_persona(persona_id):
        raise HTTPException(status_code=404, detail="人格不存在")
    return {"cleared": memory_mod.clear_all(persona_id)}


def _conversation_payload(conversation: dict) -> dict:
    """会话对外形状：persona 只留展示三字段。

    性格/背景/表达 DNA 这些文本只用于拼提示词，逐会话重复下发没有意义；
    时间线里的每个会话最多几千字节，列表接口才不会被撑爆。
    """
    persona = conversation.get("persona") or {}
    return {
        **{key: value for key, value in conversation.items() if key != "persona"},
        "persona": {
            "id": persona.get("id") or conversation.get("persona_id") or "",
            # 人格被删后 persona_id 置空，名字只能靠快照；快照缺失才有兜底
            "name": persona.get("name") or conversation.get("title") or "",
            "avatar": persona.get("avatar") or "",
        },
    }


@router.get("/bot/conversations")
def list_conversations(
    channel: Optional[Literal["local", "qq"]] = None,
    persona_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    return [
        _conversation_payload(row)
        for row in db.list_bot_conversations(
            channel=channel, persona_id=persona_id, limit=limit
        )
    ]


@router.post("/bot/conversations")
def create_conversation(req: CreateBotConversationRequest):
    try:
        return _conversation_payload(db.create_bot_conversation(req.persona_id, req.channel))
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


class BotConversationUpdateRequest(BaseModel):
    # 仅 QQ 会话可用的本地显示覆盖；空串 = 清除回落默认，不设 min_length
    display_name: Optional[str] = Field(None, max_length=50)
    display_avatar: Optional[str] = Field(None, max_length=400_000)

    _check_avatar = field_validator("display_avatar")(_validate_avatar)


@router.patch("/bot/conversations/{conversation_id}")
def update_conversation(conversation_id: str, req: BotConversationUpdateRequest):
    conversation = db.get_bot_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 显示覆盖只服务 QQ 记录页：本地会话标题就是人格名，盖了反而误导
    if conversation.get("channel") != "qq":
        raise HTTPException(status_code=400, detail="仅 QQ 会话支持自定义显示名/头像")
    updated = db.update_bot_conversation_display(
        conversation_id, **req.model_dump(exclude_unset=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _conversation_payload(updated)


@router.post("/bot/chat")
async def chat(req: BotChatRequest):
    if not db.get_bot_conversation(req.conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    async def events():
        try:
            async for kind, text in chat_service.stream_reply(
                req.conversation_id, req.content, channel="local", images=req.images or None
            ):
                # kind 直接映射事件类型：reasoning 供前端折叠展示，content 是正文。
                yield sse_event({"type": kind, "content": text})
            message = db.get_bot_messages(req.conversation_id, 1)[-1]
            yield sse_event({"type": "done", "message": message})
        except Exception as exc:
            import netconfig

            logger.warning("cKunBot 回复失败: %s", exc, exc_info=True)
            detail = netconfig.redact_urls_in_text(str(exc)) or "回复生成失败"
            yield sse_event({"type": "error", "detail": detail})

    return StreamingResponse(events(), media_type="text/event-stream")


class GalleryItemRequest(BaseModel):
    label: str = Field("", max_length=50)
    data_url: str = Field(..., max_length=400_000)

    @field_validator("data_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        # 头像口径（256px webp data URL），不是 Phase 2 聊天图的 1.5M 通道
        if not _AVATAR_DATA_URL.match(value):
            raise ValueError("头像图库仅支持上传的图片")
        return value


@router.get("/bot/gallery")
def list_gallery():
    return db.list_bot_gallery()


@router.post("/bot/gallery")
def add_gallery_item(req: GalleryItemRequest):
    return db.create_bot_gallery_item(req.data_url, label=req.label)


@router.delete("/bot/gallery/{item_id}")
def remove_gallery_item(item_id: str):
    if not db.delete_bot_gallery_item(item_id):
        raise HTTPException(status_code=404, detail="图库条目不存在")
    return {"deleted": True}


class BotUserAvatarRequest(BaseModel):
    # 空串 = 清除（回落淡色占位）；与 persona.avatar 同闸门，走既有 _validate_avatar
    avatar: str = Field("", max_length=400_000)

    _check_avatar = field_validator("avatar")(_validate_avatar)


_USER_AVATAR_KEY = "bot_user_avatar"


@router.get("/bot/user-avatar")
def get_user_avatar():
    return {"avatar": db.get_app_setting(_USER_AVATAR_KEY, "") or ""}


@router.put("/bot/user-avatar")
def put_user_avatar(req: BotUserAvatarRequest):
    db.set_app_setting(_USER_AVATAR_KEY, req.avatar)
    return {"avatar": req.avatar}
