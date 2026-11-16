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

from fastapi import APIRouter, HTTPException, Query, File, UploadFile, Form
from pydantic import BaseModel, Field

import database as db

router = APIRouter(tags=["chat"])

logger = logging.getLogger(__name__)


# ── 文件上传 ──
# 仅允许文本类扩展名（作为 LLM 上下文）；二进制/图片请走其他渠道
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_ATTACHMENT_CHARS = 50_000  # 注入上下文的文本上限

ALLOWED_UPLOAD_EXTS = {
    ".txt", ".log", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".properties", ".xml", ".csv", ".tsv",
    ".sql", ".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".js", ".ts", ".jsx", ".tsx", ".vue", ".css", ".scss", ".less", ".html", ".htm",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php",
    ".rb", ".pl", ".kt", ".swift", ".gradle",
}

# 无扩展名的常见构建文件：Path("Makefile").suffix 是空串，只按后缀匹配永远放行不了
ALLOWED_UPLOAD_NAMES = {"makefile", "dockerfile"}

# 图片扩展名：走二进制存储 + 多模态注入（image_url），不做 UTF-8 解码与文本截断。
# 存储层刻意放宽（svg/ico/tiff/avif/heic/heif）：能否真的被模型消费是能力问题，
# 在模型调用层暴露并给出人话提示 —— 存储层拦掉反而让用户对着 400 一头雾水。
# ⚠️ 与前端 HomeComposer 的 IMAGE_EXT_BY_MIME 保持同口径（跨语言无法共享单一源）。
ALLOWED_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".svg", ".ico", ".tif", ".tiff", ".avif", ".heic", ".heif",
}

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# 视频扩展名：二进制落盘，模型无法直接观看 —— 上传回执只带身份，运行时把
# 存储路径与元数据提示注入任务（Agent 可用 shell 工具 ffprobe/ffmpeg 分析）。
# 刻意不含 .ts：该后缀已被文本白名单占用（TypeScript 源码），语义二义性
# 宁可拒收也不猜 —— MPEG-TS 用户请改用 .mp4/.mkv 等无歧义容器。
# ⚠️ 与前端 HomeComposer 的 VIDEO_EXT_BY_MIME 保持同口径。
ALLOWED_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".wmv", ".flv"}

_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
}

# 视频单文件上限：远大于文本/图片（10MB）—— 短屏录/排障录像常见几十 MB。
# 读取走 MAX+1 同款带上限读，不会先吃满内存。
MAX_VIDEO_SIZE = 100 * 1024 * 1024


def _uploads_root() -> Path:
    """上传文件根目录：与数据库同目录下的 uploads/（跨重装稳定）"""
    return Path(db.get_db_path()).parent / "uploads"


def _sanitize_conv(raw: str) -> str:
    """会话目录名消毒：仅保留字母数字/连字符/下划线，防止路径穿越。

    上传落盘与会话删除级联清理两处共用同一规则 —— 此前各抄一份 re.sub，
    靠注释维持一致（典型跨处漂移风险），现收敛到此单一实现。
    """
    return re.sub(r"[^A-Za-z0-9_\-]", "", raw or "")


