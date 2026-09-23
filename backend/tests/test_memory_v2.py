"""v2 记忆子系统单测（spec §13，P1+P2+P3 部分）。

覆盖：schema/迁移幂等、scope/kind 过滤、RRF 次序与断层截断、Reconciler 四操作
与审计、job 重试到 failed、修剪/衰减、块精确替换与超限、伪向量不入库、
画像块渲染预算、四个记忆工具端到端。
"""
import asyncio
import json
from pathlib import Path

import pytest

import database as db
from agent.memory import mstore, reconcile, retriever
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
    n = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
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


def test_importance_zero_survives_retrieval(memdb):
    mid = mstore.add_memory(kind="semantic", content="低重要性但确实相关的磁盘惯例",
                            title="低重要性", source="test", importance=0.0)
    hits = _run(retriever.search("磁盘惯例", scopes=["global"], embed_client=None))
    assert [h.memory_id for h in hits] == [mid]
    # v1 的 `row["importance"] or 0.5` 会把 0.0 抬回 0.5；v2 原样透传
    assert hits[0].importance == 0.0


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


def test_update_missing_target_id_degrades_to_add_not_hits_fallback(memdb, monkeypatch):
    """审查修复锁定（CRITICAL）：prompt 明确要求 UPDATE/DELETE 给 target_id；LLM 若违约
    漏给，旧实现回落到相似度首位 hits[0]，会 supersede 一条并非目标的相似记忆。现降级为
    ADD：保留候选增量、绝不动既有行。"""
    # bash 记忆与候选共享关键词，会被 _similar_existing 检索为 hits[0]（旧代码的误伤对象）
    unrelated = mstore.add_memory(kind="semantic", content="用户默认 shell 是 bash", source="test")
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "用户默认 shell 已改为 zsh", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "UPDATE", "target_id": None,
                 "merged_content": "用户默认 shell 是 zsh", "reason": "更新但漏给 target"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    result = _process(FakeEmbed())
    assert mstore.get_memory(unrelated)["status"] == "active"  # 未被误 supersede
    active = {r["content"] for r in mstore.list_memories(status="active")}
    assert "用户默认 shell 已改为 zsh" in active  # 候选降级为独立新增
    assert result["applied"]["ADD"] == 1 and result["applied"].get("UPDATE", 0) == 0


def test_delete_missing_target_id_degrades_to_noop_not_retire(memdb, monkeypatch):
    """DELETE 漏给 target_id 时不作废任何行（降级 NOOP），也不误新增候选。"""
    victim = mstore.add_memory(kind="semantic", content="用户默认 shell 是 bash", source="test")
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"title": "", "content": "用户默认 shell 已改为 zsh", "kind": "semantic", "scope": "global"}]})

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "DELETE", "target_id": None, "reason": "否定但漏给 target"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    result = _process(FakeEmbed())
    assert mstore.get_memory(victim)["status"] == "active"  # 未被误 retire
    assert len(mstore.list_memories()) == 1  # 候选未入库
    assert result["applied"]["NOOP"] == 1


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


def _no_llm_used(monkeypatch):
    async def boom(prompt, block, cfg):
        raise AssertionError("关闭对账 LLM 后不应调用模型")
    monkeypatch.setattr(reconcile, "_call_llm", boom)


def test_reconcile_llm_off_threshold_noop(memdb, monkeypatch):
    """设置关掉对账 LLM：重复候选走纯阈值去重 NOOP，不打模型也不涨行"""
    _no_llm_used(monkeypatch)
    mstore.add_memory(kind="semantic", content="用户负责 cKun 后端", source="test")
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"content": "用户负责 cKun 后端", "kind": "semantic", "scope": "global"}]})

    result = _run(reconcile.process_jobs(
        embed_client=FakeEmbed(), model_cfg={"model_id": "m"}, llm_enabled=False))
    assert result["applied"]["NOOP"] == 1
    assert len(mstore.list_memories()) == 1


