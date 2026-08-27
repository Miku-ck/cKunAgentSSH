"""
对话路由 — SSE 流式聊天 + 会话 CRUD
"""
import asyncio
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db
from llm_adapter import chat_stream
from prompts import build_system_prompt
from safe_tokens import safe_max_tokens
from sse_utils import sse_event
from database import get_model_config as _db_get_model_config_raw


def db_get_model_config(model_id: str):
    """内部调用：获取模型配置（含真实 api_key，不脱敏）"""
    return _db_get_model_config_raw(model_id, mask_key=False)

router = APIRouter(tags=["chat"])

HEARTBEAT_INTERVAL = 15  # 秒


# ── Request / Response Models ──


class StreamChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=50000)
    conversation_id: str
    model_id: str = ""  # 空字符串：触发 mode 兜底逻辑选择默认模型
    enable_thinking: bool = False
    system_prompt: Optional[str] = None  # 前端上下文提示词（文件/SSH 信息等）
    mode: Literal["ssh"] = "ssh"
    context_window: Optional[int] = None  # 历史消息条数上限，None 时用默认 20


class CreateConversationRequest(BaseModel):
    title: Optional[str] = None
    mode: Literal["ssh"] = "ssh"  # 标记会话所属模式，用于历史隔离


class UpdateTitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


# ── SSE 流式对话 ──


