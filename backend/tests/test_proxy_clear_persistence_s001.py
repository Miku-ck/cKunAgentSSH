# -*- coding: utf-8 -*-
"""S0-01 回归 — 「清除代理」必须是可持久表达的动作。

面板点「清除」会往库里写一行 `http.proxy_url = ''`。timeout/retry 早在 a8e588c
就按「缺行才用 env，用户写过就以 DB 为准（显式写的 0 也算写过）」判优先级，
而代理漏了这一路、用「非空才算数」：空串为假 ⇒ 当场直连、重启后运维注入的
CKUN_HTTP_PROXY 静默复活，把用户刚做的动作覆盖掉。
"""
import os

import pytest

import netconfig


ENV_KEY = "CKUN_HTTP_PROXY"
ENVD = "http://ops-injected.proxy:3128"


@pytest.fixture
def env_proxy(monkeypatch, tmp_workspace):
    """部署侧注入 env 代理 + 一个干净库（无 http.* 行）。"""
    import database as db
    monkeypatch.setenv(ENV_KEY, ENVD)
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s001.db"))
    yield db
    db._close_all_connections()
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


def _effective_proxy():
    return netconfig.load_policy_from_db()["proxy_url"]


def test_absent_row_lets_env_proxy_apply(env_proxy):
    """没写过 = env 说话（引入本设置前的行为，也是运维统一注入的形态）。"""
    assert env_proxy.get_http_policy()["proxy_set"] is False
    assert _effective_proxy() == ENVD


def test_cleared_proxy_row_survives_restart(env_proxy):
    """S0-01 本体：写了再清除，重启后必须仍是直连，而不是 env 复活。"""
    env_proxy.set_app_setting("http.proxy_url", "http://user-chosen.proxy:8080")
    assert _effective_proxy() == "http://user-chosen.proxy:8080"

    # 面板的清除动作 = PUT {proxy_url:''} → 库里落下空串行
    env_proxy.set_app_setting("http.proxy_url", "")
    assert env_proxy.get_http_policy()["proxy_set"] is True
    assert _effective_proxy() == "", "空串是「显式清空」，不能被当成「缺省」"


def test_explicitly_set_proxy_wins_over_env(env_proxy):
    env_proxy.set_app_setting("http.proxy_url", ENVD.replace("ops-injected", "user"))
    assert _effective_proxy() == "http://user.proxy:3128"


def test_proxy_source_uses_the_same_judgement(env_proxy):
    """面板的来源徽标不许再用「非空才算 db」另抄一份口径。"""
    from routes.settings import _proxy_source
    assert _proxy_source() == "env"
    env_proxy.set_app_setting("http.proxy_url", "http://u.proxy:1")
    assert _proxy_source() == "db"
    # 清除后：权威值是「用户清空」，所以来源仍是 db —— 绝不能再显示「来自环境变量」
    env_proxy.set_app_setting("http.proxy_url", "")
    assert _proxy_source() == "db"


def test_no_env_means_clear_is_a_no_op(env_proxy, monkeypatch):
    """没有 env 注入时，清除前后都是直连（回归基线：这条一直是好的）。"""
    monkeypatch.delenv(ENV_KEY, raising=False)
    env_proxy.set_app_setting("http.proxy_url", "")
    assert _effective_proxy() == ""


def test_all_three_siblings_share_one_rule(env_proxy):
    """同族判据不许再漏一个：0 与空串都算「用户写过」。"""
    env_proxy.set_app_setting("http.timeout_seconds", "0")
    env_proxy.set_app_setting("http.retry_count", "0")
    p = env_proxy.get_http_policy()
    assert (p["timeout_set"], p["retry_set"], p["proxy_set"]) == (True, True, False)
    merged = netconfig.load_policy_from_db()
    assert merged["timeout_seconds"] == 0 and merged["retry_count"] == 0
