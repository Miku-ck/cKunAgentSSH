"""S10-09 / S10-10 / S10-03 回归 — 首启模型种子探测的鉴权、超时预算与凭据脱敏。

三条不变量：
1. 探测必须带 `Authorization`（否则开了 --api-key 的端点回 401 ⇒ 静默永不种子）。
2. 探测预算是请求级的 3s，不被用户的 `http.timeout_seconds` 替换（否则启动关键
   路径被一个为「一次生成的间隔」而设计的设置接管，最坏两条共 1200s）。
3. base_url 内嵌的凭据不出现在日志、也不出现在回传渲染层的文本里。
"""
import pytest

import netconfig


# ── redact_urls_in_text：纯函数，真断言 ──────────────────────────────────────

def test_redact_urls_in_text_strips_userinfo():
    assert netconfig.redact_urls_in_text(
        "Connecting to https://user:token@gw.example/v1/models failed"
    ) == "Connecting to https://***@gw.example/v1/models failed"


def test_redact_urls_in_text_keeps_credential_free_text_untouched():
    for text in (
        "HTTP 401: unauthorized",
        "https://gw.example/v1/models",
        "读取超时",
        "",
        None,
    ):
        assert netconfig.redact_urls_in_text(text) == (text or "")


def test_redact_urls_in_text_handles_percent_and_username_only():
    # 口令里 percent-encode 的 @（p%40ss）不能被误判成分隔符
    assert netconfig.redact_urls_in_text("http://u:p%40ss@host/x") == "http://***@host/x"
    # 只有用户名没有口令，同样是凭据
    assert netconfig.redact_urls_in_text("socks5://alice@proxy:1080") == "socks5://***@proxy:1080"
    # 多个 URL 各打各的
    out = netconfig.redact_urls_in_text("a https://k:v@x/y b http://m@z w")
    assert out == "a https://***@x/y b http://***@z w"
    # 邮箱不是 URL（没有 scheme:// 前缀）
    assert netconfig.redact_urls_in_text("mail me@example.com") == "mail me@example.com"


def test_redact_urls_in_text_never_leaves_password_behind():
    out = netconfig.redact_urls_in_text("see https://user:sup3rs3cr3t@gw/v1 for details")
    assert "sup3rs3cr3t" not in out
    assert "user" not in out.split("@")[0].split("//")[-1]


# ── 种子探测：鉴权 + 请求级超时 ──────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingClient:
    def __init__(self, sink, payload):
        self.sink = sink
        self.payload = payload

    def get(self, url, **kwargs):
        self.sink.append({"url": url, **kwargs})
        return _FakeResp(self.payload)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def probe_env(monkeypatch):
    """拦截 netconfig.new_sync_client，把每次 get 的参数记下来。"""
    calls = []
    clients = []
    payload = {"data": [{"id": "Qwen3-32B"}]}

    def _factory(**kwargs):
        c = _RecordingClient(calls, payload)
        clients.append((c, kwargs))
        return c

    monkeypatch.setattr(netconfig, "new_sync_client", _factory)
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    netconfig._policy = {"proxy_url": "", "timeout_seconds": 0, "retry_count": 1}
    netconfig._loaded = True
    yield calls, clients
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


def test_probe_sends_authorization(probe_env):
    """S10-10：缺 header 时对开鉴权的端点必然 401 ⇒ 自动种子静默永久失效。"""
    import database as db
    calls, _ = probe_env
    assert db._probe_first_model("http://vllm.local:8000/v1", "sk-abc123") == "Qwen3-32B"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-abc123"


def test_probe_sends_bearer_empty_for_local_endpoint(probe_env):
    """本地 vLLM 无密钥时 "EMPTY" 是合法值（与 llm_adapter 的 `api_key or "EMPTY"` 同源）。"""
    import database as db
    calls, _ = probe_env
    db._probe_first_model("http://vllm.local:8000/v1", "")
    db._probe_first_model("http://vllm.local:8000/v1")
    assert calls[0]["headers"]["Authorization"] == "Bearer EMPTY"
    assert calls[1]["headers"]["Authorization"] == "Bearer EMPTY"


def test_probe_timeout_is_request_level_not_client_default(probe_env):
    """S10-09：用户 http.timeout_seconds 不得接管探测预算。

    旧写法把 3.0 放在 default_timeout 位置 —— netconfig.http_timeout 的语义是
    「用户设了就用它替换调用点默认」且 read 另抬到 60s 下限，于是 3s 预算被整个丢弃。
    """
    import database as db
    calls, clients = probe_env
    netconfig.set_policy("", 600, 1)
    assert db._probe_first_model("http://vllm.local:8000/v1", "k") == "Qwen3-32B"
    # 请求级 timeout 恒为探测预算本身
    assert calls[0]["timeout"] == 3.0
    # client 默认超时即便被用户值接管也无所谓：per-request 优先。关键是不再把
    # 3.0 交出去当 default_timeout（那才会被替换）
    assert clients[0][1] == {}, "new_sync_client 不应收到 default_timeout"


