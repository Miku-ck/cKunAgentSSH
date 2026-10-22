"""首启模型种子逻辑测试（seed_models_async / _probe_first_model）

设计契约（原设计文档已退役，口径即下面这几条）：
- 未显式设置 VLLM_BASE_URL → 不种子
- 显式设置 + /models 探测成功 → 种子 data[0].id 为默认模型
- 另设 VLLM_THINKING_URL → 再种子一条思考模型
- 探测失败 → 不种子、不抛异常
- 表非空（已有数据）→ 不探测、不改动
探测链路（netconfig.new_sync_client）全部 mock，不发真实请求。

重构适配：_seed_models(conn) 已重构为异步的 seed_models_async(db_path)
（P2 E9：httpx 同步探测移入线程池，不阻塞事件循环），测试改为
临时文件 DB + asyncio.run（函数自开连接，内存库不可用）。

临时目录使用 conftest 的 tmp_workspace（普通 os.mkdir）— 不能用
tmp_path/tempfile.mkdtemp：Windows 下 mkdtemp 打限制性 ACL，沙箱
进程随后访问会被拒（见 conftest.py 注释）。
"""
import asyncio
import os
import sqlite3

import database as db
import netconfig


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


class FakeSyncClient:
    """netconfig.new_sync_client 的替身（探测已从 httpx.get 改走统一出口）"""

    def __init__(self, fn):
        self._fn = fn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, **_kwargs):
        return self._fn(url)


def patch_probe(monkeypatch, fn):
    monkeypatch.setattr(netconfig, "new_sync_client", lambda **_kw: FakeSyncClient(fn))


_MODELS_DDL = """
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


def make_db_file(tmp_dir: str):
    """建一个只有 models_config 表的临时文件库（seed_models_async 自开连接）"""
    db_file = os.path.join(tmp_dir, "models.db")
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(_MODELS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db_file


def count_models(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM models_config").fetchone()["c"]
    finally:
        conn.close()


def query_models(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM models_config").fetchall()
    finally:
        conn.close()


def set_env(monkeypatch, base_url="", thinking_url=""):
    monkeypatch.setattr("config.settings.vllm_base_url", base_url)
    monkeypatch.setattr("config.settings.vllm_thinking_url", thinking_url)
    monkeypatch.setattr("config.settings.vllm_api_key", "EMPTY")


def test_no_env_no_seed(monkeypatch, tmp_workspace):
    """未显式配置 VLLM_BASE_URL → 不种子任何模型"""
    db_file = make_db_file(tmp_workspace)
    set_env(monkeypatch)
    asyncio.run(db.seed_models_async(str(db_file)))
    assert count_models(db_file) == 0


def test_probe_success_seeds_default(monkeypatch, tmp_workspace):
    """探测成功 → 种子 data[0].id 为默认 + SSH 默认模型"""
    db_file = make_db_file(tmp_workspace)
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResponse(payload={"data": [{"id": "model-a"}, {"id": "model-b"}]})

    patch_probe(monkeypatch, fake_get)
    asyncio.run(db.seed_models_async(str(db_file)))

    rows = query_models(db_file)
    assert len(rows) == 1
    assert rows[0]["model_id"] == "model-a"
    assert rows[0]["is_default"] == 1
    assert rows[0]["is_default_ssh"] == 1
    assert rows[0]["supports_thinking"] == 0
    assert calls == ["http://127.0.0.1:9030/v1/models"]


def test_thinking_url_seeds_second_model(monkeypatch, tmp_workspace):
    """主 + thinking URL → 两条种子，第二条为思考模型且非默认"""
    db_file = make_db_file(tmp_workspace)
    set_env(monkeypatch,
            base_url="http://127.0.0.1:9030/v1",
            thinking_url="http://127.0.0.1:9031/v1")

    def fake_get(url, timeout=None):
        if "9031" in url:
            return FakeResponse(payload={"data": [{"id": "thinking-m"}]})
        return FakeResponse(payload={"data": [{"id": "main-m"}]})

    patch_probe(monkeypatch, fake_get)
    asyncio.run(db.seed_models_async(str(db_file)))

    rows = query_models(db_file)
    assert len(rows) == 2
    assert rows[0]["model_id"] == "main-m"
    assert rows[0]["is_default"] == 1
    assert rows[1]["model_id"] == "thinking-m"
    assert rows[1]["supports_thinking"] == 1
    assert rows[1]["is_default"] == 0


def test_probe_failure_no_seed_no_raise(monkeypatch, tmp_workspace):
    """探测连接失败 → 不种子、不抛异常（静默降级）"""
    db_file = make_db_file(tmp_workspace)
    set_env(monkeypatch, base_url="http://127.0.0.1:1/v1")  # 不可达地址（被 mock 拦截）

    def fake_get(url, timeout=None):
        raise Exception("connection refused")

    patch_probe(monkeypatch, fake_get)
    asyncio.run(db.seed_models_async(str(db_file)))
    assert count_models(db_file) == 0


def test_probe_empty_data_no_seed(monkeypatch, tmp_workspace):
    """/models 返回空 data → 不种子"""
    db_file = make_db_file(tmp_workspace)
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")
    patch_probe(monkeypatch, lambda url: FakeResponse(payload={"data": []}))
    asyncio.run(db.seed_models_async(str(db_file)))
    assert count_models(db_file) == 0


def test_existing_rows_untouched(monkeypatch, tmp_workspace):
    """表非空（旧数据）→ 不探测、不改动"""
    db_file = make_db_file(tmp_workspace)
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "INSERT INTO models_config (id, name, model_id, base_url, created_at, updated_at)"
        " VALUES ('existing', 'x', 'x', 'http://old', '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()
    set_env(monkeypatch, base_url="http://127.0.0.1:9030/v1")

    def fail_get(url, timeout=None):
        raise AssertionError("表非空时不应发起探测")

    patch_probe(monkeypatch, fail_get)
    asyncio.run(db.seed_models_async(str(db_file)))
    assert count_models(db_file) == 1


def test_probe_first_model_direct(monkeypatch):
    """_probe_first_model 边界：URL 已含 /models 不重复拼接；异常返回 None"""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResponse(payload={"data": [{"id": "m1"}]})

    patch_probe(monkeypatch, fake_get)
    assert db._probe_first_model("http://h/v1") == "m1"
    assert calls[-1] == "http://h/v1/models"
    assert db._probe_first_model("http://h/v1/models") == "m1"
    assert calls[-1] == "http://h/v1/models"  # 不重复拼接

    def boom(url, timeout=None):
        raise Exception("boom")

    patch_probe(monkeypatch, boom)
    assert db._probe_first_model("http://h/v1") is None
