"""/chat/upload 视频通道与 /agent/run 视频引用注入回归

覆盖：
- 视频上传走二进制通道（kind=video、content 恒空、mime 映射），独立 100MB 上限
- .ts 语义二义性守卫：必须仍是 TypeScript 文本通道，不被视频判定吞掉
- _load_run_video_note：欢乐路径（路径注入 + ffprobe 提示）/ 校验 fail-closed
"""
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database as db
from routes import chat as chat_routes
from routes import agent as agent_routes

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64


def _client() -> TestClient:
    import fastapi

    app = fastapi.FastAPI()
    app.include_router(chat_routes.router, prefix="/api")
    return TestClient(app)


def _upload(client: TestClient, name: str, data: bytes, conv: str = "conv-1", ctype: str | None = None):
    kwargs = {"files": {"file": (name, data) if ctype is None else (name, data, ctype)},
              "data": {"conversation_id": conv}}
    return client.post("/api/chat/upload", **kwargs)


def _conv_id(base_db) -> str:
    return db.create_conversation(title="t")["id"]


@pytest.mark.parametrize("name,mime", [
    ("rec.mp4", "video/mp4"),
    ("clip.webm", "video/webm"),
    ("demo.mkv", "video/x-matroska"),
    ("ios.mov", "video/quicktime"),
])
def test_video_upload_uses_binary_channel(base_db, name, mime):
    conv = _conv_id(base_db)
    resp = _upload(_client(), name, FAKE_MP4, conv)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "video"
    assert body["content"] == ""
    assert body["truncated"] is False
    assert body["mime"] == mime
    stored = (chat_routes._uploads_root() / conv / f"{body['file_id']}_{name}")
    assert stored.is_file()
    assert stored.read_bytes() == FAKE_MP4


def test_ts_extension_stays_text_not_video(base_db):
    """.ts 语义冲突守卫：TypeScript 源码通道不被视频判定劫持（MPEG-TS 请用 .mp4/.mkv）"""
    resp = _upload(_client(), "main.ts", b"const a = 1\n")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "text"
    assert "const a = 1" in body["content"]


def test_video_size_cap_independent_from_text(base_db, monkeypatch):
    """视频 100MB 独立上限（不占文本/图片 10MB 口径），超限 413"""
    monkeypatch.setattr(chat_routes, "MAX_VIDEO_SIZE", 1024)
    resp = _upload(_client(), "big.mp4", FAKE_MP4 + b"0" * 2048)
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_load_video_note_happy_path(base_db):
    conv = _conv_id(base_db)
    file_id = uuid.uuid4().hex
    d = chat_routes._uploads_root() / conv
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{file_id}_rec.mp4"
    dest.write_bytes(FAKE_MP4)

    note = await agent_routes._load_run_video_note(
        [agent_routes.ImageRef(file_id=file_id, filename="rec.mp4")], conv)
    assert "[视频附件]" in note
    assert "ffprobe" in note                      # 分析手段必须告知
    assert str(dest) in note                      # 模型要拿得到绝对路径
    assert "无法直接观看" in note                 # 诚实声明能力边界


@pytest.mark.asyncio
async def test_load_video_note_rejects_bad_ref(base_db):
    conv = _conv_id(base_db)
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_video_note(
            [agent_routes.ImageRef(file_id="../evil", filename="x.mp4")], conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_video_note_rejects_missing_copy(base_db):
    conv = _conv_id(base_db)
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_video_note(
            [agent_routes.ImageRef(file_id=uuid.uuid4().hex, filename="gone.mp4")], conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_video_note_rejects_non_video_suffix(base_db):
    conv = _conv_id(base_db)
    file_id = uuid.uuid4().hex
    d = chat_routes._uploads_root() / conv
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_id}_a.ts").write_bytes(b"const x = 1")
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_video_note(
            [agent_routes.ImageRef(file_id=file_id, filename="a.ts")], conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_video_note_empty_passthrough(base_db):
    assert await agent_routes._load_run_video_note(None, "c") == ""
