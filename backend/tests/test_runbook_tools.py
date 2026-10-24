"""Runbook 知识库单测（方案 Phase 1）。

覆盖：save→search 往返、kind 隔离（非 runbook 记忆不串台）、importance/source
落库生效、同名重存顶替旧版、scope 守卫与主机隔离、检索故障静默降级、
retriever kind 加权（runbook / inspection_report 同分优先注入）。
"""
import asyncio
from pathlib import Path

import pytest

import database as db
from agent.memory import mstore
from agent.memory import retriever as memory_retriever
from agent.tools.builtin.runbook_tools import (
    RunbookSaveTool,
    RunbookSearchTool,
    _RUNBOOK_IMPORTANCE,
)


@pytest.fixture
def memdb():
    """与 test_memory_v2 同构：共享单库 + 每测试清表（线程本地连接不能换路径）。"""
    base = Path(__file__).resolve().parent / "_tmp"
    base.mkdir(exist_ok=True)
    # 与 test_memory_v2 / test_memory_max_items 共用同一个库文件：
    # database.py 连接是线程本地的，同进程内多个测试模块各开各的库会让
    # 主线程连到旧文件（Windows 下幽灵数据）。
    path = base / "memv2-test.db"
    original = db._db_path
    # 全量回归时前面的模块可能把线程绑定到别的库：close-all 让所有线程
    # 在下一次 _get_conn 时按当前 _db_path 重绑（自定义路径测试的既有约定）
    db._close_all_connections()
    db.init_db(str(path))
    conn = db._get_conn()
    for t in ("memories", "memory_jobs", "memory_audit", "memory_blocks",
              "memory_block_history", "memory_fts"):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    mstore.reset_flags()
    yield
    mstore.reset_flags()
    db._close_all_connections()
    if original:
        db.init_db(original)


class _Ctx:
    """工具上下文桩：仅需 ssh_session_id / agent_run_id 两个属性。"""

    def __init__(self, sid="h1"):
        self.ssh_session_id = sid
        self.conversation_id = ""
        self.agent_run_id = "run-x"


def _run(coro):
    return asyncio.run(coro)


# ── runbook_save / runbook_search ──


def test_save_search_roundtrip(memdb):
    r = _run(RunbookSaveTool().execute(
        _Ctx(), title="nginx 502 排查", content="症状：502。定位：上游超时。修复：调大 proxy_read_timeout。验证：curl 200。",
        tags=["nginx", "502"]))
    assert r.success and "nginx 502 排查" in r.output

    row = mstore.list_memories(kind="runbook", limit=1)[0]
    assert row["kind"] == "runbook" and row["source"] == "agent"
    assert row["importance"] == _RUNBOOK_IMPORTANCE
    assert row["scope"] == "global"           # 默认 global（会话 ID 跨进程回收，见工具注释）
    assert row["agent_run_id"] == "run-x"
    assert "[标签]" in row["content"] and "nginx" in row["content"]

    # 按标签关键词检索命中（标签进正文，FTS 参与）
    s = _run(RunbookSearchTool().execute(_Ctx(), query="nginx 502"))
    assert s.success and row["id"] in s.output and "runbook" in s.output


def test_search_kind_isolation(memdb):
    """memory_search 语义库的零散事实不得串进 runbook 检索结果。"""
    mstore.add_memory(kind="semantic", content="nginx 502 通常是上游挂了",
                      title="事实", source="test")
    rb = mstore.add_memory(kind="runbook", content="[标签] nginx\nnginx 502 修复指南",
                           title="runbook", source="agent", scope="global")
    s = _run(RunbookSearchTool().execute(_Ctx(sid=""), query="nginx 502"))
    ids = [h.memory_id for h in _run(memory_retriever.search(
        "nginx", scopes=["global"], kinds=["runbook", "inspection_report"],
        embed_client=None))]
    assert ids == [rb]
    assert rb in s.output
    # 语义记忆在输出里以 (semantic/...) 形式出现才算串台
    assert "(semantic/" not in s.output


def test_resave_supersedes_old(memdb):
    ctx = _Ctx()
    _run(RunbookSaveTool().execute(ctx, title="磁盘满处理", content="v1：清理日志"))
    old_id = mstore.list_memories(kind="runbook", limit=1)[0]["id"]
    r = _run(RunbookSaveTool().execute(ctx, title="磁盘满处理", content="v2：清理日志并配置轮转"))
    assert r.success and "顶替旧版" in r.output
    rows = mstore.list_memories(kind="runbook", limit=10)
    active = [x for x in rows if x["status"] == "active"]
    assert len(active) == 1 and active[0]["id"] != old_id
    assert "v2" in active[0]["content"]
    old = mstore.get_memory(old_id)
    assert old["status"] == "superseded"


