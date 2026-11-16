"""非 Windows keyring 凭据信封 — db/_secrets.py 系统凭据库路径的回归测试。

守护点：
- 非 Windows + keyring 可用：protect_secret 产出 `keyring:v1:` 句柄信封，
  明文只进系统凭据库（keyring），DB 行不含明文；读取侧经句柄还原恒等；
- fail-closed：keyring 缺失/不可用时保存显式抛 SecretStorageError，
  绝不静默降级成明文落库（DB 行不存在）；
- 诚实读取：keyring 不可用/条目缺失 → 空串 + 留痕，绝不把句柄当 key 回给调用方；
- 旧值兼容：无前缀旧明文行与 dpapi 信封行读取行为不变；
- 双份实现 parity：database.py 与 db/_secrets.py 的凭据信封函数 AST 一致
  （迁移过渡期副本纪律，同 test_db_schedules_parity 口径）。

运行: python -m pytest backend/tests/test_secret_keyring_envelope.py -v
"""
import ast
import inspect
import sys

import pytest

import database as db
import db._secrets as secrets_mod

KEY = "sk-keyringtest-1234567890abcdef"


def _fresh_db(tmp_workspace):
    """每个用例一份干净库（连接池先清空，路径才切换得掉）。"""
    db._close_all_connections()
    db.init_db(tmp_workspace + "/keyring-envelope.sqlite")
    return db


def _fake_keyring(monkeypatch, *, fail_import=False):
    """可记录写入的假 keyring（不依赖真实系统凭据库）。"""
    store: dict[str, str] = {}
    written: list[tuple[str, str]] = []

    def fake_import():
        if fail_import:
            raise ImportError("No module named 'keyring'")
        return object(), type("_E", (), {"NoKeyringError": RuntimeError})

    def fake_set(username, value):
        if fail_import:
            raise db.SecretStorageError("keyring not installed")
        written.append((username, value))
        store[username] = value

    def fake_get(username):
        return store.get(username)

    monkeypatch.setattr(db, "_keyring_import", fake_import)
    monkeypatch.setattr(db, "_keyring_set", fake_set)
    monkeypatch.setattr(db, "_keyring_get", fake_get)
    monkeypatch.setattr(db, "_keyring_delete", lambda username: store.pop(username, None))
    return store, written


def _as_non_windows(monkeypatch):
    monkeypatch.setattr(db, "_SECRET_PROTECTION_PLATFORM", "linux")


# ── 写侧：非 Windows + keyring 可用 → 句柄信封，明文不落库 ────────────────────

def test_protect_on_non_windows_stores_plaintext_in_keyring_only(tmp_workspace, monkeypatch):
    _fresh_db(tmp_workspace)
    _as_non_windows(monkeypatch)
    store, written = _fake_keyring(monkeypatch)

    sealed = db.protect_secret(KEY)
    assert sealed.startswith(db._KEYRING_ENVELOPE_PREFIX)
    assert KEY not in sealed
    # 明文只进了系统凭据库（service/username 契约），且不在 DB 行值里
    assert written and written[0][1] == KEY
    assert written[0][0].startswith(db._KEYRING_USERNAME_PREFIX)
    handle = sealed[len(db._KEYRING_ENVELOPE_PREFIX):]
    assert db._KEYRING_USERNAME_PREFIX + handle in store

    cfg = db.create_model_config(name="m", base_url="http://upstream.invalid/v1",
                                 api_key=KEY)
    row = db._get_conn().execute(
        "SELECT api_key FROM models_config WHERE id = ?", (cfg["id"],)).fetchone()
    assert row["api_key"].startswith(db._KEYRING_ENVELOPE_PREFIX)
    assert KEY not in row["api_key"]
    assert db.get_model_config(cfg["id"], mask_key=False)["api_key"] == KEY


def test_protect_roundtrip_and_idempotency_on_non_windows(tmp_workspace, monkeypatch):
    """句柄信封经读取侧还原恒等；已加封的值不二次包裹；空串仍是清除语义。"""
    _fresh_db(tmp_workspace)
    _as_non_windows(monkeypatch)
    _fake_keyring(monkeypatch)

    sealed = db.protect_secret(KEY)
    assert db.unprotect_secret(sealed) == KEY
    assert db.protect_secret(sealed) == sealed
    assert db.protect_secret("") == ""
    assert db.unprotect_secret("") == ""


