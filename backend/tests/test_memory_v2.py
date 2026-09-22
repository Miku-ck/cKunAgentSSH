"""v2 记忆子系统单测（spec §13，P1+P2 部分）。

覆盖：schema/迁移幂等、scope/kind 过滤、RRF 次序与断层截断、Reconciler 四操作
与审计、job 重试到 failed、修剪/衰减、块精确替换与超限、伪向量不入库。
"""
import asyncio
import json
import struct
from pathlib import Path

import pytest

import database as db
from agent.memory import mstore, reconcile, retriever, vec
from agent.memory import blocks as memory_blocks


@pytest.fixture
def memdb():
    """共享单库 + 每测试清表。

    database.py 的连接是线程本地的：init_db 换路径不会让已建立的连接重开，
    每个测试换 tmp 路径会让主线程连到已被 rmtree 的旧文件（Windows 下表现为
    挂起/幽灵数据）。因此本模块所有测试固定同一个库文件，逐测试 DELETE 清表。
    """
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    path = base / "memv2-test.db"
    original = db._db_path
    db.init_db(str(path))
    conn = db._get_conn()
    for t in ("memories", "memory_jobs", "memory_audit", "memory_blocks",
              "memory_block_history", "memory_fts", "agent_memories", "agent_strategies"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.execute("DELETE FROM app_settings WHERE key='memory_v2.migrated'")
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    if original:
        db.init_db(original)


class FakeEmbed:
    """固定向量的 embedding 客户端桩：真实 API 在单测中不可用也不需要。"""
    model = "fake-model"

    def __init__(self, dim=8, seed=1.0):
        self._v = [seed + i * 0.01 for i in range(dim)]

    async def embed_one(self, text):
        return list(self._v)

    async def embed(self, texts):
        return [list(self._v) for _ in texts]


def _run(coro):
    return asyncio.run(coro)


# ── P1: schema + 迁移 ──


def test_v2_tables_created(memdb):
    conn = db._get_conn()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"memories", "memory_jobs", "memory_audit", "memory_blocks",
            "memory_block_history"} <= names
    flag = conn.execute(
        "SELECT value FROM app_settings WHERE key='memory_v2.fts'").fetchone()
    assert flag[0] in ("0", "1")