def test_concurrent_same_title_saves_keep_one_active(memdb):
    """并发同名保存收敛到恰好一份 active（竞态回归钉）。

    修复前 add→查旧→顶替三步非事务序列在 asyncio.gather 交错下会互相
    顶替成环：两行双双 superseded、零 active，沉淀凭空丢失。加进程级锁后
    每次保存原子胜出。"""
    async def blast():
        return await asyncio.gather(*[
            RunbookSaveTool().execute(_Ctx(), title="磁盘满处理", content=f"v{i}：清理日志")
            for i in range(8)
        ])

    results = _run(blast())
    assert all(r.success for r in results)
    rows = mstore.list_memories(kind="runbook", status="active", limit=100)
    assert len(rows) == 1
    all_rows = mstore.list_memories(kind="runbook", limit=100)
    assert len(all_rows) == 8
    superseded = [x for x in all_rows if x["status"] == "superseded"]
    assert len(superseded) == 7
    # 顶替链无环：被顶替的行的 superseded_by 必须指向最终 active 行之外不至于自指
    assert all(x["superseded_by"] and x["superseded_by"] != x["id"] for x in superseded)
    assert "磁盘满处理" == rows[0]["title"]


def test_save_scope_guard(memdb):
    bad = _run(RunbookSaveTool().execute(
        _Ctx(), title="t", content="c", scope="host:other-host"))
    assert not bad.success and "作用域" in bad.error
    bad2 = _run(RunbookSaveTool().execute(_Ctx(), title="t", content="", ))
    assert not bad2.success


def test_search_host_isolation_and_silent_degrade(memdb, monkeypatch):
    """A 机沉淀的 host 作用域 runbook 不能被 B 机检索到；检索故障静默降级。"""
    rb = mstore.add_memory(kind="runbook", content="redis 内存打满处理指南",
                           title="redis", source="agent", scope="host:h1")
    s = _run(RunbookSearchTool().execute(_Ctx(sid="h2"), query="redis 内存"))
    assert rb not in (s.output or "")
    s2 = _run(RunbookSearchTool().execute(_Ctx(sid="h1"), query="redis 内存"))
    assert rb in s2.output

    async def boom(*a, **k):
        raise RuntimeError("检索炸了")
    monkeypatch.setattr(memory_retriever, "search", boom)
    r = _run(RunbookSearchTool().execute(_Ctx(), query="redis"))
    assert r.success and "暂不可用" in r.output


# ── retriever kind 加权（Phase 1.2）──


def test_retriever_boosts_runbook_over_semantic(memdb):
    """同为关键词命中时，runbook 与 semantic 的 RRF 名次差被 boost 反超。"""
    mstore.add_memory(kind="semantic", content="redis", title="事实", source="test")
    rb = mstore.add_memory(kind="runbook", content="[标签] redis\nredis 配置修改后需重启并验证",
                           title="runbook", source="agent", scope="global")
    hits = _run(memory_retriever.search("redis", scopes=["global"], embed_client=None))
    assert hits and hits[0].memory_id == rb


def test_retriever_boosts_inspection_report(memdb):
    mstore.add_memory(kind="semantic", content="nginx", title="事实", source="test")
    rep = mstore.add_memory(kind="inspection_report", content="[标签] nginx\nnginx 巡检异常：worker 连接数打满",
                            title="巡检", source="schedule", scope="global")
    hits = _run(memory_retriever.search("nginx", scopes=["global"], embed_client=None))
    assert hits and hits[0].memory_id == rep


def test_boost_does_not_override_much_higher_relevance(memdb):
    """普通记忆显著更相关（断层差距大）时不被 boost 翻盘。"""
    base = mstore.add_memory(kind="semantic", content="redis redis redis redis",
                             title="强命中", source="test")
    rb = mstore.add_memory(kind="runbook", content="[标签] other\n完全无关主题的指南用词另外",
                           title="runbook", source="agent", scope="global")
    hits = _run(memory_retriever.search("redis", scopes=["global"], embed_client=None))
    ids = [h.memory_id for h in hits]
    # 无关 runbook 可能因 fts 词重叠兜底不命中而根本不出现；出现也必须排在强命中之后
    if rb in ids:
        assert ids.index(base) < ids.index(rb)
    else:
        assert base in ids


def test_retrieve_for_injection_prefers_runbook(memdb):
    """注入通道同样吃加权：runbook 与事实同时命中时 runbook 的行排前。"""
    mstore.add_memory(kind="semantic", content="redis", title="事实", source="test")
    rb = mstore.add_memory(kind="runbook", content="[标签] redis\nredis 慢查询定位与修复指南",
                           title="runbook", source="agent", scope="global")
    lines = _run(memory_retriever.retrieve_for_injection(
        "redis", scopes=["global"], embed_client=None, max_items=5))
    assert lines and rb in lines[0]
