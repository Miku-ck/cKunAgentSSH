"""
对话路由 — 附件上传 + 会话 CRUD
（普通流式对话已删除：AI 助手固定走 /agent/run，由后端自主意图识别）
"""
import asyncio
import logging
import re
from typing import Optional, Literal
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, File, UploadFile, Form
from pydantic import BaseModel, Field

import database as db

router = APIRouter(tags=["chat"])


# ── Request / Response Models ──
# 注意：以下常量必须在 AttachmentItem 之前定义（Field 默认值在类创建时求值）
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_ATTACHMENT_CHARS = 50_000  # 注入上下文的文本上限


class AttachmentItem(BaseModel):
    """对话附件 — 由 /chat/upload 上传后返回，随消息一并发送"""
    file_id: str = ""
    filename: str = ""
    content: str = Field("", max_length=MAX_ATTACHMENT_CHARS)


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


# ── 会话 CRUD ──


@router.get("/chat/conversations")
def list_conversations(limit: int = Query(default=50, ge=1, le=500),
                       offset: int = Query(default=0, ge=0),
                       mode: Optional[str] = None):
    """获取会话列表。mode 不为 None 时仅返回指定模式的会话

    审查修复：limit/offset 加边界 — limit=-1 在 SQLite 语义为"无限制"全表返回，
    其余负值直接 OperationalError → 500
    """
    return db.list_conversations(limit=limit, offset=offset, mode=mode)


@router.post("/chat/conversations")
def create_conversation(req: CreateConversationRequest):
    """创建新会话"""
    title = req.title or "New Chat"
    return db.create_conversation(title=title, mode=req.mode)


@router.get("/chat/conversations/{cid}/messages")
def get_messages(cid: str, limit: int = Query(default=50, ge=1, le=500),
                 offset: int = Query(default=0, ge=0)):
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