def test_migration_from_v1(memdb):
    now = db._now()
    conn = db._get_conn()
    api_emb = [0.5] * 64
    for i, (t, src, emb) in enumerate([
        ("semantic", "api:glm", json.dumps(api_emb)),
        ("semantic", "local-256", json.dumps([0.1] * 256)),
        ("episodic", "local-256", json.dumps([0.1] * 256)),
    ]):
        conn.execute(
            "INSERT INTO agent_memories (id,type,mode,content,embedding,"
            "embedding_src,importance,use_count,last_used_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"am-{i}", t, "ssh", f"内容{i}", emb, src, 0.5, i, now, now),
        )
    conn.execute(
        "INSERT INTO agent_strategies (id,mode,strategy_type,name,content,score,"
        "enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("as-0", "ssh", "reflection", "磁盘排查", "先 du 后 df", 0.8, 1, now, now),
    )
    conn.commit()
    # init_db 时存量表为空、迁移已置标记；此处模拟"带旧数据启动"需重开门闸
    conn.execute("DELETE FROM app_settings WHERE key='memory_v2.migrated'")
    db._migrate_db(conn)
    conn.commit()

    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
    assert rows["am-0"]["status"] == "pending_reconcile"
    assert rows["am-0"]["embedding"] is not None  # api 向量搬运为 BLOB
    assert rows["am-0"]["kind"] == "semantic"
    assert rows["am-1"]["embedding"] is None      # 伪向量丢弃
    assert rows["am-2"]["status"] == "active"     # episodic 免对账
    assert rows["mig-as-0"]["kind"] == "procedural"
    jobs = conn.execute(
        "SELECT * FROM memory_jobs WHERE kind='backfill_reconcile'").fetchall()
    assert len(jobs) == 1
    # 两条 semantic 都是 pending_reconcile，一起进回填批（episodic 免对账）
    assert json.loads(jobs[0]["payload"])["ids"] == ["am-0", "am-1"]

    # 幂等：重跑不产生第二份
    db._migrate_db(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert n == 4


def test_migration_idempotent_across_init(memdb):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO agent_memories (id,type,mode,content,importance,use_count,"
        "last_used_at,created_at) VALUES ('x1','semantic','ssh','偏好X',0.5,0,?,?)",
        (db._now(), db._now()))
    conn.commit()
    conn.execute("DELETE FROM app_settings WHERE key='memory_v2.migrated'")
    db._migrate_db(conn)
    conn.commit()
    db._migrate_db(conn)  # 第二次：标记已置，直接跳过
    conn.commit()
    n = conn2_count = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert n == 1


# ── P1: 写入与检索 ──


def test_add_and_fts_search(memdb):
    mid = mstore.add_memory(kind="semantic", content="用户偏好使用 zsh 作为默认 shell",
                            title="shell 偏好", source="test")
    hits = mstore.fts_hits("zsh 偏好", scopes=["global"])
    assert any(h[0] == mid for h in hits)
    lines = _run(retriever.retrieve_for_injection(
        "zsh", scopes=["global"], embed_client=None, max_items=5))
    assert any(mid in ln for ln in lines)
    # 只有真正注入的行计热度
    row = mstore.get_memory(mid)
    assert row["use_count"] == 1


def test_search_rrf_order_and_gap_cut(memdb):
    a = mstore.add_memory(kind="semantic", content="nginx 反向代理配置在 /etc/nginx",
                          title="nginx", source="test")
    b = mstore.add_memory(kind="semantic", content="postgres 数据库端口配置",
                          title="postgres", source="test")
    hits = _run(retriever.search("nginx 反向代理", scopes=["global"], embed_client=None))
    ids = [h.memory_id for h in hits]
    assert ids and ids[0] == a and b not in ids


def test_scope_filter(memdb):
    mstore.add_memory(kind="semantic", content="主机磁盘信息", scope="host:h1", source="test")
    g = mstore.add_memory(kind="semantic", content="全局磁盘惯例", source="test")
    hits = mstore.fts_hits("磁盘", scopes=["global"])
    assert [h[0] for h in hits] == [g]


def test_pseudo_vector_never_stored(memdb):
    # embed 不可用（客户端为 None）→ 无向量入库，但 FTS 仍可检
    mid = mstore.add_memory(kind="semantic", content="无向量也能关键词命中",
                            source="test", embedding=None)
    row = mstore.get_memory(mid)
    assert row["has_embedding"] is False
    assert any(h[0] == mid for h in mstore.fts_hits("关键词命中", scopes=["global"]))


def test_vec_blob_roundtrip_scan(memdb):
    v = [0.1 * i for i in range(8)]
    mid = mstore.add_memory(kind="semantic", content="向量检索目标", source="test",
                            embedding=v, embedding_src="api:fake")
    hits = mstore.vec_search(v, limit=5, scopes=["global"])
    assert hits and hits[0][0] == mid and hits[0][1] > 0.99


# ── P2: Reconciler ──


def _process(embed_client=None, model_cfg=None):
    return _run(reconcile.process_jobs(
        embed_client=embed_client,
        model_cfg=model_cfg or {"base_url": "u", "api_key": "k", "model_id": "m"}))


def test_candidate_add_and_audit(memdb, monkeypatch):
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "t", "content": "用户负责 cKun 后端", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "ADD", "reason": "新事实"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    result = _process(FakeEmbed())
    assert result["processed"] == 1 and result["applied"]["ADD"] == 1
    rows = mstore.list_memories()
    assert rows and rows[0]["status"] == "active" and rows[0]["source"] == "chat_extract"
    audit = mstore.list_audit(rows[0]["id"])
    assert audit[0]["action"] == "ADD"
    # job 已消费
    assert mstore.list_jobs(status="pending") == []


def test_candidate_noop_drops_duplicate(memdb, monkeypatch):
    kept = mstore.add_memory(kind="semantic", content="用户负责 cKun 后端", source="test")
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "用户负责 cKun 后端", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "NOOP", "target_id": kept, "reason": "等价重复"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    result = _process(FakeEmbed())
    assert result["applied"]["NOOP"] == 1
    assert len(mstore.list_memories()) == 1  # 候选未入库


def test_update_supersedes_with_heat_inheritance(memdb, monkeypatch):
    old = mstore.add_memory(kind="semantic", content="用户默认 shell 是 bash", source="test")
    db_conn = db._get_conn()
    db_conn.execute("UPDATE memories SET use_count=5 WHERE id=?", (old,))
    db_conn.commit()
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "用户默认 shell 已改为 zsh", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "UPDATE", "target_id": old,
                 "merged_content": "用户默认 shell 是 zsh", "reason": "更新"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    _process(FakeEmbed())
    rows = {r["content"]: r for r in mstore.list_memories()}
    new = rows["用户默认 shell 是 zsh"]
    assert new["status"] == "active"
    old_row = rows["用户默认 shell 是 bash"]
    assert old_row["status"] == "superseded" and old_row["superseded_by"] == new["id"]
    assert new["use_count"] == 5  # 热度继承


def test_delete_invalidates_not_physical(memdb, monkeypatch):
    target = mstore.add_memory(kind="semantic", content="旧事实已被否定", source="test")
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "X 服务已下线", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "DELETE", "target_id": target, "reason": "过时"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    _process(FakeEmbed())
    row = mstore.get_memory(target)
    assert row["status"] == "invalidated" and row["invalid_at"]
    assert mstore.list_memories(status="invalidated")  # 行仍在，供审计


def test_llm_failure_retries_then_failed(memdb, monkeypatch):
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "候选X", "kind": "semantic", "scope": "global"}]})

    async def broken_llm(prompt, block, cfg):
        raise RuntimeError("llm down")
    monkeypatch.setattr(reconcile, "_call_llm", broken_llm)

    for expected_attempts in (1, 2, 3):
        result = _process(FakeEmbed())
        assert result["failed"] == 1
        job = mstore.list_jobs()[0]
        assert job["attempts"] == expected_attempts
    assert mstore.list_jobs(status="failed")
    # 重投同内容不再产生 active 行（候选未入库）
    assert mstore.list_memories() == []


