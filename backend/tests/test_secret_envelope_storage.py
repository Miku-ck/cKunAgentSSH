"""凭据信封存储（DPAPI）— LLM/Tavily api_key 不再明文落库的回归测试。

守护点：
- 加解密往返：protect_secret → unprotect_secret 恒等还原（win32 真 DPAPI）；
- 旧明文兼容：无信封前缀的值读取侧原样返回，历史行不用迁移；
- 信封幂等：已加封的值不会被二次包裹（重复保存不叠层）；
- 诚实失败：密文损坏/解密失败返回空串并留痕，绝不把密文当 key 回给调用方；
  加密失败向上抛错，绝不静默降级成明文落盘；
- 掩码不回显：前端拿到的 api_key/tavily 掩码里不含明文也不含信封密文，
  掩码逻辑（_mask_api_key/_mask_secret）本身与加密改造前逐字一致。

运行: python -m pytest backend/tests/test_secret_envelope_storage.py -v
"""
import sys

import pytest

import database as db

KEY = "sk-antinarative-1234567890abcdef"
TAVILY_KEY = "tvly-abcdef1234567890abcdef"


def _fresh_db(tmp_workspace, monkeypatch):
    """每个用例一份干净库（连接池先清空，路径才切换得掉）。"""
    db._close_all_connections()
    db.init_db(tmp_workspace + "/envelope.sqlite")
    return db


# ── DPAPI 真实往返（仅 Windows；其他平台跳过，对应平台的降级另行断言）──────

win32_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")


@win32_only
def test_dpapi_roundtrip(tmp_workspace):
    _fresh_db(tmp_workspace, None)
    sealed = db.protect_secret(KEY)
    assert sealed.startswith(db._SECRET_ENVELOPE_PREFIX)
    assert KEY not in sealed
    assert db.unprotect_secret(sealed) == KEY


@win32_only
def test_dpapi_ciphertext_is_nondeterministic_and_idempotent(tmp_workspace):
    """同一明文两次加封密文不同（DPAPI 内含随机性，拖库者无法按值比对）；
    已加封的值再次 protect 不二次包裹（重复保存不叠层）。"""
    _fresh_db(tmp_workspace, None)
    a = db.protect_secret(KEY)
    b = db.protect_secret(KEY)
    assert a != b
    assert db.protect_secret(a) == a
    # 空值 = 清除语义，不包信封
    assert db.protect_secret("") == ""
    assert db.unprotect_secret("") == ""


@win32_only
def test_model_config_roundtrip_through_db(tmp_workspace):
    """模型 api_key：库里是信封，mask_key=False 读回明文，mask 读回掩码。"""
    _fresh_db(tmp_workspace, None)
    cfg = db.create_model_config(name="m", base_url="http://upstream.invalid/v1",
                                 api_key=KEY)
    row = db._get_conn().execute(
        "SELECT api_key FROM models_config WHERE id = ?", (cfg["id"],)).fetchone()
    assert row["api_key"].startswith(db._SECRET_ENVELOPE_PREFIX)
    assert KEY not in row["api_key"]
    assert db.get_model_config(cfg["id"], mask_key=False)["api_key"] == KEY
    masked = db.get_model_config(cfg["id"])["api_key"]
    assert masked == "sk-a****"
    assert KEY not in masked


@win32_only
def test_legacy_plaintext_model_row_still_readable(tmp_workspace):
    """历史明文行（信封机制上线前写入）读取侧原样返回，下次保存升级为信封。"""
    _fresh_db(tmp_workspace, None)
    cfg = db.create_model_config(name="m", base_url="http://upstream.invalid/v1")
    conn = db._get_conn()
    conn.execute("UPDATE models_config SET api_key = ? WHERE id = ?", (KEY, cfg["id"]))
    conn.commit()
    assert db.get_model_config(cfg["id"], mask_key=False)["api_key"] == KEY
    # 保存一次 → 行值升级为信封，读取结果不变
    db.update_model_config(cfg["id"], api_key=KEY + "X")
    row = conn.execute(
        "SELECT api_key FROM models_config WHERE id = ?", (cfg["id"],)).fetchone()
    assert row["api_key"].startswith(db._SECRET_ENVELOPE_PREFIX)
    assert db.get_model_config(cfg["id"], mask_key=False)["api_key"] == KEY + "X"


