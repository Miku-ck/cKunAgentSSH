# -*- coding: utf-8 -*-
"""C-review MINOR Round-3 回归（9 项未修复 MINOR 中可单测的部分）。

覆盖：
- MINOR-2  llm_adapter LRU 淘汰不得 aclose 仍有进行中流的客户端
- MINOR-4  上传带上限读取（MAX+1），超限 413
- MINOR-6  memory.retrieve 的 sqlite 调用放线程池
- MINOR-10 web_fetch max_chars 上限钳制
- MINOR-12 同会话并发占位（冲突/误删保护）+ 启动对账标记滞留 run

运行: python -m pytest backend/tests/test_minor_round3.py -v
"""
import asyncio
import sqlite3
import sys
import threading

import pytest
import fastapi
from fastapi.testclient import TestClient


# ── 通用 fixture ──

class _NoClose:
    """包装 sqlite 连接：吞掉 database.py 函数 finally 里的 conn.close()"""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ── MINOR-2：LRU 淘汰与 in-flight 保护 ──

def _reset_llm_cache():
    import llm_adapter
    llm_adapter._client_cache.clear()
    llm_adapter._client_in_flight.clear()


def test_lru_eviction_skips_aclose_when_in_flight():
    """淘汰的客户端仍有进行中流 → 跳过 aclose（不掐断其他请求）"""
    import llm_adapter
    _reset_llm_cache()
    try:
        closed = []

        class _FakePool:
            def __init__(self, sink):
                self._sink = sink

            async def aclose(self):
                self._sink.append(True)

        class _FakeClient:
            def __init__(self, sink):
                self._client = _FakePool(sink)

        sinks = {}
        # 填满缓存（max 8），最旧的一个（key0）设为 in-flight
        for i in range(8):
            key = (f"http://host{i}", "k")
            sinks[key] = []
            llm_adapter._client_cache[key] = _FakeClient(sinks[key])
        in_flight_key = (f"http://host{0}", "k")  # 最旧（OrderedDict 头，将被淘汰）
        llm_adapter._client_in_flight[in_flight_key] = 1

        # 新 key 触发淘汰最旧的（in-flight 的那个）
        asyncio.run(llm_adapter._get_client("http://host-new", "k"))

        assert not sinks[in_flight_key], "in-flight 客户端不得被 aclose"
        assert in_flight_key not in llm_adapter._client_in_flight, "计数应被清理"
    finally:
        _reset_llm_cache()


def test_lru_eviction_closes_when_idle():
    """无进行中流的被淘汰客户端正常 aclose（对照组）"""
    import llm_adapter
    _reset_llm_cache()
    try:
        closed = []

        class _FakePool:
            async def aclose(self):
                closed.append(True)

        class _FakeClient:
            def __init__(self):
                self._client = _FakePool()

        for i in range(8):
            llm_adapter._client_cache[(f"http://host{i}", "k")] = _FakeClient()

        asyncio.run(llm_adapter._get_client("http://host-new", "k"))
        assert closed == [True], "idle 客户端应被 aclose"
    finally:
        _reset_llm_cache()


# ── MINOR-4：上传带上限读取 ──

def test_upload_oversized_rejected_413():
    import routes.chat as chat_mod
    app = fastapi.FastAPI()
    app.include_router(chat_mod.router)
    client = TestClient(app)
    # 超过 10MB 上限（MAX+1 读取即应拒绝）
    payload = b"x" * (chat_mod.MAX_UPLOAD_SIZE + 1024)
    resp = client.post(
        "/chat/upload",
        files={"file": ("big.txt", payload, "text/plain")},
        data={"conversation_id": "c1"},
    )
    assert resp.status_code == 413


def test_upload_within_limit_ok(monkeypatch, tmp_workspace):
    from pathlib import Path
    import routes.chat as chat_mod
    # 落盘到 tmp_workspace，避免污染真实 uploads 目录
    monkeypatch.setattr(chat_mod, "_uploads_root", lambda: Path(tmp_workspace))
    app = fastapi.FastAPI()
    app.include_router(chat_mod.router)
    client = TestClient(app)
    payload = b"hello " * 100
    resp = client.post(
        "/chat/upload",
        files={"file": ("ok.txt", payload, "text/plain")},
        data={"conversation_id": "c1"},
    )
    assert resp.status_code == 200
    assert resp.json()["content"].startswith("hello ")