def test_reconcile_llm_off_threshold_adds_fresh(memdb, monkeypatch):
    """无对照记忆时阈值路径直接 ADD，保证关掉 LLM 仍能沉淀"""
    _no_llm_used(monkeypatch)
    mstore.enqueue_job(reconcile.JOB_CANDIDATE, {"items": [
        {"content": "全新的一条事实", "kind": "semantic", "scope": "global"}]})

    result = _run(reconcile.process_jobs(
        embed_client=FakeEmbed(), model_cfg={"model_id": "m"}, llm_enabled=False))
    assert result["applied"]["ADD"] == 1
    rows = mstore.list_memories()
    assert len(rows) == 1 and rows[0]["status"] == "active"


def test_reconcile_llm_off_keeps_pending_migration(memdb, monkeypatch):
    """迁移行无 LLM 时按 ADD 语义转正（沿用原 id），不重复入库"""
    _no_llm_used(monkeypatch)
    mid = mstore.add_memory(kind="semantic", content="待确认的旧事实",
                            source="migration", status="pending_reconcile")
    mstore.enqueue_job(reconcile.JOB_BACKFILL, {"ids": [mid]})

    _run(reconcile.process_jobs(
        embed_client=FakeEmbed(), model_cfg={"model_id": "m"}, llm_enabled=False))
    assert mstore.get_memory(mid)["status"] == "active"
    assert len(mstore.list_memories()) == 1


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


# ── P3: 画像块渲染与预算 ──


def _seed_three_blocks():
    mstore.put_block(memory_blocks.USER_PREFERENCES, "用户偏好: 中文回答，禁止 rm -rf", "test")
    mstore.put_block(memory_blocks.AGENT_RULES, "规则: 变更前先备份", "test")
    mstore.put_block(memory_blocks.host_key("s1"), "[目标机环境] 发行版: Ubuntu 22.04", "probe")


def test_render_blocks_order_and_label(memdb):
    _seed_three_blocks()
    text = _run(memory_blocks.render_for_context("s1", 1200))
    assert text.startswith("[画像]")
    assert (text.index(memory_blocks.USER_PREFERENCES)
            < text.index(memory_blocks.AGENT_RULES)
            < text.index(memory_blocks.host_key("s1")))
    # 只注入当前主机的块
    mstore.put_block(memory_blocks.host_key("s2"), "另一台机器", "probe")
    assert "另一台机器" not in _run(memory_blocks.render_for_context("s1", 1200))


def test_render_blocks_budget_drops_whole_block(memdb):
    """超预算按整块丢弃：切半句会反转「禁止 rm -rf」这类否定式规则的语义"""
    from agent.cost_tracker import estimate_tokens

    _seed_three_blocks()
    full = _run(memory_blocks.render_for_context("s1", 1200))
    # 预算 = 前两块的成本之和 → 第三块（主机画像）刚好挤不进
    budget = sum(
        estimate_tokens(f"### {key}\n{mstore.get_block(key)['value']}")
        for key in (memory_blocks.USER_PREFERENCES, memory_blocks.AGENT_RULES)
    )
    tight = _run(memory_blocks.render_for_context("s1", budget_tokens=budget))
    assert memory_blocks.host_key("s1") in full
    assert memory_blocks.host_key("s1") not in tight
    assert "禁止 rm -rf" in tight          # 用户偏好优先保留，且保留整句
    assert "发行版" not in tight
    assert _run(memory_blocks.render_for_context("s1", budget_tokens=1)) == ""


def test_budget_clamps_accounts_to_window():
    """spec §8：设置值是"想要多少"，窗口是"能给多少"，取小者。"""
    from agent.memory.budget import MemoryBudget

    tight = MemoryBudget.for_context(8192, blocks_wanted=8000, injection_wanted=8000)
    assert tight.blocks < tight.window and tight.injection < tight.window
    assert (tight.blocks + tight.injection + tight.summary
            + tight.history + tight.reserve) <= tight.window
    assert tight.reserve >= 1024

    # 窗口足够大时设置值原样通过（小模型才会被夹）
    wide = MemoryBudget.for_context(200000, blocks_wanted=1200, injection_wanted=1500)
    assert (wide.blocks, wide.injection) == (1200, 1500)
    assert MemoryBudget.for_context(None).window == 16384
    assert MemoryBudget.for_context(8192).blocks < wide.blocks


