"""P2 记忆向量化：写入 embed、融合召回、三重门控、防复读。

端到端依赖真实 /v1/embeddings（当前 vLLM 未起，挂起验收 recall@3），
这里全部用确定性 fake embed client 离线验证逻辑分支。
"""
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from bot import memory as mem


def _persona(name: str = "晚风") -> dict:
    return db.create_bot_persona(name=name)


def _conv(persona_id: str) -> dict:
    return db.create_bot_conversation(persona_id=persona_id)


class FakeEmbed:
    """确定性 embedding：按字符桶出向量。同串恒同值，不同串大概率不同。"""

    def __init__(self, dim: int = 16, fail: bool = False):
        self.dim = dim
        self.fail = fail
        self.calls: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for ch in text:
            vec[ord(ch) % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]):
        self.calls.append(list(texts))
        if self.fail:
            return None
        return [self._vec(t) for t in texts]

    async def embed_one(self, text: str):
        vecs = await self.embed([text])
        return vecs[0] if vecs else None


def _row(persona_id: str, content: str) -> dict:
    rows = [r for r in mem.list_memories(persona_id) if r["content"] == content]
    assert rows, f"事实未落库: {content}"
    return rows[0]


def _raw(memory_id: str) -> dict:
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT embedding, embedding_src FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


# ── ① importance 按 slot 分化 ──

def test_importance_by_slot(base_db):
    facts = {f.content: f for f in mem.extract_facts("我叫小林，我生日是3月5号，我喜欢辣，周末要看展")}
    assert facts["用户叫小林"].importance == 1.0
    assert facts["用户的生日是3月5号"].importance == 0.9
    assert facts["用户喜欢辣"].importance == 0.6
    assert facts["用户的计划：看展"].importance == 0.4


# ── ② 写入侧 embed ──

@pytest.mark.asyncio
async def test_remember_async_writes_embedding(base_db):
    persona = _persona()
    fake = FakeEmbed()
    written = await mem.remember_async(persona["id"], "c1", "我叫小林", embed_client=fake)
    assert written == 1
    row = _row(persona["id"], "用户叫小林")
    raw = _raw(row["id"])
    assert raw["embedding"] is not None, "embed 可用时应写入向量"
    assert "小林" in (raw["embedding_src"] or "")


@pytest.mark.asyncio
async def test_remember_async_embed_failure_still_writes_fts_only(base_db):
    persona = _persona()
    fake = FakeEmbed(fail=True)
    written = await mem.remember_async(persona["id"], "c1", "我叫小林", embed_client=fake)
    assert written == 1
    row = _row(persona["id"], "用户叫小林")
    assert _raw(row["id"])["embedding"] is None, "embed 失败不得造伪向量"


# ── ③ G1 硬命中（无 embed 也走通：FTS 路）──

@pytest.mark.asyncio
async def test_g1_hard_hit_injects_top_fact(base_db):
    persona = _persona()
    mem.remember(persona["id"], None, "我叫小林")
    contents = await mem.recall(persona["id"], "小林最近怎么样")
    assert "用户叫小林" in contents, "用户提起小林时应注入该事实"


@pytest.mark.asyncio
async def test_g1_exempt_from_repeat_block(base_db):
    persona = _persona()
    conv = _conv(persona["id"])
    mem.remember(persona["id"], None, "我叫小林")
    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
    first = await mem.recall(persona["id"], "小林", conversation_id=conv["id"], now=now)
    assert first == ["用户叫小林"]
    # 立刻再问一次：G1 豁免防复读，仍注入
    second = await mem.recall(persona["id"], "小林", conversation_id=conv["id"], now=now)
    assert second == ["用户叫小林"]


# ── ④ 防复读 7 天（G3 路径受窗口约束）──

@pytest.mark.asyncio
async def test_g3_blocked_within_7_days_then_allowed(base_db, monkeypatch):
    persona = _persona()
    conv = _conv(persona["id"])
    mem.remember(persona["id"], None, "我喜欢辣")
    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
    # 强制 G3 触发（query 不命中任何事实 → 无 G1/G2）；高 novelty 抵消抑制项
    monkeypatch.setattr(mem, "_deterministic_uniform", lambda pid, n: 0.0)
    first = await mem.recall(
        persona["id"], "今天天气怎么样", conversation_id=conv["id"], now=now, novelty=100.0
    )
    assert first == ["用户喜欢辣"]
    # 同轮之后再触发：7 天内被防复读拦住
    second = await mem.recall(
        persona["id"], "晚饭吃什么", conversation_id=conv["id"], now=now, novelty=100.0
    )
    assert second == []
    # 8 天后解禁（抑制项封顶 0.12，novelty=100 时 p=0.06 仍可触发）
    later = now + timedelta(days=8)
    third = await mem.recall(
        persona["id"], "晚饭吃什么", conversation_id=conv["id"], now=later, novelty=100.0
    )
    assert third == ["用户喜欢辣"]


# ── ⑤ G3 确定性：同一天同决策 ──

@pytest.mark.asyncio
async def test_g3_deterministic_same_day(base_db, monkeypatch):
    persona = _persona()
    mem.remember(persona["id"], None, "我喜欢辣")
    # 固定 uniform 值（触发区），同日两次调用 → 决策一致
    monkeypatch.setattr(mem, "_deterministic_uniform", lambda pid, n: 0.05)
    day = datetime(2026, 10, 29, 9, 0, 0, tzinfo=timezone.utc)
    a = await mem.recall(persona["id"], "", now=day)
    day_later_hour = datetime(2026, 10, 29, 21, 0, 0, tzinfo=timezone.utc)
    b = await mem.recall(persona["id"], "", now=day_later_hour)
    assert a == b == ["用户喜欢辣"]
    # 次日种子变化：uniform=0.05 < p(≥0.08) 恒触发，决策仍可复现
    next_day = datetime(2026, 10, 30, 9, 0, 0, tzinfo=timezone.utc)
    c = await mem.recall(persona["id"], "", now=next_day)
    assert c == ["用户喜欢辣"]


# ── ⑥ 注入上限 ──

@pytest.mark.asyncio
async def test_inject_limit_three(base_db, monkeypatch):
    persona = _persona()
    mem.remember(persona["id"], None, "我叫小林，我住在杭州，我喜欢辣，我养了只猫")
    monkeypatch.setattr(mem, "_deterministic_uniform", lambda pid, n: 0.0)
    # query 命中多条（小林/辣），加 G3 钩子 → 上限 3
    contents = await mem.recall(
        persona["id"], "小林 辣 猫 看展", now=datetime.now(timezone.utc)
    )
    assert len(contents) <= 3


# ── ⑦ 空库/无人格的安全边界 ──

@pytest.mark.asyncio
async def test_recall_empty_and_no_persona(base_db):
    persona = _persona()
    assert await mem.recall(persona["id"], "小林") == []
    assert await mem.recall("", "小林") == []