def test_backfill_pending_promoted(memdb, monkeypatch):
    mid = mstore.add_memory(kind="semantic", content="迁移来的独立事实",
                            source="migration", status="pending_reconcile")
    mstore.enqueue_job(reconcile.JOB_BACKFILL, {"ids": [mid]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "ADD", "reason": "无矛盾，转正"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    _process(FakeEmbed())
    row = mstore.get_memory(mid)
    assert row["status"] == "active"  # 沿用原 id 转正


def test_agent_add_bypasses_when_llm_down(memdb, monkeypatch):
    mstore.enqueue_job(reconcile.JOB_AGENT_ADD, {
        "title": "用户告知", "content": "部署目录是 /srv/app", "kind": "semantic",
        "scope": "global", "importance": 0.7})

    async def none_llm(prompt, block, cfg):
        return None
    monkeypatch.setattr(reconcile, "_call_llm", none_llm)

    _process(FakeEmbed())
    rows = mstore.list_memories()
    assert len(rows) == 1 and rows[0]["source"] == "agent_tool"


# ── P2: tick 维护任务 ──


def test_trim_pools_keeps_max_and_spares_pinned(memdb):
    from agent.consciousness_tick import ConsciousnessTick, TickConfig
    for i in range(5):
        mstore.add_memory(kind="semantic", content=f"记忆{i}", source="test",
                          importance=0.1 * i + 0.1)
    keep = mstore.add_memory(kind="semantic", content="重要且pin", source="test",
                             importance=0.01, pinned=1)
    tick = ConsciousnessTick(TickConfig(max_items=2))
    result = _run(tick._trim_pools())
    assert result["retired"] == 3  # importance 最高 2 条 + pinned 存活
    active_ids = {r["id"] for r in mstore.list_memories(status="active")}
    assert keep in active_ids


def test_decay_and_retire_all(memdb):
    conn = db._get_conn()
    mid = mstore.add_memory(kind="semantic", content="陈旧零命中记忆", source="test",
                            importance=0.25)
    conn.execute("UPDATE memories SET created_at=datetime('now','-400 day'),"
                 " valid_at=datetime('now','-400 day') WHERE id=?", (mid,))
    conn.commit()
    result = mstore.decay_memories()
    assert result["invalidated"] >= 1
    assert mstore.get_memory(mid)["status"] == "invalidated"

    alive = mstore.add_memory(kind="semantic", content="活跃", source="test", pinned=1)
    mstore.add_memory(kind="semantic", content="陪葬", source="test")
    assert mstore.retire_all_unpinned() == 1
    assert mstore.get_memory(alive)["status"] == "active"


def test_blocks_replace_and_limits(memdb):
    mstore.put_block("user-preferences", "用户喜欢简洁回答", "test")
    v2 = mstore.replace_in_block("user-preferences", "简洁回答", "详尽回答", "agent")
    assert mstore.get_block("user-preferences")["value"] == "用户喜欢详尽回答"
    assert v2 == 2
    with pytest.raises(ValueError):  # 出现 0 次
        mstore.replace_in_block("user-preferences", "不存在", "x", "agent")
    with pytest.raises(ValueError):  # 超限
        mstore.put_block("agent-rules", "x" * 2001, "test")
    hist = mstore.list_block_history("user-preferences")
    assert len(hist) == 2
    assert memory_blocks.allowed_key("host-profile:h1")
    assert not memory_blocks.allowed_key("evil-key")


def test_episodic_job_written_without_llm(memdb):
    mstore.enqueue_job(reconcile.JOB_EPISODIC, {"content": "任务: ls | 结果: ok"})
    _run(reconcile.process_jobs(embed_client=FakeEmbed(), model_cfg={}))
    eps = mstore.list_memories(kind="episodic")
    assert len(eps) == 1 and "任务: ls" in eps[0]["content"]
    assert eps[0]["source"] == "run_summary"