def test_budget_cap_text_tokens_keeps_tail():
    from agent.cost_tracker import estimate_tokens
    from agent.memory.budget import cap_text_tokens

    assert cap_text_tokens("短文本", 300) == "短文本"
    capped = cap_text_tokens("记录" * 4000, 300)
    assert estimate_tokens(capped) <= 360          # marker 的余量在内
    assert capped.endswith("记录记录")              # 保尾：近期内容更有价值
    assert "[早期上下文已折叠]" in capped


def test_bridle_uses_window_derived_summary_account(memdb):
    """bridle 的摘要上限来自统一预算，不再是 max_context_tokens × 3 字符。"""
    from agent.harness.bridle import Bridle

    small = Bridle(max_context_tokens=4096)
    big = Bridle(max_context_tokens=200000)
    assert 0 < small.budget.summary < big.budget.summary
    assert small.budget.summary == max(200, int((4096 - 1024) * 0.25))


def test_context_summary_persisted_on_run(memdb):
    """spec §8：run 内滚动摘要落库，轨迹回放与 episodic job 都还能拿到它。"""
    rid = "r-sum-" + db._new_id()
    conv = db.create_conversation(title="摘要落库", mode="ssh")
    db.create_agent_run(rid, conv["id"], "排查 nginx 502", mode="ssh")
    db.update_agent_run(rid, "completed", context_summary="先确认端口占用，再改回 listen")
    assert db.get_agent_run(rid)["context_summary"] == "先确认端口占用，再改回 listen"

    # 不传该参数 = 不覆写（中间态更新频繁，摘要只在终态写一次）
    db.update_agent_run(rid, "running", iteration=3)
    assert db.get_agent_run(rid)["context_summary"] == "先确认端口占用，再改回 listen"


def test_episodic_prompt_includes_folded_summary():
    from agent.memory.reconcile import format_episodic

    plain = format_episodic(task="部署", tool_calls=[], final_content="ok", iterations=2)
    with_fold = format_episodic(
        task="部署", tool_calls=[], final_content="ok", iterations=2,
        folded_summary="前 12 轮在改 nginx 配置",
    )
    assert "早期过程" not in plain
    assert "早期过程: 前 12 轮在改 nginx 配置" in with_fold


def test_degraded_probe_text_is_not_a_host_fact(memdb):
    from agent import host_profile as hp
    assert not hp.is_known_profile(hp.UNKNOWN_TEXT)
    assert not hp.is_known_profile(hp.NO_SESSION_TEXT)
    assert not hp.is_known_profile("")
    assert hp.is_known_profile("[目标机环境] 发行版: Ubuntu 22.04")


# ── P3: 记忆工具端到端 ──


def _ctx(session_id: str = "s1"):
    from agent.tools import ToolContext
    return ToolContext(ssh_session_id=session_id, mode="ssh",
                       conversation_id="c1", agent_run_id="r1")


def _tool_registry():
    from agent.tools import create_registry_for_mode
    return create_registry_for_mode("ssh")


def test_memory_tools_registered(memdb):
    names = _tool_registry().names()
    assert {"memory_search", "memory_add", "memory_invalidate", "block_update"} <= set(names)


def test_memory_search_tool_hits_seeded_memory(memdb):
    mid = mstore.add_memory(kind="procedural", content="该主机 nginx 日志在 /var/log/nginx/error.log",
                           title="nginx 日志路径", source="test", scope="host:s1")
    reg = _tool_registry()
    res = _run(reg.execute("memory_search", {"query": "nginx 日志"}, _ctx()))
    assert res.success and mid in res.output
    assert "(procedural/host:s1)" in res.output
    # 主机作用域外查不到（A 机经验不当作 B 机事实）
    other = _run(reg.execute("memory_search", {"query": "nginx 日志"}, _ctx("s2")))
    assert mid not in other.output


