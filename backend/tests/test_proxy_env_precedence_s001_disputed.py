# -*- coding: utf-8 -*-
"""`*_set` 判据只属于 timeout/retry —— 代理是**刻意的例外**，不是同族防护漏了一处。

切片审查 S0-01 的形式论据是：timeout/retry 早就按「行存在即用户表达过，显式写的 0
也算写过」判 env 让位，代理却用「非空才算数」，属于「同一族防护漏一处」。
这个类比不成立，本文件把差异钉下来：

  - timeout/retry 的 0 是**合法业务值**（0 = 不重试 / 0 = 不覆盖调用点默认）。
    若不区分「缺行」与「显式 0」，运维注入的重试次数会永久压掉用户的显式 0。
  - 代理的「空」同时是「清除」和「没写过」两种意思，字符串本身分不开。
    要区分就必须引入「这一行存在过」的额外判据（proxy_set）—— 那正是被回退的
    口径 B，需要产品裁决，不该由一次重构顺手带进来。

所以下面三条分别钉住：判据没偷偷长回来、timeout/retry 仍按行存在判、两者不同不是遗漏。
现状口径本身的测试在 test_proxy_clear_persistence_s001.py 与 round 22。
"""
import os

import pytest

import netconfig


@pytest.fixture
def clean_db(tmp_workspace):
    import database as db
    saved = dict(netconfig._policy)
    saved_loaded = netconfig._loaded
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "s001_asym.db"))
    yield db
    db._close_all_connections()
    netconfig._policy = saved
    netconfig._loaded = saved_loaded


def test_get_http_policy_exposes_no_proxy_set_flag(clean_db):
    """`proxy_set` 是被回退的口径 B 的载体 —— 不该悄悄长回来。"""
    p = clean_db.get_http_policy()
    assert "proxy_set" not in p, (
        "proxy_set 又出现了：口径 B（行存在即权威，空串=清除）需要产品裁决，"
        "不要在裁决前单侧实现。见 test_proxy_clear_persistence_s001.py 顶部的来龙去脉")
    assert set(p) == {"proxy_url", "timeout_seconds", "retry_count",
                      "timeout_set", "retry_set"}


def test_explicit_zero_still_counts_as_user_set_for_timeout_and_retry(clean_db):
    """timeout/retry 这一侧的判据是真的：0 也算用户写过，env 必须让位。"""
    clean_db.set_app_setting("http.timeout_seconds", "0")
    clean_db.set_app_setting("http.retry_count", "0")
    p = clean_db.get_http_policy()
    assert p["timeout_set"] is True and p["retry_set"] is True
    merged = netconfig.load_policy_from_db()
    assert merged["timeout_seconds"] == 0 and merged["retry_count"] == 0


def test_dirty_timeout_row_lets_env_back_in(clean_db, monkeypatch):
    """「脏行不算用户设定」是 *_set 判据的另一半：一个手工改坏的值不该永久锁掉运维注入。"""
    clean_db.set_app_setting("http.timeout_seconds", "abc")
    monkeypatch.setenv("CKUN_HTTP_TIMEOUT", "45")
    merged = netconfig.load_policy_from_db()
    assert merged["timeout_seconds"] == 45, (
        "脏行把 env 锁死了 —— 读到的应该是运维注入值，不是默认 0")