@router.post("/chat/upload")
async def upload_chat_file(
    file: UploadFile = File(...),
    conversation_id: str = Form(""),
):
    """上传对话附件（文本类文件、图片或视频），返回内容供前端随消息注入 LLM 上下文"""
    raw_name = file.filename or "unnamed"
    # 消毒文件名：去掉路径分隔符，最长 120 字符
    safe_name = raw_name.replace("\\", "/").split("/")[-1][:120].strip() or "unnamed"
    suffix = Path(safe_name).suffix.lower()
    content_type = (file.content_type or "").lower()
    is_image = suffix in ALLOWED_IMAGE_EXTS or content_type.startswith("image/")
    # 视频判定放在文本白名单之前：.mp4 等后缀不在文本表里本就轮不到文本分支，
    # 这里显式短路是为了让 content_type=video/* 的无扩展名文件也走对通道。
    # 但必须让位于文本后缀：浏览器按扩展名猜 MIME，会把 .ts（TypeScript 源码）
    # 发成 video/mp2t —— 后缀与 content_type 冲突时后缀赢，MPEG-TS 请改容器。
    is_video = (suffix in ALLOWED_VIDEO_EXTS or content_type.startswith("video/")) \
        and suffix not in ALLOWED_UPLOAD_EXTS \
        and Path(safe_name).name.lower() not in ALLOWED_UPLOAD_NAMES
    if not is_image and not is_video and suffix not in ALLOWED_UPLOAD_EXTS \
            and Path(safe_name).name.lower() not in ALLOWED_UPLOAD_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{suffix or safe_name}'，仅支持文本类文件（txt/log/md/代码/配置等）、"
                   f"图片（png/jpg/webp 等）与视频（mp4/webm/mkv 等）",
        )

    # C-review MINOR-4：带上限读取（MAX+1）再校验大小 — 原实现全量读入内存后
    # 才检查，超限文件会先占用超出上限的内存。视频上限独立且更大。
    size_cap = MAX_VIDEO_SIZE if is_video else MAX_UPLOAD_SIZE
    cap_label = "100MB" if is_video else "10MB"
    data = await file.read(size_cap + 1)
    if len(data) > size_cap:
        raise HTTPException(status_code=413, detail=f"文件超过 {cap_label} 上限")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="文件内容为空")

    file_id = uuid.uuid4().hex
    # 消毒 conversation_id：仅保留字母数字/连字符/下划线，防止路径穿越；空则兜底 "general"
    safe_conv = _sanitize_conv(conversation_id) or "general"
    conv_dir = _uploads_root() / safe_conv
    # C-review MINOR-5：同步磁盘 IO 放线程池 — 原实现 async 路由内直接
    # mkdir/write_bytes（最多 10MB），阻塞事件循环（其余 db 调用都已 to_thread）
    await asyncio.to_thread(conv_dir.mkdir, parents=True, exist_ok=True)
    # C-review NIT：落盘名用完整 file_id — 原 file_id[:8]（32 bit）在高频上传
    # 下生日碰撞可覆盖他人上传文件
    dest = conv_dir / f"{file_id}_{safe_name}"
    await asyncio.to_thread(dest.write_bytes, data)

    if is_image:
        # 图片走二进制通道：绝不 decode（二进制 decode 只产出替换字符的乱码），
        # 也不存在 50k 文本截断 —— 模型消费的是原图（base64 data URL 在 /agent/run
        # 侧按需读取构造）。落盘副本保留在 uploads/，供 run 请求按 file_id 取用。
        mime = _IMAGE_MIME.get(suffix) or content_type or "image/png"
        return {
            "file_id": file_id,
            "filename": safe_name,
            "size": len(data),
            "content": "",
            "truncated": False,
            "kind": "image",
            "mime": mime,
        }

    if is_video:
        # 视频走二进制通道：模型无法直接观看视频内容，上传只做落盘存档；
        # 运行时（/agent/run 侧）按 file_id 解析出存储路径，把路径与 ffprobe
        # 分析提示注入任务文本，由 Agent 的 shell 工具按需检查元数据。
        mime = _VIDEO_MIME.get(suffix) or content_type or "video/mp4"
        return {
            "file_id": file_id,
            "filename": safe_name,
            "size": len(data),
            "content": "",
            "truncated": False,
            "kind": "video",
            "mime": mime,
        }

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
        "kind": "text",
    }


class CreateConversationRequest(BaseModel):
    # 标题会整份写进 localStorage 的会话缓存，建会话时不设限等于留下一条绕过校验的路径
    title: Optional[str] = Field(None, max_length=200)
    mode: Literal["ssh"] = "ssh"  # 标记会话所属模式，用于历史隔离


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
    # session 授权池随会话终结回收：PermissionGate 的 session 池按 conversation_id
    # 键控且进程长驻，once/denied 池都有 run 终态清理；会话终结有两条路径 ——
    # 这里的用户删除与保留清理（prune_conversations），两处都得回收，否则已消失
    # 会话的授权键在后端进程内逐会话累积（与 uploads 级联清理同一动机）。
    # 失败不阻塞删除（session 授权进程重启即清零）。
    try:
        from agent.harness.reins import _get_global_permission_gate_sync
        _get_global_permission_gate_sync().cleanup_conversation_session(cid)
    except Exception:
        logger.warning("conversation session 权限池回收失败（不阻塞删除）: %s", cid, exc_info=True)
    # B-MINOR 修复：级联清理 <db_dir>/uploads/<conv>/ 目录，
    # 避免会话删除后上传文件无限堆积（消毒走 _sanitize_conv，与上传接口同一实现；
    # 空会话 id 落入共享的 "general" 目录，不删除）
    try:
        import shutil as _shutil
        safe_conv = _sanitize_conv(cid)
        if safe_conv:
            root = _uploads_root().resolve()
            conv_dir = (root / safe_conv).resolve()
            # 路径必须 join 在 uploads 根下并校验，防止意外 rmtree 根外目录
            if conv_dir.is_dir() and root in conv_dir.parents:
                _shutil.rmtree(conv_dir, ignore_errors=True)
    except Exception:
        logger.warning("conversation uploads 级联清理失败（不阻塞删除）: %s", cid, exc_info=True)
    return {"deleted": True}
