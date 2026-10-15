"""Embedding 客户端：仅真实 API，失败即无向量（宁缺毋滥）。

v1 的 n-gram 伪向量是误注入源头，v2 彻底移除：embed 不可用时返回 None，
调用方将该记忆标为"仅 FTS 可检"，候选照常入队（spec §11）。
"""
import logging
import time
from collections import OrderedDict

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
            # PERF-005：共享 AsyncClient（键=超时+代理），不再每次调用重付
            # TCP(+TLS) 握手；连接池随进程长驻，HTTP 策略变更由 settings
            # 写入侧调 invalidate_http_clients() 重建（与 llm_adapter 同钩子）
            client = _shared_http_client()
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


# PERF-005：共享 httpx.AsyncClient 池。客户端只依赖 HTTP 策略（超时/代理），
# 与 embedding 模型配置无关 —— 模型切换走上面的 EmbeddingClient LRU 即可，
# 策略变更（proxy/timeout 写入）由 routes/settings.py 调 invalidate_http_clients()。
# 不做 LRU：全局策略单值，正常只有 1~2 个键，失效时整体清空不会增长。
_EMBED_TIMEOUT = 10.0
_http_clients: dict = {}


def _shared_http_client():
    """按 (超时, 代理) 取共享 AsyncClient；调用方不关闭（连接池随进程长驻）。"""
    import netconfig
    key = (_EMBED_TIMEOUT, netconfig.proxy_url() or "")
    client = _http_clients.get(key)
    if client is None or client.is_closed:
        client = netconfig.new_client(default_timeout=_EMBED_TIMEOUT)
        _http_clients[key] = client
    return client


async def invalidate_http_clients() -> int:
    """HTTP 策略（代理/超时）变更后调用：关闭并丢弃共享客户端，下次按新策略重建。

    仍有在途请求的客户端被 aclose 会让该次 embed 失败 —— 影响限于单次调用
    （调用方按 None 走 FTS-only），不做 llm_adapter 那套在途计数：embed 请求
    短（≤10s）且失效是低频的设置保存动作，简单优先。"""
    stale = list(_http_clients.values())
    _http_clients.clear()
    closed = 0
    for c in stale:
        try:
            if not c.is_closed:
                await c.aclose()
                closed += 1
        except Exception:
            pass  # 关闭失败依赖 GC 回收
    return closed
