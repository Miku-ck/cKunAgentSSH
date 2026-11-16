"""/chat/upload 图片通道与 /agent/run 图片引用解析回归

覆盖：
- 图片上传走二进制通道（kind=image、content 恒空、无 50k 截断），落盘副本存在
- 文本路径回执带 kind=text（老前端忽略该字段，行为不变）
- 未支持类型仍然 400；超限 413
- _load_run_image_blocks：形态校验 / 缺失副本 / 非图片后缀 / 张数上限 / 欢乐路径
"""
import base64
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database as db
from routes import chat as chat_routes
from routes import agent as agent_routes

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64  # 合法魔数开头的伪 PNG


def _client() -> TestClient:
    import fastapi

    app = fastapi.FastAPI()
    app.include_router(chat_routes.router, prefix="/api")
    return TestClient(app)


def _upload(client: TestClient, name: str, data: bytes, conv: str = "conv-1"):
    return client.post(
        "/api/chat/upload",
        files={"file": (name, data)},
        data={"conversation_id": conv},
    )


def _conv_id(base_db) -> str:
    return db.create_conversation(title="t")["id"]


def test_image_upload_uses_binary_channel(base_db):
    """图片不 decode 不截断：kind=image、content 空、mime 正确、副本在盘"""
    conv = _conv_id(base_db)
    resp = _upload(_client(), "截图.png", PNG_BYTES, conv)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "image"
    assert body["content"] == ""
    assert body["truncated"] is False
    assert body["mime"] == "image/png"
    assert body["size"] == len(PNG_BYTES)
    stored = (chat_routes._uploads_root() / conv / f"{body['file_id']}_截图.png")
    assert stored.is_file()
    assert stored.read_bytes() == PNG_BYTES


def test_image_upload_by_content_type_without_image_ext(base_db):
    """无扩展名但 content-type 是图片：按 MIME 分流；后缀不在映射表时 mime 回退 content_type"""
    resp = _client().post(
        "/api/chat/upload",
        files={"file": ("shot", PNG_BYTES, "image/png")},
        data={"conversation_id": "conv-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "image"
    assert body["mime"] == "image/png"


def test_text_upload_reports_kind_text(base_db):
    resp = _upload(_client(), "a.log", "hello\n".encode())
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "text"
    assert body["content"] == "hello\n"


def test_unsupported_ext_still_rejected(base_db):
    assert _upload(_client(), "virus.exe", b"MZ").status_code == 400


def test_image_size_cap_applies(base_db):
    big = PNG_BYTES + b"0" * (11 * 1024 * 1024)
    assert _upload(_client(), "big.png", big).status_code == 413


@pytest.mark.parametrize("name,mime", [
    ("diag.svg", "image/svg+xml"),
    ("favicon.ico", "image/x-icon"),
    ("scan.tif", "image/tiff"),
    ("scan.tiff", "image/tiff"),
    ("photo.avif", "image/avif"),
    ("ios.heic", "image/heic"),
    ("ios.heif", "image/heif"),
])
def test_extended_image_formats(base_db, name, mime):
    """多格式图片按扩展名分流，mime 按映射表回执（存储层放宽，能力问题留给模型层）"""
    resp = _upload(_client(), name, PNG_BYTES)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "image"
    assert body["mime"] == mime
    assert body["content"] == ""


# ── /agent/run 侧的图片引用解析 ────────────────────────────────────────────


def _seed_upload_file(conv: str, filename: str, data: bytes) -> str:
    file_id = uuid.uuid4().hex
    d = chat_routes._uploads_root() / conv
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_id}_{filename}").write_bytes(data)
    return file_id


@pytest.mark.asyncio
async def test_load_image_blocks_happy_path(base_db):
    conv = _conv_id(base_db)
    fid = _seed_upload_file(conv, "shot.png", PNG_BYTES)
    blocks = await agent_routes._load_run_image_blocks(
        [agent_routes.ImageRef(file_id=fid, filename="shot.png")], conv)
    assert blocks and len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "image_url"
    url = b["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES


@pytest.mark.asyncio
async def test_load_image_blocks_rejects_bad_file_id(base_db):
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_image_blocks(
            [agent_routes.ImageRef(file_id="../evil", filename="x.png")], "c")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_image_blocks_rejects_missing_copy(base_db):
    conv = _conv_id(base_db)
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_image_blocks(
            [agent_routes.ImageRef(file_id=uuid.uuid4().hex, filename="gone.png")], conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_image_blocks_rejects_non_image_suffix(base_db):
    conv = _conv_id(base_db)
    fid = _seed_upload_file(conv, "run.log", b"text")
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_image_blocks(
            [agent_routes.ImageRef(file_id=fid, filename="run.log")], conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_image_blocks_rejects_over_limit(base_db):
    conv = _conv_id(base_db)
    refs = [agent_routes.ImageRef(file_id=uuid.uuid4().hex, filename="a.png")
            for _ in range(agent_routes._RUN_IMAGE_LIMIT + 1)]
    with pytest.raises(HTTPException) as ei:
        await agent_routes._load_run_image_blocks(refs, conv)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_load_image_blocks_finds_copy_across_conversations(base_db):
    """上传落点与 run 会话不一致也能按 file_id 找到副本。

    真实路径：附件在会话创建之前挂上 → 上传退回 general/（或建失败的旧会话），
    run 却带正式会话 id 来找。文本附件不受影响，图片/视频按物理路径定位
    必须跨会话兜底，否则用户对着「附件已失效」发不出去。
    """
    orphan_conv = "general"
    fid = _seed_upload_file(orphan_conv, "(36).jpg", PNG_BYTES)
    run_conv = _conv_id(base_db)
    blocks = await agent_routes._load_run_image_blocks(
        [agent_routes.ImageRef(file_id=fid, filename="(36).jpg")], run_conv)
    assert blocks and len(blocks) == 1
    assert blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_load_image_blocks_none_passthrough(base_db):
    assert await agent_routes._load_run_image_blocks(None, "c") is None
