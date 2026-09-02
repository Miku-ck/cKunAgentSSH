"""
三层记忆系统 — Agent 的长期记忆

三层结构:
  1. Working Memory（工作记忆）: 当前会话 messages，由 AgentEngine 在内存中维护，不持久化
  2. Episodic Memory（情景记忆）: Agent 运行结束后的总结，存 DB，按 mode + 时间检索
  3. Semantic Memory（语义记忆）: 从对话中提取的事实/知识，存 DB + embedding 向量，
                                  按 query 的语义相似度检索

Embedding 生成策略:
  - 优先调用 base_url 的 /v1/embeddings 端点（OpenAI 兼容服务大多支持）
  - 失败时回退到本地哈希伪 embedding（dim=256），保证无 embedding 服务时也能工作

检索逻辑:
  - Episodic: 按 mode 过滤 + 最近使用时间排序，取 top_k
  - Semantic: query embedding 与所有 semantic 记忆的 embedding 做余弦相似度，
              取 score > threshold 的 top_k
"""
import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 伪 embedding 维度（仅在 embedding API 不可用时使用）
_FALLBACK_DIM = 256
# 语义检索相似度阈值
_DEFAULT_THRESHOLD = 0.3
# 默认 top_k
_DEFAULT_TOP_K = 5
# 语义检索候选池上限（审查 MINOR：原为 200，含 embedding 全文拉取负载过高）
_SEMANTIC_CANDIDATE_CAP = 100

# 模块级缓存：按 (base_url, api_key, embedding_model) 缓存 MemoryManager 实例，
# 避免每次 Agent 运行都重建实例导致 _embedding_available 缓存丢失。
_memory_mgr_cache: dict[tuple, "MemoryManager"] = {}
_MEMORY_MGR_CACHE_MAX = 4


def get_memory_manager(
    base_url: str = "",
    api_key: str = "",
    embedding_model: str = "",
) -> "MemoryManager":
    """获取或创建缓存的 MemoryManager 实例"""
    key = (base_url, api_key, embedding_model)
    mgr = _memory_mgr_cache.get(key)
    if mgr is None:
        mgr = MemoryManager(base_url, api_key, embedding_model)
        _memory_mgr_cache[key] = mgr
        # LRU 式淘汰：超过上限时移除最早的条目
        if len(_memory_mgr_cache) > _MEMORY_MGR_CACHE_MAX:
            oldest_key = next(iter(_memory_mgr_cache))
            _memory_mgr_cache.pop(oldest_key, None)
    return mgr


def invalidate_memory_manager_cache() -> None:
    """清除所有缓存的 MemoryManager 实例（模型配置变更时调用）"""
    _memory_mgr_cache.clear()


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    type: str               # "episodic" / "semantic"
    mode: str
    content: str
    source_conv_id: str = ""
    agent_run_id: str = ""
    embedding: list[float] | None = None  # 仅 semantic 类型有
    importance: float = 0.5              # 重要性 0-1
    created_at: str = ""
    last_used_at: str = ""
    use_count: int = 0


@dataclass
class MemoryQueryResult:
    """记忆检索结果"""
    memory: MemoryEntry
    score: float