# ── MINOR-6：memory.retrieve 线程池 ──

def test_memory_retrieve_offloaded_to_thread(monkeypatch):
    import agent.memory as mem
    import database as db

    main_tid = threading.get_ident()
    seen_tids = []

    def fake_get_agent_memories(**kw):
        seen_tids.append(threading.get_ident())
        return []

    monkeypatch.setattr(db, "get_agent_memories", fake_get_agent_memories)

    class _Dummy:
        pass

    asyncio.run(mem.MemoryManager.retrieve(_Dummy(), query="x", mode="ssh", top_k=3))
    assert seen_tids, "get_agent_memories 应被调用"
    assert all(t != main_tid for t in seen_tids), "sqlite 调用必须在事件循环线程之外"


# ── MINOR-10：web_fetch max_chars 上限 ──

def test_web_fetch_max_chars_capped(monkeypatch):
    import agent.tools.builtin.web_tools as wt
    from agent.tools import ToolContext

    # 屏蔽 trafilatura（确定性走 _strip_html 路径）
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    monkeypatch.setattr(wt, "_strip_html", lambda h: h)

    tool = wt.WebFetchTool()

    async def _safe(url):
        return (True, "203.0.113.10")

    monkeypatch.setattr(tool, "_check_url_safe", _safe)
    big = "x" * 300_000

    async def fake_get(self, client, direct_url, headers, sni, cap=wt._MAX_RESPONSE_BYTES):
        return 200, "", big

    monkeypatch.setattr(wt.WebFetchTool, "_get_with_limit", fake_get)

    result = asyncio.run(tool.execute(ToolContext(), "http://example.com/page",
                                      max_chars=10 ** 9))
    assert result.success
    assert len(result.output) <= wt._MAX_FETCH_CHARS + 64, (
        "max_chars=10^9 必须被钳制到上限，不得整页灌入上下文"
    )
    assert "已截断" in result.output


# ── MINOR-12：同会话并发占位 + 启动对账 ──

def test_run_slot_conflict(monkeypatch):
    from routes import agent as ra

    # 测试环境无对应 DB 行 — 占位者行缺失不算陈旧（防误接管新建 run），保持冲突
    monkeypatch.setattr(ra.db, "get_agent_run", lambda run_id: None)

    async def main():
        assert await ra._acquire_run_slot("c1", "run1") is None
        assert await ra._acquire_run_slot("c1", "run2") == "run1", "同会话第二个 run 必须冲突"
        await ra._release_run_slot("c1", "run1")
        assert await ra._acquire_run_slot("c1", "run3") is None, "释放后可再占位"
        # 误删保护：占位者是 run3 时，用 run9 释放不得删掉 run3
        await ra._release_run_slot("c1", "run9")
        assert await ra._acquire_run_slot("c1", "runX") == "run3"
        await ra._release_run_slot("c1", "run3")

    asyncio.run(main())


def test_run_slot_stale_takeover(monkeypatch):
    """回归：占位 run 在 DB 中已 completed（finally 被取消打断的残留）→ 新请求接管，
    会话不再被永久锁死（"已有运行中的 Agent 任务"故障）。"""
    from routes import agent as ra

    monkeypatch.setattr(ra.db, "get_agent_run",
                        lambda run_id: {"id": run_id, "status": "completed"} if run_id == "stale1" else None)

    async def main():
        ra._in_flight_runs["c2"] = "stale1"   # 模拟泄漏的占位
        assert await ra._acquire_run_slot("c2", "new1") is None, "终态残留必须被接管"
        assert ra._in_flight_runs.get("c2") == "new1"
        # DB 行仍是 running 的占位者不得被接管
        ra._in_flight_runs["c3"] = "live1"
        monkeypatch.setattr(ra.db, "get_agent_run",
                            lambda run_id: {"id": run_id, "status": "running"} if run_id == "live1" else None)
        assert await ra._acquire_run_slot("c3", "new2") == "live1", "活 run 必须保持冲突"
        ra._in_flight_runs.pop("c3", None)

    asyncio.run(main())