def test_probe_url_normalized(probe_env):
    import database as db
    calls, _ = probe_env
    db._probe_first_model("http://vllm.local:8000/v1/", "k")
    assert calls[0]["url"] == "http://vllm.local:8000/v1/models"
    db._probe_first_model("http://vllm.local:8000/v1/models", "k")
    assert calls[1]["url"] == "http://vllm.local:8000/v1/models"


def test_probe_failures_degrade_to_none_and_redact_log(probe_env, monkeypatch, caplog):
    """探测失败必须返回 None（绝不阻塞启动），且日志不得带出 base_url 里的凭据。"""
    import database as db

    def _boom(**kwargs):
        raise RuntimeError(
            "ConnectError: connecting to https://user:s3cr3t@gw.example/v1/models")

    monkeypatch.setattr(netconfig, "new_sync_client", _boom)
    with caplog.at_level("WARNING", logger="database"):
        assert db._probe_first_model("https://user:s3cr3t@gw.example/v1", "k") is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t" not in joined
    assert "user" not in joined.split("@")[0].split("//")[-1]


def test_seed_passes_api_key_to_probe(monkeypatch, tmp_path):
    """S10-10 的调用侧一半：seed_models_async 读出了 key 却只用于写库 = 等于没读。"""
    import asyncio

    import config
    import database as db

    seen = []

    def _fake_probe(base_url, api_key="EMPTY", timeout=3.0):
        seen.append((base_url, api_key))
        return None  # 本例只关心调用侧有没有把 key 传下去

    path = str(tmp_path / "seed.db")
    db._close_all_connections()
    db.init_db(path)
    db._close_all_connections()

    monkeypatch.setattr(db, "_probe_first_model", _fake_probe)
    monkeypatch.setattr(config.settings, "vllm_base_url", "http://a.example/v1", raising=False)
    monkeypatch.setattr(config.settings, "vllm_thinking_url", "http://b.example/v1",
                        raising=False)
    monkeypatch.setattr(config.settings, "vllm_api_key", "sk-real", raising=False)

    asyncio.run(db.seed_models_async(path))
    try:
        assert seen == [("http://a.example/v1", "sk-real"), ("http://b.example/v1", "sk-real")]
    finally:
        db._close_all_connections()


# ── S10-08：name/model_id 互换修正必须是一次性迁移 ────────────────────────────

def _row(path, mid):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT name, model_id FROM models_config WHERE id=?", (mid,)
        ).fetchone()
    finally:
        conn.close()


def _put(path, mid, name, model_id):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        # base_url/provider/created_at/updated_at 在 CREATE TABLE 里是
        # NOT NULL 且无默认（补列循环的 DEFAULT 只在 ALTER TABLE 路径生效）
        conn.execute(
            "INSERT OR REPLACE INTO models_config"
            "(id,name,model_id,base_url,provider,created_at,updated_at) "
            "VALUES(?,?,?,'http://x','custom','2026-01-01 00:00:00','2026-01-01 00:00:00')",
            (mid, name, model_id),
        )
        conn.commit()
    finally:
        conn.close()


def _flag(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        r = conn.execute(
            "SELECT value FROM app_settings WHERE key='models.name_id_fixed'"
        ).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def test_display_name_with_slash_survives_restart(tmp_path):
    """S10-08：用户起的带斜杠显示名，第二次启动不再被当成 model_id 翻走。

    原实现每次 init_db 无条件跑形状猜测，"Qwen/本地最优" 会被与 model_id 互换 ——
    互换后真正发往服务端的 model 字段变成那句中文显示名，直接 4xx。
    """
    import database as db
    path = str(tmp_path / "m.db")
    db._close_all_connections()
    db.init_db(path)
    assert _flag(path) == "1", "首次 init_db 应置上一次闸门"

    _put(path, "m1", "Qwen/本地最优", "qwen3-32b")
    db._close_all_connections()
    db.init_db(path)          # 第二次启动
    assert _row(path, "m1") == ("Qwen/本地最优", "qwen3-32b")


def test_legacy_swapped_rows_still_repaired_exactly_once(tmp_path):
    """闸门不是把修复功能删掉：旧库的反向数据仍修一次，但只修一次。"""
    import sqlite3
    import database as db
    path = str(tmp_path / "m.db")
    db._close_all_connections()
    db.init_db(path)
    # 模拟引入闸门之前就存在的老库：没有标记，且 name/model_id 确实写反了
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM app_settings WHERE key='models.name_id_fixed'")
    conn.commit()
    conn.close()
    _put(path, "m2", "Qwen/Qwen3-32B", "通义千问32B")

    db._close_all_connections()
    db.init_db(path)
    assert _row(path, "m2") == ("通义千问32B", "Qwen/Qwen3-32B"), "一次性修复应生效"

    db._close_all_connections()
    db.init_db(path)
    assert _row(path, "m2") == ("通义千问32B", "Qwen/Qwen3-32B"), "第二次启动不得再动"
    db._close_all_connections()