def test_memory_search_tool_empty_and_bad_args(memdb):
    reg = _tool_registry()
    assert "无相关记忆" in _run(reg.execute("memory_search", {"query": "全新主题"}, _ctx())).output
    assert not _run(reg.execute("memory_search", {"query": "  "}, _ctx())).success


def test_memory_add_tool_goes_through_reconcile(memdb, monkeypatch):
    reg = _tool_registry()
    res = _run(reg.execute("memory_add", {
        "content": "生产库只在维护窗口重启", "kind": "procedural", "title": "维护窗口",
    }, _ctx()))
    assert res.success and res.output.startswith("已提交")
    jobs = mstore.list_jobs()
    assert len(jobs) == 1 and jobs[0]["kind"] == reconcile.JOB_AGENT_ADD
    assert jobs[0]["priority"] == 1          # 用户显式意图排在自动提取之前
    assert not mstore.list_memories()        # 未对账不生效

    async def fake_llm(prompt, block, cfg):
        return [{"index": 0, "op": "ADD", "reason": "用户显式登记"}]
    monkeypatch.setattr(reconcile, "_call_llm", fake_llm)

    _process(FakeEmbed())
    rows = mstore.list_memories()
    assert len(rows) == 1 and rows[0]["source"] == "agent_tool"
    assert rows[0]["kind"] == "procedural"


def test_memory_add_tool_rejects_bad_scope(memdb):
    reg = _tool_registry()
    res = _run(reg.execute("memory_add", {
        "content": "x", "kind": "semantic", "scope": "host:other-session"}, _ctx()))
    assert not res.success and "当前主机" in res.error
    assert not _run(reg.execute("memory_add", {"content": "x", "kind": "episodic"}, _ctx())).success


def test_memory_invalidate_tool_marks_invalidated_with_audit(memdb):
    mid = mstore.add_memory(kind="semantic", content="包管理器是 yum", source="test")
    reg = _tool_registry()
    res = _run(reg.execute("memory_invalidate", {"memory_id": mid, "reason": "实测为 apt"}, _ctx()))
    assert res.success
    assert mstore.get_memory(mid)["status"] == "invalidated"
    audit = mstore.list_audit(mid)
    assert audit and audit[0]["action"] == "DELETE" and "实测为 apt" in audit[0]["reason"]
    # 二次作废与未知 id 都不该成功
    assert not _run(reg.execute("memory_invalidate",
                                {"memory_id": "nope", "reason": "x"}, _ctx())).success


def test_block_update_tool_append_and_target_host_only(memdb):
    reg = _tool_registry()
    res = _run(reg.execute("block_update", {
        "key": memory_blocks.USER_PREFERENCES, "old_string": "", "new_string": "- 回答用中文"}, _ctx()))
    assert res.success
    assert mstore.get_block(memory_blocks.USER_PREFERENCES)["value"] == "- 回答用中文"
    res2 = _run(reg.execute("block_update", {
        "key": "user-preferences", "old_string": "中文", "new_string": "英文"}, _ctx()))
    assert res2.success and "英文" in mstore.get_block(memory_blocks.USER_PREFERENCES)["value"]

    # host: 前缀简写归一化为 host-profile:，且只能改当前主机
    res3 = _run(reg.execute("block_update", {
        "key": "host:s1", "old_string": "", "new_string": "时区 CST"}, _ctx()))
    assert res3.success
    assert mstore.get_block(memory_blocks.host_key("s1"))["value"] == "时区 CST"
    assert not _run(reg.execute("block_update", {
        "key": "host-profile:s2", "old_string": "", "new_string": "x"}, _ctx())).success
    assert not _run(reg.execute("block_update", {
        "key": "system-prompt", "old_string": "", "new_string": "x"}, _ctx())).success


# ── 管理端点：手动新增与块历史（spec §9）──


