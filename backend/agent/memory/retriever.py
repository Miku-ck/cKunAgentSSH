"""v2 混合检索：FTS BM25 + 向量 KNN，RRF 融合 + 断层截断（spec §6）。

单一检索服务，三个消费方共用：run 前预检索注入 / memory_search 工具 /
Reconciler 对账相似检索。任何一路失败都独立降级，不阻塞对话。
"""
import asyncio
import logging
from dataclasses import dataclass

from ..cost_tracker import estimate_tokens
from . import mstore

logger = logging.getLogger(__name__)


@dataclass
class Hit:
    memory_id: str
    kind: str
    scope: str
    title: str
    content: str
    importance: float
    status: str
    source: str
    score: float          # RRF 融合分
    relevance: float      # 向量余弦（有关键词路时为 None → 仅排名参与）


async def search(
    query: str,
    *,
    scopes: list,
    kinds: "list | None" = None,
    top_k: int = 8,
    min_cosine: float = 0.3,
    embed_client=None,
    exclude_ids="()",
    statuses=mstore._ACTIVE_STATUSES,
) -> "list[Hit]":
    """两路并行召回 → RRF(k=60) 融合 → 余弦下限过滤 → 相对断层截尾。"""
    exclude = set(exclude_ids or ())
    fts_task = asyncio.to_thread(
        mstore.fts_hits, query, 20, scopes, kinds, statuses,
    )
    vec_task = _vec_path(query, scopes, kinds, statuses, embed_client)
    fts, vec = await asyncio.gather(fts_task, vec_task, return_exceptions=True)
    if isinstance(fts, BaseException):
        logger.warning("记忆 FTS 检索失败: %s", fts)
        fts = []
    if isinstance(vec, BaseException):
        logger.warning("记忆向量检索失败: %s", vec)
        vec = []

    rrf: dict = {}
    rel: dict = {}
    for lst in (vec, fts):
        for rank, (mid, val) in enumerate(lst or []):
            rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (60 + rank + 1)
            if lst is vec:
                rel[mid] = val

    order = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
    if vec:
        # 绝对下限只卡向量分（有向量路时）；fts-only 命中保留（关键词精确场景）
        order = [(m, s) for m, s in order if rel.get(m) is None or rel[m] >= min_cosine]
    order = [(m, s) for m, s in order if m not in exclude]

    # 断层截尾：相邻 RRF 相对头部差距 >0.15 处截断，防凑数注入
    cut: "list[str]" = []
    if order:
        top = order[0][1]
        for i, (mid, s) in enumerate(order):
            if i > 0 and top > 0 and (order[i - 1][1] - s) / top > 0.15:
                break
            cut.append(mid)
            if len(cut) >= top_k:
                break

    rows = await asyncio.to_thread(mstore.get_many, cut)
    by_id = {r["id"]: r for r in rows}
    hits = []
    for mid in cut:
        r = by_id.get(mid)
        if not r:
            continue
        hits.append(Hit(
            memory_id=mid, kind=r["kind"], scope=r["scope"], title=r.get("title") or "",
            content=r["content"], importance=r["importance"], status=r["status"],
            source=r["source"], score=rrf[mid], relevance=rel.get(mid),
        ))
    return hits


async def _vec_path(query, scopes, kinds, statuses, embed_client):
    if embed_client is None:
        return []
    qv = await embed_client.embed_one(query)
    if not qv:
        return []
    return await asyncio.to_thread(
        mstore.vec_search, qv, 20, scopes, kinds, statuses,
    )


async def retrieve_for_injection(
    query: str,
    *,
    scopes: list,
    embed_client=None,
    max_items: int = 5,
    budget_tokens: int = 1500,
) -> "list[str]":
    """预检索注入通道：渲染行 + token 预算裁剪 + 仅对实际注入者计热度。"""
    hits = await search(query, scopes=scopes, top_k=max_items * 2,
                        embed_client=embed_client)
    lines: "list[str]" = []
    injected_ids: "list[str]" = []
    used = 0
    for h in hits:
        line = _render(h)
        t = estimate_tokens(line)
        if used + t > budget_tokens:
            continue
        lines.append(line)
        injected_ids.append(h.memory_id)
        used += t
        if len(lines) >= max_items:
            break
    if injected_ids:
        await asyncio.to_thread(mstore.mark_injected, injected_ids)
    return lines


def _render(h: Hit) -> str:
    title = h.title or h.content[:24]
    prefix = "[待确认] " if h.status == "pending_reconcile" else ""
    return f"{prefix}[mem:{h.memory_id}] ({h.kind}/{h.scope}) {title}: {h.content}"