# ── 写侧 fail-closed：keyring 不可用 → 显式失败，DB 无行 ───────────────────────

def test_missing_keyring_raises_and_writes_nothing(tmp_workspace, monkeypatch):
    _fresh_db(tmp_workspace)
    _as_non_windows(monkeypatch)
    _fake_keyring(monkeypatch, fail_import=True)

    with pytest.raises(db.SecretStorageError):
        db.create_model_config(name="m", base_url="http://upstream.invalid/v1",
                               api_key=KEY)
    # 库里不存在任何含明文或句柄的行 —— 保存失败没有副作用
    rows = db._get_conn().execute("SELECT api_key FROM models_config").fetchall()
    assert all(not (r["api_key"] or "").startswith(db._KEYRING_ENVELOPE_PREFIX)
               and KEY not in (r["api_key"] or "") for r in rows)


def test_keyring_backend_unavailable_raises(tmp_workspace, monkeypatch):
    """装了 keyring 但系统无凭据后端（NoKeyringError）同样显式失败。"""
    _fresh_db(tmp_workspace)
    _as_non_windows(monkeypatch)

    class _Errors:
        class NoKeyringError(RuntimeError):
            pass

    class _Keyring:
        @staticmethod
        def set_password(service, username, value):
            raise _Errors.NoKeyringError("no backend")

        @staticmethod
        def get_password(username):
            return None

    monkeypatch.setattr(db, "_keyring_import", lambda: (_Keyring(), _Errors))
    with pytest.raises(db.SecretStorageError):
        db.protect_secret(KEY)


# ── 读侧：诚实失败（不可用/条目缺失 → 空串），旧值兼容 ────────────────────────

def test_read_keyring_handle_unavailable_returns_empty(monkeypatch):
    """句柄在但 keyring 不可用/条目缺失 → 空串 + 不抛：报「未配置」引导重填。"""
    _as_non_windows(monkeypatch)
    monkeypatch.setattr(db, "_keyring_import",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    assert db.unprotect_secret(db._KEYRING_ENVELOPE_PREFIX + "abc123") == ""

    monkeypatch.setattr(db, "_keyring_get", lambda username: None)
    assert db.unprotect_secret(db._KEYRING_ENVELOPE_PREFIX + "abc123") == ""


def test_legacy_values_unchanged_on_non_windows(monkeypatch):
    """旧明文行与 dpapi 信封前缀在非 Windows 读取侧行为不变（历史行兼容/跨平台库）。"""
    _as_non_windows(monkeypatch)
    assert db.unprotect_secret("plain-old-value") == "plain-old-value"
    # dpapi 前缀但密文畸形 → 空串（不把密文当 key），与 DPAPI 失败口径一致
    assert db.unprotect_secret(db._SECRET_ENVELOPE_PREFIX + "!!!not-base64!!!") == ""
    # 相近前缀不误判
    assert db.unprotect_secret("keyring:v2:xyz") == "keyring:v2:xyz"
    assert db.unprotect_secret("keyring:v1") == "keyring:v1"


def test_win32_platform_ignores_keyring_path(monkeypatch):
    """win32 平台写侧仍走 DPAPI，不触 keyring（发布产物不含 keyring 也正常）。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    monkeypatch.delattr(db, "_keyring_set", raising=False)
    sealed = db.protect_secret(KEY)
    assert sealed.startswith(db._SECRET_ENVELOPE_PREFIX)
    assert db.unprotect_secret(sealed) == KEY


# ── 双份实现 parity：database.py 与 db/_secrets.py 必须同步 ────────────────────

def test_secrets_functions_ast_parity():
    """迁移过渡期副本纪律：凭据信封相关函数在两份实现里 AST 逐字一致。"""
    def _top_functions(module):
        tree = ast.parse(inspect.getsource(module))
        return {
            node.name: ast.dump(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    old_funcs = _top_functions(db)
    new_funcs = _top_functions(secrets_mod)
    for name in ("protect_secret", "unprotect_secret", "_dpapi_transform",
                 "_keyring_import", "_keyring_set", "_keyring_get", "_keyring_delete"):
        assert old_funcs.get(name) == new_funcs.get(name), (
            f"{name} 在 database.py 与 db/_secrets.py 的实现不一致：副本必须同步修改")
    # 异常类型语义一致（可被调用方现有的 OSError 捕获路径接住）
    assert issubclass(db.SecretStorageError, OSError)
    assert issubclass(secrets_mod.SecretStorageError, OSError)