# ── 平台降级与非 Windows 路径（任何平台都跑）───────────────────────────────

def test_non_windows_platform_keeps_plaintext(monkeypatch):
    """非 win32 平台暂保持明文落库（TODO：接系统凭据库），读写均为恒等变换。"""
    monkeypatch.setattr(db, "_SECRET_PROTECTION_PLATFORM", "linux")
    assert db.protect_secret(KEY) == KEY
    assert db.unprotect_secret(KEY) == KEY


def test_unprotect_passthrough_for_plain_values():
    assert db.unprotect_secret("log.level") == "log.level"
    assert db.unprotect_secret(None) is None


@win32_only
def test_tampered_envelope_returns_empty_not_ciphertext(tmp_workspace, monkeypatch):
    """解密失败（密文损坏 / 库跨用户拷贝）→ 空串 + 不抛：宁报「未配置」
    也不把密文当 key 送给上游。"""
    _fresh_db(tmp_workspace, None)
    assert db.unprotect_secret(db._SECRET_ENVELOPE_PREFIX + "!!!not-base64!!!") == ""
    good = db.protect_secret(KEY)
    monkeypatch.setattr(db, "_dpapi_transform",
                        lambda data, protect: (_ for _ in ()).throw(OSError("boom")))
    assert db.unprotect_secret(good) == ""


@win32_only
def test_protect_failure_raises_instead_of_plaintext_fallback(tmp_workspace, monkeypatch):
    """加密失败必须向上抛 —— 静默降级会把「这条没加密」藏进库。"""
    _fresh_db(tmp_workspace, None)
    monkeypatch.setattr(db, "_dpapi_transform",
                        lambda data, protect: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        db.protect_secret(KEY)


# ── Tavily 密钥（app_settings 通道）与掩码不回显 ───────────────────────────

@win32_only
def test_tavily_key_enveloped_and_masked_for_frontend(tmp_workspace):
    """PUT web-tools 后：库里是信封、get_app_setting 读回明文、GET 面板只回掩码。"""
    _fresh_db(tmp_workspace, None)
    from routes import settings as settings_routes

    settings_routes.update_web_tools_settings(
        settings_routes.WebToolsSettingsRequest(tavily_api_key=TAVILY_KEY))
    raw = db.list_app_settings().get("web.tavily_api_key")
    assert raw.startswith(db._SECRET_ENVELOPE_PREFIX)
    assert TAVILY_KEY not in raw
    assert db.get_app_setting("web.tavily_api_key") == TAVILY_KEY

    view = settings_routes.get_web_tools_settings()
    assert view["tavily_api_key_set"] is True
    masked = view["tavily_api_key_masked"]
    assert TAVILY_KEY not in masked
    # 掩码口径不变：前 4 后 4（密钥 >12 字符），中间不可能拼回原文
    assert masked == "tvly…" + TAVILY_KEY[-4:]


def test_mask_secret_never_reveals_middle():
    """掩码函数本体与改造前一致：长值前 4 后 4，短值只报已设置。"""
    from routes.settings import _mask_secret
    long_val = "0123456789abcdef"
    assert _mask_secret(long_val) == "0123…cdef"
    assert "4567" not in _mask_secret(long_val)
    assert _mask_secret("short") == "已设置"
    assert _mask_secret("") == ""


def test_get_app_setting_plaintext_keys_unaffected(tmp_workspace):
    """信封化只作用于机密键：普通设置键（log.level 等）读写逐字不变。"""
    _fresh_db(tmp_workspace, None)
    db.set_app_setting("log.level", "warning")
    assert db.get_app_setting("log.level") == "warning"
    assert db.get_app_setting("absent.key", "fallback") == "fallback"


# ── 自检：证明测试真的连着实现（防止 mock 掉一切后恒绿的摆设）──────────────

@win32_only
def test_envelope_detection_rejects_similar_prefix(tmp_workspace):
    """前缀比对必须是完整 `dpapi:v1:` —— 前缀相近但不同的值按旧明文透传，
    不会被错误地送进 DPAPI 解密。"""
    _fresh_db(tmp_workspace, None)
    assert db.unprotect_secret("dpapi:v2:AAAA") == "dpapi:v2:AAAA"
    assert db.unprotect_secret("dpapi:v1") == "dpapi:v1"
