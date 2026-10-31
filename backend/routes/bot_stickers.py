"""表情包素材：刻意不挂 /api —— 图片由 <img> 与微信平台直接 GET，带不了 token。

只暴露内置的几张 png，键必须命中 manifest，不存在任意路径读取面。
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from bot import stickers as sticker_mod

router = APIRouter()


@router.get("/assets/stickers/{key}")
def read_sticker(key: str):
    """按 manifest 里的键返回内置 png；键不存在返回 404，命中时带一天强缓存。"""
    path = sticker_mod.path_for(key)
    if not path:
        raise HTTPException(status_code=404, detail="表情包不存在")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
