"""
对话路由 — SSE 流式聊天 + 会话 CRUD
"""
import asyncio
import logging
import re
from typing import Optional, Literal
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Request, File, UploadFile, Form
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
# 注意：以下常量必须在 AttachmentItem 之前定义（Field 默认值在类创建时求值）
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_ATTACHMENT_CHARS = 50_000  # 注入上下文的文本上限


class AttachmentItem(BaseModel):
    """对话附件 — 由 /chat/upload 上传后返回，随消息一并发送"""
    file_id: str = ""
    filename: str = ""
    content: str = Field("", max_length=MAX_ATTACHMENT_CHARS)


class StreamChatRequest(BaseModel):
    message: str = Field("", max_length=50000)  # 可空：仅当带 attachments 时允许空消息
    conversation_id: str
    model_id: str = ""  # 空字符串：触发 mode 兜底逻辑选择默认模型
    enable_thinking: bool = False
    system_prompt: Optional[str] = None  # 前端上下文提示词（文件/SSH 信息等）
    mode: Literal["ssh"] = "ssh"
    context_window: Optional[int] = None  # 历史消息条数上限，None 时用默认 20
    attachments: list[AttachmentItem] = []  # 附带文件（上传接口返回的内容）


# ── 文件上传 ──
# 仅允许文本类扩展名（作为 LLM 上下文）；二进制/图片请走其他渠道
ALLOWED_UPLOAD_EXTS = {
    ".txt", ".log", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".properties", ".xml", ".csv", ".tsv",
    ".sql", ".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".js", ".ts", ".jsx", ".tsx", ".vue", ".css", ".scss", ".less", ".html", ".htm",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php",
    ".rb", ".pl", ".kt", ".swift", ".gradle", ".makefile", ".dockerfile",
}


def _uploads_root() -> Path:
    """上传文件根目录：与数据库同目录下的 uploads/（跨重装稳定）"""
    return Path(db._db_path).parent / "uploads"


@router.post("/chat/upload")
async def upload_chat_file(
    file: UploadFile = File(...),
    conversation_id: str = Form(""),
):
    """上传对话附件（文本类文件），返回内容供前端随消息注入 LLM 上下文"""
    raw_name = file.filename or "unnamed"
    # 消毒文件名：去掉路径分隔符，最长 120 字符
    safe_name = raw_name.replace("\\", "/").split("/")[-1][:120].strip() or "unnamed"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{suffix or safe_name}'，仅支持文本类文件（txt/log/md/代码/配置等）",
        )

    # C-review MINOR-4：带上限读取（MAX+1）再校验大小 — 原实现全量读入内存后
    # 才检查，超限文件会先占用超出上限的内存
    data = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 10MB 上限")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="文件内容为空")

    file_id = uuid.uuid4().hex
    # 消毒 conversation_id：仅保留字母数字/连字符/下划线，防止路径穿越；空则兜底 "general"
    safe_conv = re.sub(r"[^A-Za-z0-9_\-]", "", conversation_id or "") or "general"
    conv_dir = _uploads_root() / safe_conv
    # C-review MINOR-5：同步磁盘 IO 放线程池 — 原实现 async 路由内直接
    # mkdir/write_bytes（最多 10MB），阻塞事件循环（其余 db 调用都已 to_thread）
    await asyncio.to_thread(conv_dir.mkdir, parents=True, exist_ok=True)
    # C-review NIT：落盘名用完整 file_id — 原 file_id[:8]（32 bit）在高频上传
    # 下生日碰撞可覆盖他人上传文件
    dest = conv_dir / f"{file_id}_{safe_name}"
    await asyncio.to_thread(dest.write_bytes, data)

    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_ATTACHMENT_CHARS
    if truncated:
        text = text[:MAX_ATTACHMENT_CHARS]

    return {
        "file_id": file_id,
        "filename": safe_name,
        "size": len(data),
        "content": text,
        "truncated": truncated,
        "conversation_id": conversation_id,
    }


