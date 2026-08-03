"""
模型列表路由 — GET /api/models（从 DB 读取）
"""
from fastapi import APIRouter

import database as db

router = APIRouter(tags=["models"])


@router.get("/models")
async def list_models():
    """返回可用模型列表（供前端下拉选择）"""
    models = db.list_models_config()
    return [
        {
            "id": m["id"],
            "name": m["name"],
            "model_id": m.get("model_id"),
            "provider": m.get("provider"),
            "supports_thinking": bool(m["supports_thinking"]),
            "is_default": bool(m.get("is_default", 0)),
        }
        for m in models
    ]