def test_release_run_slot_sync_identity():
    """同步释放：仅删自己的占位；无 await，取消安全。"""
    from routes import agent as ra
    ra._in_flight_runs["c4"] = "mine"
    ra._release_run_slot_sync("c4", "other")     # 身份不符 → 不动
    assert ra._in_flight_runs.get("c4") == "mine"
    ra._release_run_slot_sync("c4", "mine")
    assert ra._in_flight_runs.get("c4") is None


def test_mark_stale_agent_runs_failed(monkeypatch):
    import database as db

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, task TEXT,
            status TEXT DEFAULT 'init', iteration INTEGER DEFAULT 0,
            max_iterations INTEGER DEFAULT 10, state_history TEXT,
            total_tokens INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0.0,
            duration_ms INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
            mode TEXT, model_used TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT)"""
    )
    conn.executemany(
        "INSERT INTO agent_runs (id, conversation_id, status) VALUES (?, ?, ?)",
        [("r1", "c", "running"), ("r2", "c", "init"),
         ("r3", "c", "completed"), ("r4", "c", "failed")],
    )
    conn.commit()
    monkeypatch.setattr(db, "_get_conn", lambda: _NoClose(conn))

    n = db.mark_stale_agent_runs_failed()
    assert n == 2, "running/init 两条应被标记"
    rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM agent_runs")}
    assert rows["r1"] == "failed" and rows["r2"] == "failed"
    assert rows["r3"] == "completed" and rows["r4"] == "failed"
    # 幂等：再跑一次无滞留
    assert db.mark_stale_agent_runs_failed() == 0


def test_try_set_default_conversation_title(monkeypatch):
    """MINOR-1：标题仍为默认值 → 写入；窗口期内用户已改自定义标题 → 不覆盖"""
    import database as db

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, "
        "status TEXT DEFAULT 'completed', mode TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'New Chat')")
    conn.commit()
    monkeypatch.setattr(db, "_get_conn", lambda: _NoClose(conn))

    defaults = tuple({"New Chat", "新对话", "New Conversation", ""})
    assert db.try_set_default_conversation_title("c1", "生成的标题", defaults) is True
    assert conn.execute("SELECT title FROM conversations WHERE id='c1'").fetchone()[0] == "生成的标题"
    # 用户自定义标题后，后处理再写不得覆盖
    conn.execute("UPDATE conversations SET title='用户起的名字' WHERE id='c1'")
    conn.commit()
    assert db.try_set_default_conversation_title("c1", "又生成的标题", defaults) is False
    assert conn.execute("SELECT title FROM conversations WHERE id='c1'").fetchone()[0] == "用户起的名字"


def test_get_recent_messages_rowid_tiebreak(monkeypatch):
    """MINOR-3：同秒消息按 rowid（插入序）排序，乱序喂 LLM 的问题消除"""
    import database as db

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL, reasoning TEXT, "
        "model_used TEXT, tokens_used INTEGER DEFAULT 0, run_id TEXT, "
        "agent_trace TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    # 同一秒（秒级时间戳）连续插入，按"第一条→第二条→第三条"顺序入库。
    # uuid 主键 c/a/b 的字典序与插入序不一致：若误按 uuid 排序，结果会乱序。
    # 期望 get_recent_messages 按 rowid（真实插入序）正序返回。
    conn.executemany(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, 'c1', 'user', ?, '2025-01-01 00:00:00')",
        [("uuid-c", "第一条"), ("uuid-a", "第二条"), ("uuid-b", "第三条")],
    )
    conn.commit()
    monkeypatch.setattr(db, "_get_conn", lambda: _NoClose(conn))

    rows = db.get_recent_messages("c1", limit=10)
    assert [r["content"] for r in rows] == ["第一条", "第二条", "第三条"], \
        "同秒消息必须按插入序（rowid）排序，而非 uuid 字典序"
