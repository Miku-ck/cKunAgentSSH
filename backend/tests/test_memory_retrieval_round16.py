"""记忆子系统修复回归（P1 兜底 / P2 双阈值 / P3 自增强+池 / P4 向量来源 / 去重防护）。

P1：兜底原为「三层检索为空即注入最近 5 条 semantic」。空结果最常见的原因
恰恰是相关性判定全部低于 threshold（= 库里没有相关记忆），此时兜底等于
绕开 threshold 注入最不相关的近期记忆——意图跳过修复想避免的污染场景复活。
改为仅检索故障（超时/异常）时兜底。

P2：单一 threshold 同时卡 episodic 的 Jaccard 分与 semantic 的 cosine 分。
episodic 摘要是数百字长模板句、词集大，与短 query 的 Jaccard 极易低于
主调用点传入的 0.3，episodic 层实际不可达。拆为 threshold（semantic）与
episodic_threshold 两个参数。

P3：检索命中 touch 会刷 last_used_at，而候选排序/prune 都按它 —— 自增强
回路；且 prune 保留上限 max_items（可到 500）与候选池（恒 100）错位，
池外记忆不可检索也剪不掉。touch 改为只加计数；池改为读 max_items。

P4：换 embedding 后端后旧新向量同维不可比，余弦失真且去重 fail-open。
semantic 行落 embedding_src 指纹，检索/去重只比同来源；历史 NULL 行维持
维度校验的旧行为。
"""
import asyncio
import os

import pytest

import agent.memory as mem
import database as db
import routes.agent as agent_routes


@pytest.fixture(autouse=True)
def _stub_memory_settings(monkeypatch):
    """retrieve 会读 max_items 定候选池（P3）；单测不碰真实 app_settings。"""
    monkeypatch.setattr(db, "get_memory_settings", lambda: {"max_items": 100, "enabled": True})


# ── P2：双阈值拆分 ──

_QUERY = "查看 nginx"
_EPISODIC_CONTENT = "任务: 查看 nginx 日志 | 结果: 已查看 nginx 日志完成"


def _score_of_content_in_range():
    q = mem._extract_terms(_QUERY)
    c = mem._extract_terms(_EPISODIC_CONTENT)
    return mem._jaccard_score(q, c)


def _episodic_rows():
    return [{
        "id": "m1", "type": "episodic", "mode": "ssh",
        "content": _EPISODIC_CONTENT, "embedding": None,
    }]


@pytest.mark.asyncio
async def test_episodic_scored_with_its_own_threshold(monkeypatch):
    """Jaccard 分低于 semantic threshold 但高于 episodic 默认值的记忆应能召回。"""
    score = _score_of_content_in_range()
    assert score < 0.3, f"用例前提：episodic 分应低于 semantic threshold，实际 {score}"
    assert score >= 0.15, f"用例前提：episodic 分应高于 episodic 默认阈值，实际 {score}"

    def fake_get(mode=None, mem_type=None, limit=100):
        return _episodic_rows() if mem_type == "episodic" else []

    monkeypatch.setattr(db, "get_agent_memories", fake_get)
    monkeypatch.setattr(db, "touch_agent_memory", lambda mid: None)

    results = await mem.MemoryManager().retrieve(
        query=_QUERY, mode="ssh", top_k=5, threshold=0.3,
    )
    assert [r.memory.id for r in results] == ["m1"]

    # 显式抬高 episodic_threshold 后同一条应被挡住（证明两参数各管各层）
    results = await mem.MemoryManager().retrieve(
        query=_QUERY, mode="ssh", top_k=5, threshold=0.3,
        episodic_threshold=score + 0.01,
    )
    assert results == []


# ── P1：兜底仅故障触发 ──


class _FakeMgr:
    def __init__(self, results=None, error=None, delay=0.0):
        self._results = results or []
        self._error = error
        self._delay = delay

    async def retrieve(self, query, mode, top_k=5, threshold=0.3, **kw):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._results


def _mk_result(mid, content):
    entry = mem.MemoryEntry(id=mid, type="semantic", mode="ssh", content=content)
    return mem.MemoryQueryResult(memory=entry, score=0.9)


@pytest.fixture
def fallback_spy(monkeypatch):
    calls = []

    def fake_fallback(mode, limit=5):
        calls.append((mode, limit))
        return [{"content": "fb1"}]

    monkeypatch.setattr(db, "get_relevant_memories", fake_fallback)
    return calls


