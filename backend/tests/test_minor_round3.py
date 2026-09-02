# -*- coding: utf-8 -*-
"""C-review MINOR Round-3 回归（9 项未修复 MINOR 中可单测的部分）。

覆盖：
- MINOR-2  llm_adapter LRU 淘汰不得 aclose 仍有进行中流的客户端
- MINOR-4  上传带上限读取（MAX+1），超限 413
- MINOR-6  memory.retrieve 的 sqlite 调用放线程池
- MINOR-7  chat/stream 把 reasoning 预算传入 safe_max_tokens（thinking 开/关）
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


# ── MINOR-7：reasoning 预算传入 safe_max_tokens ──

def _chat_req(enable_thinking: bool):
    import routes.chat as chat_mod
    return chat_mod.StreamChatRequest(
        message="hi", conversation_id="c1", model_id="",
        enable_thinking=enable_thinking,
    )


def _patch_chat_deps(monkeypatch, captured: dict):
    import routes.chat as chat_mod

    def rec_safe(configured, prompt_text, max_output_tokens=None, reasoning_tokens=None):
        captured["configured"] = configured
        captured["reasoning"] = reasoning_tokens
        return 2048

    monkeypatch.setattr(chat_mod, "safe_max_tokens", rec_safe)
    monkeypatch.setattr(chat_mod.db, "get_default_model",
                        lambda mode: {"id": "m1", "model_id": "m1"})
    monkeypatch.setattr(chat_mod, "db_get_model_config", lambda mid: {
        "id": "m1", "model_id": "m1", "base_url": "http://x", "api_key": "k",
        "max_tokens": 16384, "supports_thinking": True, "reasoning_tokens": 8000,
        "max_output_tokens": None, "temperature": None, "top_p": None,
    })
    monkeypatch.setattr(chat_mod.db, "get_conversation",
                        lambda cid: {"id": cid, "title": "t"})
    monkeypatch.setattr(chat_mod.db, "get_recent_messages", lambda cid, limit=20: [])
    monkeypatch.setattr(chat_mod.db, "get_relevant_memories", lambda mode, limit=5: [])


def test_chat_stream_passes_reasoning_budget_when_thinking(monkeypatch):
    import routes.chat as chat_mod
    captured = {}
    _patch_chat_deps(monkeypatch, captured)
    asyncio.run(chat_mod.stream_chat(_chat_req(enable_thinking=True), None))
    assert captured.get("reasoning") == 8000, "thinking 开启时 reasoning 预算必须传入封顶计算"


def test_chat_stream_no_reasoning_budget_when_off(monkeypatch):
    import routes.chat as chat_mod
    captured = {}
    _patch_chat_deps(monkeypatch, captured)
    asyncio.run(chat_mod.stream_chat(_chat_req(enable_thinking=False), None))
    assert captured.get("reasoning") is None, "thinking 关闭时不得预留推理预算"


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

def test_run_slot_conflict():
    from routes.agent import _acquire_run_slot, _release_run_slot

    async def main():
        assert await _acquire_run_slot("c1", "run1") is None
        assert await _acquire_run_slot("c1", "run2") == "run1", "同会话第二个 run 必须冲突"
        await _release_run_slot("c1", "run1")
        assert await _acquire_run_slot("c1", "run3") is None, "释放后可再占位"
        # 误删保护：占位者是 run3 时，用 run9 释放不得删掉 run3
        await _release_run_slot("c1", "run9")
        assert await _acquire_run_slot("c1", "runX") == "run3"
        await _release_run_slot("c1", "run3")

    asyncio.run(main())


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