class CreateConversationRequest(BaseModel):
    title: Optional[str] = None
    mode: Literal["ssh"] = "ssh"  # 标记会话所属模式，用于历史隔离


class UpdateTitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


# ── SSE 流式对话 ──


@router.post("/chat/stream")
async def stream_chat(req: StreamChatRequest, request: Request):
    """SSE 流式对话 — 对接 vLLM"""
    # 消息为空且无附件时拒绝（允许"仅上传附件"的空消息请求）
    if not req.message.strip() and not req.attachments:
        raise HTTPException(status_code=400, detail="消息内容为空")
    # ── 按 mode 选择默认模型（兜底）──
    # 审查 M-13：db 同步调用放线程池，避免阻塞事件循环（卡住所有并发请求/心跳）
    effective_model_id = req.model_id
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, req.mode)
        if default_model:
            # 审查外热修：优先行主键 id（原优先 LLM 模型名，与 get_model_config
            # 按 id 查询的契约不一致，会 400 Unknown model_id）
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        default_model = await asyncio.to_thread(db.get_default_model, None)
        if default_model:
            effective_model_id = default_model.get("id") or default_model.get("model_id")
    if not effective_model_id:
        raise HTTPException(status_code=400, detail=f"未找到 mode={req.mode} 的默认模型，请先配置模型")

    req.model_id = effective_model_id
    model_cfg = await asyncio.to_thread(db_get_model_config, req.model_id)  # 内部调用，含真实 api_key
    if not model_cfg:
        raise HTTPException(status_code=400, detail=f"Unknown model_id: {req.model_id}")

    # 验证会话存在
    conv = await asyncio.to_thread(db.get_conversation, req.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # 加载历史消息构建上下文 — 条数优先用请求参数，兜底 20
    # 前端 ChatPanel 的 contextWindow 设置会通过请求体透传过来
    # C-review M3：硬顶 100 条 — 无上限时用户设大后 prompt 超模型上下文，
    # 且历史只增不减，该会话此后每条请求都会 400
    history_limit = min(req.context_window, 100) if (req.context_window and req.context_window > 0) else 20
    history = await asyncio.to_thread(db.get_recent_messages, req.conversation_id, limit=history_limit)
    memories = await asyncio.to_thread(db.get_relevant_memories, req.mode, limit=5)

    # 构建上下文 prompt：前端传入的 system_prompt
    context_prompt = req.system_prompt or ""

    system_prompt = build_system_prompt(req.mode, context_prompt, memories)
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})

    # 组装用户消息：附带文件以 "[附件: 文件名]\n内容" 形式拼入上下文，
    # 使 LLM 可读取文件内容，且历史存库后仍可回看附件文本
    user_content = req.message
    if req.attachments:
        parts = [req.message] if req.message.strip() else []
        for att in req.attachments:
            parts.append(f"[附件: {att.filename}]\n{att.content}")
        user_content = "\n\n".join(parts)
    messages.append({"role": "user", "content": user_content})

    # 从模型配置读取推理参数；max_tokens 做安全封顶避免 400
    temperature = model_cfg.get("temperature") if model_cfg.get("temperature") is not None else 0.7
    top_p = model_cfg.get("top_p") if model_cfg.get("top_p") is not None else 0.9
    # C-review MINOR-7：推理预算同样占用输出预算 — 原实现未把 reasoning_tokens
    # 传给 safe_max_tokens，thinking 模型下 thinking_budget + 正文可超上下文
    _thinking_on = bool(req.enable_thinking and model_cfg.get("supports_thinking"))
    _reasoning_budget = model_cfg.get("reasoning_tokens") if _thinking_on else None
    # 审查 M-16：传入 prompt 文本（safe_max_tokens 内部用 estimate_tokens 估算，
    # 不再按 1.5 token/字符高估导致长对话输出被截到 256）
    max_tokens = safe_max_tokens(
        model_cfg.get("max_tokens"),
        " ".join(m["content"] for m in messages if isinstance(m.get("content"), str)),
        model_cfg.get("max_output_tokens"),
        _reasoning_budget,
    )

    return StreamingResponse(
        _generate_stream(
            base_url=model_cfg["base_url"],
            api_key=model_cfg["api_key"],
            model=model_cfg.get("model_id") or model_cfg["id"],
            messages=messages,
            conversation_id=req.conversation_id,
            model_id=req.model_id,
            mode=req.mode,
            enable_thinking=_thinking_on,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            request=request,
            user_message=user_content,
            # C-review MINOR-7：推理预算由调用方传入，不再二次查库（原实现在生成器
            # 内部又 db_get_model_config 一次，同请求内重复同步查询）
            reasoning_max_tokens=_reasoning_budget,
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
    reasoning_max_tokens: int | None = None,
):
    """SSE 流式生成器"""
    full_content = ""
    full_reasoning = ""
    # B-9 修复：预初始化，finally 中无论流创建是否成功都能安全关闭 LLM 流
    chunk_iter = None
    chunk_task = None  # M1 修复：进行中的 __anext__ 任务（shield 保活）

    try:
        # 先保存用户消息（审查 M-13：db 同步调用放线程池，避免阻塞事件循环）
        await asyncio.to_thread(
            db.add_message, conversation_id, "user", user_message, model_used=model_id,
        )
        await asyncio.to_thread(db.touch_conversation, conversation_id)
        await asyncio.to_thread(db.update_conversation_status, conversation_id, "in_progress")

        # 流式调用 LLM；推理/思考内容的 token 预算（仅 enable_thinking=True 时生效）
        # C-review MINOR-7：预算由调用方（safe_max_tokens 同款数据源）传入，
        # 原实现在此二次 db_get_model_config 重复查库
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
                # B-9 修复：断连立即关闭 LLM 流迭代器，停止拉取后续 token
                if chunk_task is not None:
                    chunk_task.cancel()
                try:
                    await chunk_iter.aclose()
                except Exception:
                    pass
                break
            # C-review M1 修复：chunk 任务用 ensure_future + shield 保活。
            # 原实现 asyncio.wait_for(chunk_iter.__anext__(), ...) 超时会取消
            # __anext__ 任务，CancelledError 抛进 async generator 帧将其永久
            # 关闭（llm_adapter finally 随即 aclose 底层流），下一轮 __anext__
            # 立即 StopAsyncIteration → 停顿 >HEARTBEAT_INTERVAL 的流被静默
            # 截断且半截内容按正常完成入库（已复现）。现在超时只发心跳，
            # chunk_task 继续存活，下一轮循环继续等它。
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            try:
                chunk = await asyncio.wait_for(
                    asyncio.shield(chunk_task), timeout=HEARTBEAT_INTERVAL
                )
                chunk_task = None
                if chunk.content:
                    full_content += chunk.content
                    yield sse_event({"content": chunk.content})
                if chunk.reasoning:
                    full_reasoning += chunk.reasoning
                    yield sse_event({"reasoning": chunk.reasoning})
            except StopAsyncIteration:
                chunk_task = None
                done = True
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

        # 保存助手消息
        # 审查 MINOR 修复：LLM 未产生任何内容时不写空消息行（原实现空串入库，
        # 前端渲染空气泡且污染历史上下文）
        if full_content or full_reasoning:
            await asyncio.to_thread(
                db.add_message,
                conversation_id,
                "assistant",
                full_content,
                reasoning=full_reasoning if full_reasoning else None,
                model_used=model_id,
            )
        await asyncio.to_thread(db.touch_conversation, conversation_id)
        await asyncio.to_thread(db.update_conversation_status, conversation_id, "completed")

        yield "data: [DONE]\n\n"

        # 异步后处理：记忆 + 自进化技能 + 标题生成
        # 通过 PostProcessManager 统一管理生命周期 + 总超时
        # turn_index 用于频率控制（标题仅首轮、技能每 3 轮、记忆每轮）
        try:
            # 查当前会话 user 消息数作为 turn_index（assistant 消息已保存，所以是 turn+1）
            # 审查 MINOR 修复：COUNT 查询替代 get_messages(limit=1000) 全量拉取
            _n_user = await asyncio.to_thread(db.count_messages_by_role, conversation_id, "user")
            turn_index = max(0, _n_user - 1)
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
        # 审查 MINOR 修复：LLM 响应中断时已生成的部分内容不再丢弃（原实现只发 error
        # 事件，用户看到空白但消息实际已生成一半）— 带中断标记入库
        if full_content or full_reasoning:
            try:
                partial = full_content
                if partial:
                    partial += "\n\n…[响应中断，以上内容不完整]"
                await asyncio.to_thread(
                    db.add_message,
                    conversation_id,
                    "assistant",
                    partial,
                    reasoning=full_reasoning if full_reasoning else None,
                    model_used=model_id,
                )
            except Exception:
                logger.exception("[chat] 保存中断部分内容失败")
        await asyncio.to_thread(db.update_conversation_status, conversation_id, "failed")
        yield sse_event({"error": str(e)})
        yield "data: [DONE]\n\n"
    finally:
        # M1 修复：先取消进行中的 __anext__ 任务，避免"Task was destroyed
        # but it is pending"告警；再关闭迭代器
        if chunk_task is not None and not chunk_task.done():
            chunk_task.cancel()
        # B-9 修复：客户端断连后 break 出流式循环，LLM 流迭代器若不关闭，
        # 底层 HTTP 连接会持续拉取/保持到响应结束，浪费 token 与连接
        try:
            if chunk_iter is not None:
                await chunk_iter.aclose()
        except Exception:
            pass
        # 客户端主动 abort (SSE 断开) 抛 CancelledError/ConnectionError 不被 except Exception 捕获，
        # 会话会卡 in_progress；仅在仍为 in_progress 时复位为 completed，避免覆盖 failed 状态
        try:
            cur = await asyncio.to_thread(db.get_conversation, conversation_id)
            if cur and cur.get("status") == "in_progress":
                await asyncio.to_thread(db.update_conversation_status, conversation_id, "completed")
        except Exception:
            pass


# ── 会话 CRUD ──


@router.get("/chat/conversations")
def list_conversations(limit: int = 50, offset: int = 0, mode: Optional[str] = None):
    """获取会话列表。mode 不为 None 时仅返回指定模式的会话"""
    return db.list_conversations(limit=limit, offset=offset, mode=mode)


@router.post("/chat/conversations")
def create_conversation(req: CreateConversationRequest):
    """创建新会话"""
    title = req.title or "New Chat"
    return db.create_conversation(title=title, mode=req.mode)


@router.get("/chat/conversations/{cid}/messages")
def get_messages(cid: str, limit: int = 50, offset: int = 0):
    """获取会话消息"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return db.get_messages(cid, limit=limit, offset=offset)


@router.delete("/chat/conversations/{cid}")
def delete_conversation(cid: str):
    """删除会话（级联删除该会话的上传附件目录）"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    deleted = db.delete_conversation(cid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # B-MINOR 修复：级联清理 <db_dir>/uploads/<conv>/ 目录，
    # 避免会话删除后上传文件无限堆积（与上传接口的消毒规则保持一致；
    # 空会话 id 落入共享的 "general" 目录，不删除）
    try:
        import shutil as _shutil
        safe_conv = re.sub(r"[^A-Za-z0-9_\-]", "", cid or "")
        if safe_conv:
            root = _uploads_root().resolve()
            conv_dir = (root / safe_conv).resolve()
            # 路径必须 join 在 uploads 根下并校验，防止意外 rmtree 根外目录
            if conv_dir.is_dir() and root in conv_dir.parents:
                _shutil.rmtree(conv_dir, ignore_errors=True)
    except Exception:
        pass
    return {"deleted": True}


@router.patch("/chat/conversations/{cid}")
def update_conversation(cid: str, req: UpdateTitleRequest):
    """更新会话标题"""
    conv = db.get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = db.update_conversation_title(cid, req.title)
    if not result:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return result