@router.post("/chat/stream")
async def stream_chat(req: StreamChatRequest, request: Request):
    """SSE 流式对话 — 对接 vLLM"""
    # ── 按 mode 选择默认模型（兜底）──
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = db.get_default_model(req.mode)
        if default_model:
            effective_model_id = default_model.get("model_id") or default_model.get("id")
    if not effective_model_id:
        default_model = db.get_default_model(None)
        if default_model:
            effective_model_id = default_model.get("model_id") or default_model.get("id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail=f"未找到 mode={req.mode} 的默认模型，请先配置模型")

    req.model_id = effective_model_id
    model_cfg = db_get_model_config(req.model_id)  # 内部调用，含真实 api_key
    if not model_cfg:
        raise HTTPException(status_code=400, detail=f"Unknown model_id: {req.model_id}")

    # 验证会话存在
    conv = db.get_conversation(req.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # 加载历史消息构建上下文 — 条数优先用请求参数，兜底 20
    # 前端 ChatPanel 的 contextWindow 设置会通过请求体透传过来
    history_limit = req.context_window if (req.context_window and req.context_window > 0) else 20
    history = db.get_recent_messages(req.conversation_id, limit=history_limit)
    memories = db.get_relevant_memories(req.mode, limit=5)

    # 构建上下文 prompt：前端传入的 system_prompt
    context_prompt = req.system_prompt or ""

    system_prompt = build_system_prompt(req.mode, context_prompt, memories)
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    # 从模型配置读取推理参数；max_tokens 做安全封顶避免 400
    temperature = model_cfg.get("temperature") if model_cfg.get("temperature") is not None else 0.7
    top_p = model_cfg.get("top_p") if model_cfg.get("top_p") is not None else 0.9
    max_tokens = safe_max_tokens(
        model_cfg.get("max_tokens"),
        sum(len(m["content"]) for m in messages),
        model_cfg.get("max_output_tokens"),
    )

    return StreamingResponse(
        _generate_stream(
            base_url=model_cfg["base_url"],
            api_key=model_cfg["api_key"],
            model=model_cfg.get("model_id") or model_cfg["id"],
            messages=messages,
            conversation_id=req.conversation_id,
            user_message=req.message,
            model_id=req.model_id,
            mode=req.mode,
            enable_thinking=req.enable_thinking and model_cfg["supports_thinking"],
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            request=request,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _generate_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    conversation_id: str,
    user_message: str,
    model_id: str,
    mode: str,
    enable_thinking: bool,
    temperature: float = 0.7,
    top_p: float = 0.9,
    max_tokens: int | None = None,
    request: Request = None,
):
    """SSE 流式生成器"""
    full_content = ""
    full_reasoning = ""

    try:
        # 先保存用户消息
        db.add_message(conversation_id, "user", user_message, model_used=model_id)
        db.touch_conversation(conversation_id)
        db.update_conversation_status(conversation_id, "in_progress")

# 流式调用 LLM；推理/思考内容的 token 预算（仅 enable_thinking=True 时生效）
        _cfg = db_get_model_config(model_id)
        reasoning_max_tokens = _cfg.get("reasoning_tokens") if enable_thinking else None
        chunk_iter = chat_stream(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            reasoning_max_tokens=reasoning_max_tokens,
        )

        done = False
        while not done:
            # 检测客户端是否已断开，避免浪费 LLM 资源
            if request and await request.is_disconnected():
                break
            try:
                chunk = await asyncio.wait_for(
                    chunk_iter.__anext__(), timeout=HEARTBEAT_INTERVAL
                )
                if chunk.content:
                    full_content += chunk.content
                    yield sse_event({"content": chunk.content})
                if chunk.reasoning:
                    full_reasoning += chunk.reasoning
                    yield sse_event({"reasoning": chunk.reasoning})
            except StopAsyncIteration:
                done = True
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

        # 保存助手消息
        db.add_message(
            conversation_id,
            "assistant",
            full_content,
            reasoning=full_reasoning if full_reasoning else None,
            model_used=model_id,
        )
        db.touch_conversation(conversation_id)
        db.update_conversation_status(conversation_id, "completed")

        yield "data: [DONE]\n\n"

        # 异步后处理：记忆 + 自进化技能 + 标题生成
        # 通过 PostProcessManager 统一管理生命周期 + 总超时
        # turn_index 用于频率控制（标题仅首轮、技能每 3 轮、记忆每轮）
        try:
            # 查当前会话已有消息数作为 turn_index（assistant 消息已保存，所以是 turn+1）
            existing_msgs = db.get_messages(conversation_id, limit=1000)
            turn_index = max(0, len([m for m in existing_msgs if m.get("role") == "user"]) - 1)
        except Exception:
            turn_index = 0

        from post_processor import get_post_process_manager
        await get_post_process_manager().submit(
            mode=mode,
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_content=full_content,
            assistant_reasoning=full_reasoning,
            model_id=model_id,
            turn_index=turn_index,
        )

    except Exception as e:
        db.update_conversation_status(conversation_id, "failed")
        yield sse_event({"error": str(e)})
        yield "data: [DONE]\n\n"
    finally:
        # 客户端主动 abort (SSE 断开) 抛 CancelledError/ConnectionError 不被 except Exception 捕获，
        # 会话会卡 in_progress；仅在仍为 in_progress 时复位为 completed，避免覆盖 failed 状态
        try:
            cur = db.get_conversation(conversation_id)
            if cur and cur.get("status") == "in_progress":
                db.update_conversation_status(conversation_id, "completed")
        except Exception:
            pass


# ── 会话 CRUD ──


@router.get("/chat/conversations")
async def list_conversations(limit: int = 50, offset: int = 0, mode: Optional[str] = None):
    """获取会话列表。mode 不为 None 时仅返回指定模式的会话"""
    return db.list_conversations(limit=limit, offset=offset, mode=mode)


@router.post("/chat/conversations")
async def create_conversation(req: CreateConversationRequest):
    """创建新会话"""
    title = req.title or "New Chat"
    return db.create_conversation(title=title, mode=req.mode)


@router.get("/chat/conversations/{cid}/messages")
async def get_messages(cid: str, limit: int = 50, offset: int = 0):
    """获取会话消息"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return db.get_messages(cid, limit=limit, offset=offset)


@router.delete("/chat/conversations/{cid}")
async def delete_conversation(cid: str):
    """删除会话"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    deleted = db.delete_conversation(cid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


@router.patch("/chat/conversations/{cid}")
async def update_conversation(cid: str, req: UpdateTitleRequest):
    """更新会话标题"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = db.update_conversation_title(cid, req.title)
    if not result:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result
