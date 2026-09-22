"""Embedding 客户端：仅真实 API，失败即无向量（宁缺毋滥）。

v1 的 n-gram 伪向量是误注入源头，v2 彻底移除：embed 不可用时返回 None，
调用方将该记忆标为"仅 FTS 可检"，候选照常入队（spec §11）。
"""
import logging
import time
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

# API 失败后的静默期：期内直接返回 None，到期放行一次重试
_RETRY_AFTER_SECONDS = 300.0


class EmbeddingClient:
    def __init__(self, base_url: str = "", api_key: str = "", model: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self._down_until = 0.0

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"

    async def embed(self, texts: list[str]) -> "list[list[float]] | None":
        """批量向量化；任何失败返回 None（调用方走 FTS-only）。"""
        if not texts or not self.base_url or not self.model:
            return None
        if time.monotonic() < self._down_until:
            return None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._endpoint(),
                    headers=headers,
                    json={
                        "model": self.model,
                        "input": [t[:8000] for t in texts],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vecs = [it["embedding"] for it in items]
            if len(vecs) != len(texts):
                raise ValueError("embedding 数量与输入不一致")
            return vecs
        except Exception as e:
            logger.debug("embedding API 不可用（%ss 后重试）: %s", _RETRY_AFTER_SECONDS, e)
            self._down_until = time.monotonic() + _RETRY_AFTER_SECONDS
            return None

    async def embed_one(self, text: str) -> "list[float] | None":
        vecs = await self.embed([text])
        return vecs[0] if vecs else None


_cache: "OrderedDict[tuple, EmbeddingClient]" = OrderedDict()
_CACHE_MAX = 4


def get_embedding_client(base_url: str, api_key: str, model: str) -> EmbeddingClient:
    key = (base_url, api_key, model)
    c = _cache.get(key)
    if c is None:
        c = EmbeddingClient(base_url, api_key, model)
        _cache[key] = c
        if len(_cache) > _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    else:
        _cache.move_to_end(key)
    return c


def clear_cache() -> None:
    _cache.clear()