def test_manual_add_endpoint_writes_pinned_active_memory(memdb):
    """用户在 UI 手敲的记忆：直写 active、source=user_ui、自动 pinned"""
    import routes.agent as agent_routes

    out = agent_routes.create_agent_memory(agent_routes.MemoryCreateRequest(
        kind="semantic", content="生产 nginx 配置在 /etc/nginx/nginx.conf",
        title="nginx 配置路径", scope="global",
    ))
    assert out["created"] and out["id"]
    row = out["memory"]
    assert row["status"] == "active" and row["source"] == "user_ui"
    assert row["pinned"] == 1 and row["has_embedding"] is False
    audit = mstore.list_audit(out["id"])
    assert [a["action"] for a in audit] == ["ADD"] and "user_ui" in audit[0]["reason"]

    # 无向量也要能被关键词通道召回（否则用户写完发现"搜不到"）
    hits = _run(retriever.search("nginx 配置", scopes=["global"], embed_client=None))
    assert out["id"] in [h.memory_id for h in hits]


def test_pinned_manual_memory_survives_pool_trim(memdb):
    """池容量修剪只动 pinned=0：用户手写的条目被静默淘汰是最难排查的丢失"""
    import routes.agent as agent_routes

    out = agent_routes.create_agent_memory(agent_routes.MemoryCreateRequest(content="一条事实"))
    mstore.add_memory(kind="semantic", content="机器沉淀的同类事实", source="test")
    assert mstore.trim_pools(0) == 1
    assert mstore.get_memory(out["id"])["status"] == "active"


def test_manual_add_endpoint_rejects_blank_and_bad_scope(memdb):
    import pytest

    import routes.agent as agent_routes
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        agent_routes.create_agent_memory(agent_routes.MemoryCreateRequest(content="   "))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e2:
        agent_routes.create_agent_memory(agent_routes.MemoryCreateRequest(
            content="一条事实", scope="weird"))
    assert e2.value.status_code == 400
    # 合法前缀放过（host:/run: 与 retriever.context_scopes 的命名一致）
    assert agent_routes.create_agent_memory(agent_routes.MemoryCreateRequest(
        content="主机级事实", scope="host:s1"))["created"]


def test_block_history_rows_carry_value_for_rollback(memdb):
    """UI 的回滚按钮靠历史行里的 value 原样重写，缺它就回滚不了"""
    mstore.put_block(memory_blocks.USER_PREFERENCES, "- 回答用中文", updated_by="user_ui")
    mstore.put_block(memory_blocks.USER_PREFERENCES, "- 回答用英文", updated_by="agent")
    hist = mstore.list_block_history(memory_blocks.USER_PREFERENCES)
    assert [h["version"] for h in hist] == [2, 1]
    assert hist[1]["value"] == "- 回答用中文" and hist[1]["updated_by"] == "user_ui"
    # 回滚 = 用旧值再写一版（历史不删，与"失效不物理删"一致）
    assert mstore.put_block(memory_blocks.USER_PREFERENCES, hist[1]["value"],
                            updated_by="user_ui") == 3
    assert mstore.get_block(memory_blocks.USER_PREFERENCES)["value"] == "- 回答用中文"


def test_retire_returns_false_when_row_already_inactive(memdb):
    """审查修复锁定：retire 的 UPDATE 带 status IN(active,pending_reconcile) 守卫，
    对已失效行命中 0 行时必须返回 False 且不再落 DELETE 审计——否则会污染审计、
    并让 reconcile DELETE 分支误判作废成功而把候选错误转正。"""
    mid = mstore.add_memory(kind="semantic", content="将要作废的事实",
                            scope="global", source="chat_extract", importance=0.6)
    assert mstore.retire(mid, reason="第一次作废") is True
    conn = db._get_conn()
    audit_cnt = lambda: conn.execute(
        "SELECT COUNT(*) FROM memory_audit WHERE memory_id=?", (mid,)).fetchone()[0]
    first_cnt = audit_cnt()
    # 第二次：行已是 invalidated，守卫 UPDATE 命中 0 行
    assert mstore.retire(mid, reason="重复作废") is False
    assert audit_cnt() == first_cnt  # 不得追加审计