@pytest.mark.asyncio
async def test_empty_result_does_not_trigger_fallback(fallback_spy):
    """检索正常但零命中 = 合法结论「无相关记忆」，不得兜底注入。"""
    contents = await agent_routes._retrieve_memory_contents(
        _FakeMgr(results=[]), query="你是什么模型", mode="ssh",
    )
    assert contents == []
    assert fallback_spy == [], "空结果不应触发兜底"


@pytest.mark.asyncio
async def test_hit_result_used_directly(fallback_spy):
    contents = await agent_routes._retrieve_memory_contents(
        _FakeMgr(results=[_mk_result("m9", "相关记忆")]), query="q", mode="ssh",
    )
    assert contents == ["相关记忆"]
    assert fallback_spy == []


@pytest.mark.asyncio
async def test_retrieve_exception_triggers_fallback(fallback_spy):
    """仅检索故障时按 recency 降级。"""
    contents = await agent_routes._retrieve_memory_contents(
        _FakeMgr(error=RuntimeError("db down")), query="q", mode="ssh",
    )
    assert contents == ["fb1"]
    assert fallback_spy == [("ssh", 5)]


@pytest.mark.asyncio
async def test_retrieve_timeout_triggers_fallback(fallback_spy, monkeypatch):
    monkeypatch.setattr(agent_routes, "_RETRIEVE_TIMEOUT_SECONDS", 0.05)
    contents = await agent_routes._retrieve_memory_contents(
        _FakeMgr(delay=1.0), query="q", mode="ssh",
    )
    assert contents == ["fb1"]


# ── P3：touch 去自增强 + 候选池与 max_items 同源 ──


def test_touch_bumps_use_count_without_moving_last_used_at(tmp_workspace):
    """last_used_at 现语义为写入时间：检索命中只加计数，不得再刷新它。

    旧行为下「被检索 → last_used_at 前移 → 更易进候选池且更晚被 prune」构成
    自增强回路；池外冷记忆既召回不到也永远剪不掉。
    """
    db_path = os.path.join(tmp_workspace, "touch.db")
    db._close_all_connections()
    db.init_db(db_path)
    try:
        mid = db.add_agent_memory(mode="ssh", mem_type="semantic", content="c1")
        before = db.get_agent_memories(mode="ssh")[0]
        db.touch_agent_memory(mid)
        db.touch_agent_memory(mid)
        after = db.get_agent_memories(mode="ssh")[0]
        assert after["use_count"] == before["use_count"] + 2
        assert after["last_used_at"] == before["last_used_at"], \
            "touch 不得更新 last_used_at（排序键的新近度必须只由写入决定）"
    finally:
        db._close_all_connections()


@pytest.mark.asyncio
async def test_semantic_pool_follows_max_items(monkeypatch):
    seen_limits = {}

    def fake_get(mode=None, mem_type=None, limit=100):
        seen_limits[mem_type] = limit
        return []

    monkeypatch.setattr(db, "get_agent_memories", fake_get)
    monkeypatch.setattr(
        db, "get_memory_settings", lambda: {"max_items": 500, "enabled": True}
    )
    await mem.MemoryManager().retrieve(query="q", mode="ssh", top_k=5)
    assert seen_limits["semantic"] == 500

    # 设置读取失败 → 回退常量池
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_memory_settings", boom)
    seen_limits.clear()
    monkeypatch.setattr(db, "get_agent_memories", fake_get)
    await mem.MemoryManager().retrieve(query="q", mode="ssh", top_k=5)
    assert seen_limits["semantic"] == mem._SEMANTIC_CANDIDATE_CAP


# ── P4：embedding 来源指纹 ──


def test_embedding_src_column_migrated_on_old_db(tmp_workspace):
    """旧库（无 embedding_src 列）经 init_db 迁移后列可读。"""
    import json
    import sqlite3

    p = os.path.join(tmp_workspace, "old.db")
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE agent_memories ("
        " id TEXT PRIMARY KEY, type TEXT NOT NULL, mode TEXT NOT NULL,"
        " content TEXT NOT NULL, source_conv_id TEXT, agent_run_id TEXT,"
        " embedding TEXT, importance REAL DEFAULT 0.5,"
        " last_used_at TEXT NOT NULL, use_count INTEGER DEFAULT 0,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO agent_memories (id, type, mode, content, embedding, last_used_at, created_at)"
        " VALUES ('x', 'semantic', 'ssh', '旧记忆', ?, 'now', 'now')",
        (json.dumps([1.0, 0.0]),),
    )
    conn.commit()
    conn.close()

    db._close_all_connections()
    db.init_db(p)
    try:
        rows = db.get_agent_memories(mode="ssh")
        assert len(rows) == 1
        assert rows[0]["embedding_src"] is None, "存量行来源未知，保持 NULL"
    finally:
        db._close_all_connections()


