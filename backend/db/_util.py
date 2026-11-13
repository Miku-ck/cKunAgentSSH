"""通用小工具 — 从 database.py 原样复制。"""
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex


def _mask_api_key(key: str | None) -> str:
    """脱敏 API Key：仅保留前4位 + ****"""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"
