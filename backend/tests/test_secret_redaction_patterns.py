"""自由文本凭据脱敏的精度回归 + 全局日志过滤器挂载。

守三件事：
1. 高置信凭据形态（key=value / Bearer / sk- / URL userinfo / mysql -p 内联）
   一律被 *** 掩码 —— 日志（logconfig）与审计（audit.append_record）两个
   出口共用 secret_redact 这一份模式库，口径必须一致；
2. 高频误报不发生：prompt_tokens/max_tokens 这类复合词（下划线让 \\btoken\\b
   天然不命中）、tar -pxzf 这类无凭据语义的 -p 连写、普通中文/英文文本必须
   原样通过 —— 误报会把脱敏器变成被人绕掉的噪音；
3. logconfig.apply_level 把 SecretRedactionFilter 幂等挂到 root handler。
"""
import logging

import pytest

import logconfig
from secret_redact import redact_secrets

# ── 1. 凭据形态必须被掩码 ──


def test_key_value_password_masked():
    assert redact_secrets("login password=abc123 ok") == "login password=*** ok"


def test_key_value_quoted_value_masked():
    assert redact_secrets('pwd: "se cret" x') == "pwd=*** x"


def test_key_value_token_and_apikey_masked():
    assert redact_secrets("token=abc.def.ghi") == "token=***"
    assert redact_secrets("api_key: sk-live-123") == "api_key=***"


def test_bearer_masked():
    out = redact_secrets("Authorization: Bearer eyJhbGciOi.12345678")
    assert "Bearer ***" in out
    assert "eyJhbGciOi" not in out


def test_openai_style_key_masked():
    out = redact_secrets("bad key sk-proj-abcdefgh12345678 rejected")
    assert "sk-***" in out
    assert "abcdefgh" not in out


def test_url_userinfo_masked():
    out = redact_secrets("connect https://user:pass@host/v1 failed")
    assert "user:pass" not in out
    assert "https://***@host/v1" in out


def test_mysql_inline_password_masked():
    out = redact_secrets("mysql -u root -p123456 -e 'select 1'")
    assert out == "mysql -u root -p*** -e 'select 1'"


def test_sshpass_inline_password_masked():
    out = redact_secrets("sshpass -pS3cret! rsync -a /src host:/dst")
    assert "S3cret" not in out
    assert "sshpass -p***" in out


# ── 2. 误报防御：无凭据语义的文本必须原样通过 ──


def test_underscore_compound_words_untouched():
    assert redact_secrets("prompt_tokens=1428 max_tokens=8192") == \
        "prompt_tokens=1428 max_tokens=8192"


def test_tar_p_flag_without_credential_context_untouched():
    assert redact_secrets("tar -pxzf foo.tar.gz") == "tar -pxzf foo.tar.gz"


def test_plain_text_untouched():
    plain = "重启 nginx 服务完成 exit=0 durationMs=123"
    assert redact_secrets(plain) == plain
    assert redact_secrets(None) == ""
    assert redact_secrets("") == ""


# ── 3. 日志过滤器挂载与改写 ──


@pytest.fixture(autouse=True)
def _restore_logconfig():
    """完整还原 logconfig 模块状态（镜像 test_http_log_diagnostics.clean_loglevel）。

    本文件按字母序排在 test_http_log_diagnostics 之前：若留下已安装的
    _handler，它「首次 apply_level 恰好 +1 个 handler」的断言会被顺序耦合
    打破 —— 状态泄漏是本文件的责任，不是它的。
    """
    root = logging.getLogger()
    saved_level = root.level
    saved_handler = logconfig._handler
    saved_current = logconfig._current
    yield
    if logconfig._handler is not None and logconfig._handler is not saved_handler:
        root.removeHandler(logconfig._handler)
    logconfig._handler = saved_handler
    root.setLevel(saved_level)
    logconfig._current = saved_current


def test_filter_rewrites_record_with_args():
    f = logconfig.SecretRedactionFilter()
    rec = logging.LogRecord(
        name="t", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="bridge handshake password=%s", args=("abc123",), exc_info=None)
    assert f.filter(rec) is True
    assert rec.msg == "bridge handshake password=***"
    assert rec.args == ()


def test_filter_plain_record_untouched():
    f = logconfig.SecretRedactionFilter()
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="schedule tick done %s", args=("ok",), exc_info=None)
    assert f.filter(rec) is True
    assert rec.getMessage() == "schedule tick done ok"


def test_apply_level_installs_filter_idempotently():
    logconfig.apply_level("warning")
    handler = logconfig._handler
    assert handler is not None, "apply_level 必须已安装 root handler"
    count = sum(isinstance(f, logconfig.SecretRedactionFilter)
                for f in handler.filters)
    assert count == 1, "脱敏过滤器应恰好挂一个"
    logconfig.apply_level("info")
    count_again = sum(isinstance(f, logconfig.SecretRedactionFilter)
                      for f in handler.filters)
    assert count_again == 1, "重复 apply_level 不得重复挂过滤器"
    logconfig.apply_level(logconfig.DEFAULT_LEVEL)