class MemoryManager:
    """三层记忆管理器"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        embedding_model: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.embedding_model = embedding_model  # 由调用方传入实际 model_id
        # 缓存最近一次 embedding API 是否可用，避免每次都试错
        self._embedding_available: Optional[bool] = None
        # embedding API 失败计数：每 5 次重试一次，避免永久放弃
        self._embedding_fail_count = 0

    # ── Embedding 生成 ──

    async def embed(self, text: str) -> list[float]:
        """
        生成文本的 embedding 向量

        优先调用远程 embedding API；失败时回退到本地伪 embedding。
        """
        if not text.strip():
            return [0.0] * _FALLBACK_DIM

        # 若已确认 embedding API 不可用，直接走回退
        if self._embedding_available is False:
            self._embedding_fail_count += 1
            # 每 5 次调用重试一次 API
            if self._embedding_fail_count % 5 != 0:
                return self._fallback_embed(text)
            # 重试
            self._embedding_available = None  # 重置为未确认，允许重试

        # 若未配置 base_url 或 embedding_model，直接走回退
        if not self.base_url or not self.embedding_model:
            self._embedding_available = False
            return self._fallback_embed(text)

        try:
            vec = await self._call_embedding_api(text)
            self._embedding_available = True
            return vec
        except Exception as e:
            # embedding 服务不可用是常见场景（如 vLLM 未部署 embedding 模型），
            # 降级到伪 embedding 是设计内行为，用 debug 级别避免日志噪音
            logger.debug(
                "Embedding API 不可用，回退到本地伪 embedding: %s", e
            )
            self._embedding_available = False
            return self._fallback_embed(text)

    async def _call_embedding_api(self, text: str) -> list[float]:
        """调用 OpenAI 兼容的 /v1/embeddings 端点"""
        # base_url 可能已含 /v1（如 http://host:9030/v1），也可能不含
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/embeddings"
        else:
            url = f"{base}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={
                    "model": self.embedding_model,
                    "input": text[:8000],  # 截断防止超长
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]

    def _fallback_embed(self, text: str) -> list[float]:
        """
        本地伪 embedding：基于字符 n-gram 哈希
        不是真正的语义 embedding，但能保证基本可用
        """
        vec = [0.0] * _FALLBACK_DIM
        # 字符级 2-gram + 3-gram
        text_lower = text.lower()
        for n in (2, 3):
            for i in range(len(text_lower) - n + 1):
                gram = text_lower[i:i + n]
                h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                vec[h % _FALLBACK_DIM] += 1.0
        # 词级 hash
        words = re.findall(r"\w+", text_lower)
        for w in words:
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            vec[h % _FALLBACK_DIM] += 2.0

        # L2 归一化
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    # ── 检索 ──

    async def retrieve(
        self,
        query: str,
        mode: str,
        top_k: int = _DEFAULT_TOP_K,
        threshold: float = 0.15,
    ) -> list[MemoryQueryResult]:
        """
        检索相关记忆（合并 episodic + semantic）

        Args:
            query: 当前任务/查询文本
            mode: 当前模式（work/ide/ssh）
            top_k: 返回最大条数
            threshold: 相似度阈值（episodic 关键词重叠分 + semantic 余弦相似度通用）。
                       E 修复：原默认 0.3 且仅 semantic 生效，episodic 硬编码 0.15；
                       现统一为传入阈值，默认 0.15（episodic 原行为，向后兼容）。
                       semantic 的既有调用方均显式传 threshold（如 0.3），不受影响。
        """
        # 延迟导入避免循环依赖
        import database as db

        results: list[MemoryQueryResult] = []

        # 1. Episodic 记忆 — 按 mode 取最近 top_k * 3 条候选，再用关键词重叠过滤
        # 修复：原实现无条件取最近 top_k 条，导致用户问"你是什么模型"时被注入
        # 之前 Agent 跑过的"任务: xxx | 使用工具: read_file | 结果: ..."等无关记忆
        try:
            # C-review MINOR-6：同步 sqlite 调用放线程池（WAL 锁竞争时不卡事件循环）
            episodic_rows = await asyncio.to_thread(
                db.get_agent_memories,
                mode=mode, mem_type="episodic", limit=top_k * 3,
            )
            query_terms = _extract_terms(query)
            for row in episodic_rows:
                entry = _row_to_entry(row)
                # 关键词重叠相似度（Jaccard 变体）
                content_terms = _extract_terms(entry.content)
                score = _jaccard_score(query_terms, content_terms)
                # E 修复：改用传入的 threshold（默认 0.15，关键词匹配较弱的放宽值）
                if score >= threshold:
                    results.append(MemoryQueryResult(memory=entry, score=score))
        except Exception as e:
            logger.warning("Episodic 记忆检索失败: %s", e)

        # 2. Semantic 记忆 — 按语义相似度检索
        # 审查 MINOR 修复：候选池由 200 降为 _SEMANTIC_CANDIDATE_CAP（100）。
        # 每次 retrieve 都会拉取候选池全部行（含 embedding 向量，单行可达数 KB），
        # 200 行 ≈ 1~2MB 文本 + 200 次元余弦；按 last_used_at 倒序取最近使用的
        # 100 条在召回质量上基本无损（长期冷记忆命中概率极低），负载减半。
        # 池子饱和（行数 == 上限）时提示一次，便于后续评估是否需要 DB 级向量索引。
        try:
            # C-review MINOR-6：同步 sqlite 调用放线程池
            semantic_rows = await asyncio.to_thread(
                db.get_agent_memories,
                mode=mode, mem_type="semantic", limit=_SEMANTIC_CANDIDATE_CAP,
            )
            if len(semantic_rows) >= _SEMANTIC_CANDIDATE_CAP and not getattr(self, "_semantic_pool_warned", False):
                self._semantic_pool_warned = True
                logger.warning(
                    "semantic 记忆候选池饱和（%d 条），检索召回可能受限；"
                    "记忆规模持续增长时建议引入 DB 级向量索引",
                    _SEMANTIC_CANDIDATE_CAP,
                )
            if semantic_rows:
                query_vec = await self.embed(query)
                query_dim = len(query_vec) if query_vec else 0
                scored = []
                mismatch_count = 0
                for row in semantic_rows:
                    entry = _row_to_entry(row)
                    if not entry.embedding:
                        continue
                    # 维度校验：不匹配则跳过并记录
                    if len(entry.embedding) != query_dim:
                        mismatch_count += 1
                        continue
                    score = _cosine(query_vec, entry.embedding)
                    if score >= threshold:
                        scored.append((MemoryQueryResult(memory=entry, score=score), score))
                if mismatch_count > 0:
                    logger.warning(
                        "semantic 记忆 embedding 维度不匹配: %d 条被跳过（query_dim=%d）",
                        mismatch_count, query_dim,
                    )
                # 按分数排序取 top_k
                scored.sort(key=lambda x: x[1], reverse=True)
                results.extend([r for r, _ in scored[:top_k]])
        except Exception as e:
            logger.warning("Semantic 记忆检索失败: %s", e)

        # 合并去重 + 排序 + 截断
        seen_ids = set()
        unique = []
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            if r.memory.id in seen_ids:
                continue
            seen_ids.add(r.memory.id)
            unique.append(r)
            if len(unique) >= top_k:
                break

        # touch 检索到的记忆（更新 last_used_at）
        # C-review MINOR-6：同步 sqlite 调用放线程池
        for r in unique:
            try:
                await asyncio.to_thread(db.touch_agent_memory, r.memory.id)
            except Exception:
                pass

        return unique

    # ── 保存 ──

    async def save_episodic(
        self,
        mode: str,
        content: str,
        source_conv_id: str = "",
        agent_run_id: str = "",
        importance: float = 0.5,
    ) -> Optional[str]:
        """保存情景记忆（Agent 运行总结）"""
        if not content.strip():
            return None
        # 延迟导入避免循环依赖
        import database as db
        try:
            # D 修复（事件循环阻塞）：同步 INSERT 放线程池
            return await asyncio.to_thread(
                db.add_agent_memory,
                mode=mode,
                mem_type="episodic",
                content=content.strip(),
                source_conv_id=source_conv_id,
                agent_run_id=agent_run_id,
                embedding=None,  # episodic 不需要 embedding
                importance=importance,
            )
        except Exception as e:
            logger.warning("保存 episodic 记忆失败: %s", e)
            return None

    async def save_semantic(
        self,
        mode: str,
        content: str,
        source_conv_id: str = "",
        agent_run_id: str = "",
        importance: float = 0.6,
    ) -> Optional[str]:
        """保存语义记忆（知识/事实），自动生成 embedding"""
        if not content.strip():
            return None
        embedding = await self.embed(content)
        # 延迟导入避免循环依赖
        import database as db
        try:
            # D 修复（事件循环阻塞）：同步 INSERT 放线程池
            return await asyncio.to_thread(
                db.add_agent_memory,
                mode=mode,
                mem_type="semantic",
                content=content.strip(),
                source_conv_id=source_conv_id,
                agent_run_id=agent_run_id,
                embedding=embedding,
                importance=importance,
            )
        except Exception as e:
            logger.warning("保存 semantic 记忆失败: %s", e)
            return None

    # ── 总结生成（供 AgentEngine 在运行结束后调用） ──

    def build_episodic_summary(
        self,
        task: str,
        tool_calls: list[dict],
        final_content: str,
        iterations: int,
    ) -> str:
        """
        从 Agent 运行轨迹构建情景记忆摘要
        不调 LLM，纯规则提取，保证低成本
        """
        parts = [f"任务: {task.strip()[:200]}"]
        if iterations:
            parts.append(f"迭代次数: {iterations}")
        if tool_calls:
            tool_names = [tc.get("tool", "?") for tc in tool_calls]
            parts.append(f"使用工具: {', '.join(tool_names[:8])}")
        if final_content:
            # 取最终内容的前 300 字作为摘要
            summary = final_content.strip().replace("\n", " ")[:300]
            parts.append(f"结果: {summary}")
        return " | ".join(parts)


# ── 辅助函数 ──


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _row_to_entry(row: dict) -> MemoryEntry:
    """DB row 转 MemoryEntry"""
    embedding = None
    emb_raw = row.get("embedding")
    if emb_raw:
        try:
            embedding = json.loads(emb_raw) if isinstance(emb_raw, str) else emb_raw
        except (json.JSONDecodeError, TypeError):
            embedding = None
    return MemoryEntry(
        id=row.get("id", ""),
        type=row.get("type", "episodic"),
        mode=row.get("mode", ""),
        content=row.get("content", ""),
        source_conv_id=row.get("source_conv_id", "") or "",
        agent_run_id=row.get("agent_run_id", "") or "",
        embedding=embedding,
        importance=float(row.get("importance", 0.5) or 0.5),
        created_at=row.get("created_at", "") or "",
        last_used_at=row.get("last_used_at", "") or "",
        use_count=int(row.get("use_count", 0) or 0),
    )


# ── Episodic 记忆关键词相似度辅助 ──


def _extract_terms(text: str) -> set[str]:
    """从文本中提取关键词集合（用于 episodic 记忆的 Jaccard 相似度）

    策略：
    - 英文：小写化 + 长度 >= 3 的单词
    - 中文：2-gram 切分（如"读取文件" → {"读取", "取文", "文件"}）
    - 过滤通用停用词（如"任务"、"使用"、"结果"等 episodic 摘要模板词）
    """
    if not text:
        return set()

    terms: set[str] = set()
    # 英文单词
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text.lower()):
        if w not in _STOPWORDS_EN:
            terms.add(w)

    # 中文 2-gram
    zh_chars = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(zh_chars) - 1):
        gram = zh_chars[i] + zh_chars[i + 1]
        if gram not in _STOPWORDS_ZH:
            terms.add(gram)

    return terms


# episodic 摘要模板里的通用词（在 build_episodic_summary 中固定出现），过滤掉避免误匹配
_STOPWORDS_ZH = {
    "任务", "迭代", "次数", "使用", "工具", "结果",
}
_STOPWORDS_EN = {
    "the", "and", "for", "with", "that", "this", "from", "have", "was",
    "are", "were", "been", "task", "iteration", "tool", "result", "used",
}


def _jaccard_score(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度变体：交集 / 并集，空集返回 0"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union if union > 0 else 0.0