@pytest.mark.asyncio
async def test_save_semantic_stores_matching_src_fingerprint(tmp_workspace):
    """未配置 API 的 manager 落的是伪向量，指纹必须是 local 而非配置的模型名。"""
    db_path = os.path.join(tmp_workspace, "src.db")
    db._close_all_connections()
    db.init_db(db_path)
    try:
        mgr = mem.MemoryManager()  # base_url/embedding_model 均为空 → 伪 embedding
        await mgr.save_semantic(mode="ssh", content="服务器 SSH 端口是 2222")
        row = db.get_agent_memories(mode="ssh", mem_type="semantic")[0]
        assert row["embedding_src"] == mem._FALLBACK_SRC
    finally:
        db._close_all_connections()


@pytest.mark.asyncio
async def test_retrieve_skips_vectors_from_other_source(monkeypatch):
    """同维不同源的向量不再参与余弦比对；NULL 来源的存量行维持仅维度校验。"""
    import json

    mgr = mem.MemoryManager()
    probe_text = "端口 2222"
    vec, vec_src = await mgr.embed_with_src(probe_text)
    assert vec_src == mem._FALLBACK_SRC

    def _row(rid, emb, src):
        return {
            "id": rid, "type": "semantic", "mode": "ssh", "content": f"c-{rid}",
            "embedding": json.dumps(emb), "embedding_src": src,
        }

    rows = [
        _row("same-src", vec, mem._FALLBACK_SRC),
        _row("other-src", vec, "api:some-other-model"),  # 同向量但异源 → 必须跳过
        _row("legacy-null-src", vec, None),              # 存量未知来源 → 只查维度
    ]

    def fake_get(mode=None, mem_type=None, limit=100):
        return rows if mem_type == "semantic" else []

    monkeypatch.setattr(db, "get_agent_memories", fake_get)
    monkeypatch.setattr(db, "touch_agent_memory", lambda mid: None)

    # query 用与 same-src 相同的文本 → cosine=1.0；threshold 卡不住同源命中
    results = await mgr.retrieve(
        query=probe_text, mode="ssh", top_k=5, threshold=0.5,
    )
    ids = {r.memory.id for r in results}
    assert ids == {"same-src", "legacy-null-src"}
    assert "other-src" not in ids


# ── P2 连带：semantic 去重不得被 episodic 层误伤 ──


@pytest.mark.asyncio
async def test_dedup_only_compares_semantic_layer():
    import post_processor as pp

    seen_kwargs = {}

    class _Mgr:
        def __init__(self, hit_type):
            self._hit_type = hit_type

        async def retrieve(self, query, mode, top_k, threshold, **kw):
            seen_kwargs.update(kw)
            entry = mem.MemoryEntry(id="h", type=self._hit_type, mode="ssh", content="c")
            return [mem.MemoryQueryResult(memory=entry, score=threshold)]

    # episodic 高分命中不算"重复"（旧实现会把新记忆吞掉）
    assert await pp._is_memory_duplicate(_Mgr("episodic"), "x", "ssh", 0.85) is False
    assert seen_kwargs.get("episodic_threshold") == 1.1, "去重必须关掉 episodic 层"
    # semantic 命中才算重复
    assert await pp._is_memory_duplicate(_Mgr("semantic"), "x", "ssh", 0.85) is True


# ── P5：MemoryManager 缓存为真 LRU（命中移动键序）──


def test_memory_mgr_cache_is_true_lru():
    mem.invalidate_memory_manager_cache()
    try:
        keys = [("u", "a", f"m{i}") for i in range(mem._MEMORY_MGR_CACHE_MAX)]
        for k in keys:
            mem.get_memory_manager(base_url=k[0], api_key=k[1], embedding_model=k[2])
        # 命中最早的 m0 → 它变成最近使用
        mem.get_memory_manager(base_url="u", api_key="a", embedding_model="m0")
        mem.get_memory_manager(base_url="u", api_key="a", embedding_model="m9")
        cache = mem._memory_mgr_cache
        assert ("u", "a", "m0") in cache, "真 LRU 下最近命中的条目不应被淘汰"
        assert ("u", "a", "m1") not in cache, "应淘汰最久未使用（m1）而非最早插入"
    finally:
        mem.invalidate_memory_manager_cache()
