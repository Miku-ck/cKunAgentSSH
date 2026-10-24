"""HTTP 策略的 env 兜底优先级（round 22）。

裁决口径：缺行才用 env，有行以 DB 为准 —— 与 main.py 对 approval_fallback_s 的
启动回填同款。此前 CKUN_HTTP_RETRIES 在 netconfig 里先算后被无条件覆盖，等于死代码；
而 CKUN_HTTP_TIMEOUT 根本没有兜底入口。

代理按同一方向收敛成一条规则，但**行为没变**：旧代码在 try 之后另有等价的
`if not proxy: proxy = env`，所以库里空行/缺行时 env 一直有效，非空库行照旧压过
`CKUN_HTTP_PROXY`（round 19 登记的口径）。这里的三条代理用例是**钉住**而不是修复。
"""
import os

import pytest


@pytest.fixture
def base_db(tmp_workspace):
    import database as db
    db_path = os.path.join(tmp_workspace, "httpenv22.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


@pytest.fixture
def clean_netconfig():
    import netconfig
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    netconfig._policy = {"proxy_url": "", "timeout_seconds": 0, "retry_count": 1}
    netconfig._loaded = True
    yield netconfig
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


@pytest.fixture
def clean_http_env(monkeypatch):
    for name in ("CKUN_HTTP_RETRIES", "CKUN_HTTP_TIMEOUT", "CKUN_HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ── database 侧：把"缺行"与"存了默认值"分开 ─────────────────────────────────

def test_http_policy_reports_explicit_rows(base_db, clean_http_env):
    import database as db
    policy = db.get_http_policy()
    assert policy["retry_set"] is False and policy["timeout_set"] is False
    assert policy["retry_count"] == 1 and policy["timeout_seconds"] == 0

    db.set_app_setting("http.retry_count", "1")
    policy = db.get_http_policy()
    # 显式写了 1（与内置默认同值）也必须报成"有行"，否则 env 会继续越权
    assert policy["retry_set"] is True
    assert policy["retry_count"] == 1
    assert policy["timeout_set"] is False, "只写重试不该把超时也算成用户设定"


def test_http_policy_dirty_row_is_not_explicit(base_db, clean_http_env):
    """脏行按默认读数显示，同时不算"用户设定" —— env 兜底仍可生效，不会被脏值锁死。"""
    import database as db
    db.set_app_setting("http.retry_count", "abc")
    policy = db.get_http_policy()
    assert policy["retry_count"] == 1
    assert policy["retry_set"] is False


def test_http_policy_out_of_range_row_is_not_explicit(base_db, clean_http_env):
    """越界行同脏行：与 _int_app_setting 的「越界回默认，不钳到边界」口径一致 ——
    库里那行代表什么用户意图不可知，默认值是唯一说得过去的读数。"""
    import database as db
    db.set_app_setting("http.retry_count", "99")
    db.set_app_setting("http.timeout_seconds", "700")
    policy = db.get_http_policy()
    assert policy["retry_count"] == 1 and policy["retry_set"] is False
    assert policy["timeout_seconds"] == 0 and policy["timeout_set"] is False


# ── netconfig 侧：缺行才用 env ───────────────────────────────────────────────

def test_retry_env_applies_when_no_db_row(base_db, clean_netconfig, clean_http_env):
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_RETRIES", "3")
    policy = netconfig.load_policy_from_db()
    assert policy["retry_count"] == 3, "缺行时运维注入的重试次数必须生效（此前被无条件覆盖）"


def test_timeout_env_applies_when_no_db_row(base_db, clean_netconfig, clean_http_env):
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_TIMEOUT", "45")
    policy = netconfig.load_policy_from_db()
    assert policy["timeout_seconds"] == 45
    assert netconfig.timeout_seconds(fallback=10.0) == 45.0


def test_db_row_wins_over_env_once_written(base_db, clean_netconfig, clean_http_env):
    """用户在设置页写过一次之后，DB 就是权威，运维注入不再越权压过显式选择。"""
    import database as db
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_RETRIES", "3")
    clean_http_env.setenv("CKUN_HTTP_TIMEOUT", "45")
    db.set_app_setting("http.retry_count", "0")
    db.set_app_setting("http.timeout_seconds", "0")
    policy = netconfig.load_policy_from_db()
    # 显式写的 0 必须保住：重试 0 = 不重试，超时 0 = 各调用点沿用原默认
    assert policy["retry_count"] == 0
    assert policy["timeout_seconds"] == 0
    assert netconfig.timeout_seconds(fallback=10.0) == 10.0, "0 = 不覆盖调用点默认"


def test_env_out_of_range_falls_back_to_default(base_db, clean_netconfig, clean_http_env):
    """env 注入同样受区间约束：重试 >3 或超时 >600 不能把出站策略带出安全区。"""
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_RETRIES", "99")
    clean_http_env.setenv("CKUN_HTTP_TIMEOUT", "99999")
    policy = netconfig.load_policy_from_db()
    assert policy["retry_count"] == 1
    assert policy["timeout_seconds"] == 0


def test_proxy_db_still_wins_over_env(base_db, clean_netconfig, clean_http_env):
    """G4 既有口径不变：库里有非空值时压过 env，运维注入不得越权盖掉用户显式选择。"""
    import database as db
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_PROXY", "http://env-proxy:3128")
    db.set_app_setting("http.proxy_url", "http://db-proxy:8080")
    policy = netconfig.load_policy_from_db()
    assert policy["proxy_url"] == "http://db-proxy:8080"


def test_proxy_env_applies_when_db_row_is_empty(base_db, clean_netconfig, clean_http_env):
    """库里没写过（或写过空串 = 清除）时，env 注入必须真的生效。

    钉住用而非回归证据：这一条在 round 22 之前也成立（旧代码 try 之后有等价的
    `if not proxy` 兜底），把它写进来是因为**新代码把它改成了"缺行才让 env 说话"
    的统一规则**，得有一道闸防止以后又退回无条件覆盖。
    """
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_PROXY", "http://env-proxy:3128")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://env-proxy:3128"


def test_proxy_explicit_clear_then_restart_falls_back_to_env(
        base_db, clean_netconfig, clean_http_env):
    """「空串 = 清除该行、清完才轮到 env 兜底」（round 19 登记的口径）。"""
    import database as db
    import netconfig
    clean_http_env.setenv("CKUN_HTTP_PROXY", "http://env-proxy:3128")
    db.set_app_setting("http.proxy_url", "http://db-proxy:8080")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://db-proxy:8080"
    db.set_app_setting("http.proxy_url", "")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://env-proxy:3128"


def test_proxy_source_matches_what_is_actually_effective(base_db, clean_http_env):
    """面板上"代理来源 = env"必须意味着请求真的在走那个 env 代理；
    报 env 却直连，与报 db 却走 env 是同一类谎。"""
    from routes import settings as routes_settings
    import database as db
    import netconfig

    clean_http_env.setenv("CKUN_HTTP_PROXY", "http://env-proxy:3128")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://env-proxy:3128"
    assert routes_settings._proxy_source() == "env"

    db.set_app_setting("http.proxy_url", "http://db-proxy:8080")
    netconfig.load_policy_from_db()
    assert routes_settings._proxy_source() == "db"
    assert netconfig.get_policy()["proxy_url"] == "http://db-proxy:8080"
