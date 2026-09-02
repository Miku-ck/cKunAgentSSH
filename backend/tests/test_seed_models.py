# -*- coding: utf-8 -*-
"""首启模型种子逻辑测试（_seed_models / _probe_first_model）

设计契约见 docs/superpowers/specs/2026-09-02-model-seed-env-driven-design.md：
- 未显式设置 VLLM_BASE_URL → 不种子
- 显式设置 + /models 探测成功 → 种子 data[0].id 为默认模型
- 另设 VLLM_THINKING_URL → 再种子一条思考模型
- 探测失败 → 不种子、不抛异常
- 表非空（已有数据）→ 不探测、不改动
httpx 全部 mock，不发真实请求。
"""
import sqlite3

import pytest

import database as db


class FakeResponse:
    def __init__(self, payload=None, status=200, raise_exc=None):
        self._payload = payload
        self._status = status
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise
        if self._status >= 400:
            raise Exception(f"HTTP {self._status}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def make_empty_conn(tmp_path=None):
    """建一个只有 models_config 表的空库连接（内存库，不落盘）"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE models_config (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model_id TEXT,
            provider TEXT NOT NULL DEFAULT 'custom',
            base_url TEXT NOT NULL DEFAULT '',
            api_key TEXT,
            supports_thinking INTEGER DEFAULT 0,
            supports_image INTEGER DEFAULT 0,
            supports_tool_calling INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0,
            is_default_ssh INTEGER DEFAULT 0,
            temperature REAL DEFAULT 0.7,
            max_tokens INTEGER DEFAULT 16384,
            max_output_tokens INTEGER DEFAULT 4096,
            reasoning_tokens INTEGER DEFAULT 8192,
            top_p REAL DEFAULT 0.9,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def count_models(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM models_config").fetchone()["c"]


def set_env(monkeypatch, base_url="", thinking_url=""):
    monkeypatch.setattr("config.settings.vllm_base_url", base_url)
    monkeypatch.setattr("config.settings.vllm_thinking_url", thinking_url)
    monkeypatch.setattr("config.settings.vllm_api_key", "EMPTY")


def test_no_env_no_seed(monkeypatch):
    """未显式配置 VLLM_BASE_URL → 不种子任何模型"""
    conn = make_empty_conn()
    set_env(monkeypatch)
    db._seed_models(conn)
    assert count_models(conn) == 0


def test_probe_success_seeds_default(monkeypatch):
    """探测成功 → 种子 data[0].id 为默认 + SSH 默认模型"""
    conn = make_empty_conn()
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResponse(payload={"data": [{"id": "model-a"}, {"id": "model-b"}]})

    monkeypatch.setattr(db.httpx, "get", fake_get)
    db._seed_models(conn)

    rows = conn.execute("SELECT * FROM models_config").fetchall()
    assert len(rows) == 1
    assert rows[0]["model_id"] == "model-a"
    assert rows[0]["is_default"] == 1
    assert rows[0]["is_default_ssh"] == 1
    assert rows[0]["supports_thinking"] == 0
    assert calls == ["http://127.0.0.1:9030/v1/models"]


def test_thinking_url_seeds_second_model(monkeypatch):
    """主 + thinking URL → 两条种子，第二条为思考模型且非默认"""
    conn = make_empty_conn()
    set_env(monkeypatch,
            base_url="http://127.0.0.1:9030/v1",
            thinking_url="http://127.0.0.1:9031/v1")

    def fake_get(url, timeout=None):
        if "9031" in url:
            return FakeResponse(payload={"data": [{"id": "thinking-m"}]})
        return FakeResponse(payload={"data": [{"id": "main-m"}]})

    monkeypatch.setattr(db.httpx, "get", fake_get)
    db._seed_models(conn)

    rows = conn.execute("SELECT * FROM models_config ORDER BY sort_order").fetchall()
    assert len(rows) == 2
    assert rows[0]["model_id"] == "main-m"
    assert rows[0]["is_default"] == 1
    assert rows[1]["model_id"] == "thinking-m"
    assert rows[1]["supports_thinking"] == 1
    assert rows[1]["is_default"] == 0


def test_probe_failure_no_seed_no_raise(monkeypatch):
    """探测连接失败 → 不种子、不抛异常（静默降级）"""
    conn = make_empty_conn()
    set_env(monkeypatch, base_url="http://127.0.0.1:1/v1")  # 不可达地址（被 mock 拦截）

    def fake_get(url, timeout=None):
        raise Exception("connection refused")

    monkeypatch.setattr(db.httpx, "get", fake_get)
    db._seed_models(conn)
    assert count_models(conn) == 0


def test_probe_empty_data_no_seed(monkeypatch):
    """/models 返回空 data → 不种子"""
    conn = make_empty_conn()
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")
    monkeypatch.setattr(db.httpx, "get", lambda url, timeout=None: FakeResponse(payload={"data": []}))
    db._seed_models(conn)
    assert count_models(conn) == 0


def test_existing_rows_untouched(monkeypatch):
    """表非空（旧数据）→ 不探测、不改动"""
    conn = make_empty_conn()
    conn.execute(
        "INSERT INTO models_config (id, name, model_id, base_url, created_at, updated_at)"
        " VALUES ('existing', 'x', 'x', 'http://old', '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    conn.commit()
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")

    def fail_get(url, timeout=None):
        raise AssertionError("表非空时不应发起探测")

    monkeypatch.setattr(db.httpx, "get", fail_get)
    db._seed_models(conn)
    assert count_models(conn) == 1


def test_probe_first_model_direct(monkeypatch):
    """_probe_first_model 边界：URL 已含 /models 不重复拼接；异常返回 None"""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResponse(payload={"data": [{"id": "m1"}]})

    monkeypatch.setattr(db.httpx, "get", fake_get)
    assert db._probe_first_model("http://h/v1") == "m1"
    assert calls[-1] == "http://h/v1/models"
    assert db._probe_first_model("http://h/v1/models") == "m1"
    assert calls[-1] == "http://h/v1/models"  # 不重复拼接

    def boom(url, timeout=None):
        raise Exception("boom")

    monkeypatch.setattr(db.httpx, "get", boom)
    assert db._probe_first_model("http://h/v1") is None
