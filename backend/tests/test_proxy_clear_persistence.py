"""S0-01 是**有争议的裁决项**，不是已定的 bug —— 本文件钉住现状口径并写下为什么。

两种读法都自洽：
  口径 A（本仓现状：round 19 登记 + round 22 两条测试钉住）：
    env 是「运维注入的默认」。「清除代理」= 放弃手动覆盖 ⇒ 回到默认，所以空串行与
    缺行同等对待，env 重新生效。
  口径 B（切片审查 S0-01 的判词）：
    env 是「策略注入」，用户在面板上的动作才是权威。空串行 = 显式「我要直连」，
    重启后 env 复活等于把用户刚做的动作静默覆盖掉。

**本文件名来自那次被回退的尝试**：我按 B 改过（get_http_policy 加 proxy_set、
netconfig 读它、_proxy_source 跟着改），撞掉 round 22 的两条测试后全部回退。
本仓另一会话在同一轮里看过 S0-01 并明确保留 A（原代码注释写着「代理口径本轮未变」）。
所以裁决前：不要按 B 单侧改判据，也不要把 A 当成「timeout/retry 同族防护漏了一处」
顺手补齐 —— 见 test_proxy_env_precedence_disputed.py 里的不对称性说明。
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


def test_absent_row_means_env_proxy(env_proxy):
    """缺行 → env 说话（A、B 在这一条上一致）。"""
    assert netconfig.load_policy_from_db()["proxy_url"] == ENVD


def test_nonempty_db_row_beats_env(env_proxy):
    """用户显式写了代理 → DB 权威（A、B 也一致）。"""
    env_proxy.set_app_setting("http.proxy_url", "http://user-chosen.proxy:8080")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://user-chosen.proxy:8080"


def test_cleared_row_falls_back_to_env_current_ruling(env_proxy):
    """空串行 = 清除该覆盖 ⇒ 轮到 env。这是**现状口径 A**。

    与 round 22 的 test_proxy_explicit_clear_then_restart_falls_back_to_env 刻意重复：
    两处一起钉，任何单侧改判据都会同时撞两条，逼改动的人读这段并走裁决，
    而不是顺手让一条测试变绿。
    """
    env_proxy.set_app_setting("http.proxy_url", "http://user-chosen.proxy:8080")
    assert netconfig.load_policy_from_db()["proxy_url"] == "http://user-chosen.proxy:8080"
    env_proxy.set_app_setting("http.proxy_url", "")
    assert netconfig.load_policy_from_db()["proxy_url"] == ENVD
    # 面板来源徽标与生效值同判据：空行不算 db 来源，否则显示与生效分叉
    from routes.settings import _proxy_source
    assert _proxy_source() == "env"


def test_being_becomes_effective_proxy_after_clear(env_proxy):
    """口径 A 下清除后**实际出站**确实回到 env —— 这条查的是生效值而非读库值。

    钉这个是因为 `proxy_url()` 是所有 httpx 客户端的唯一代理来源：如果哪天有人
    把判据改成「行存在即权威」，这里会和上面一起红，而不是只红一条。
    """
    env_proxy.set_app_setting("http.proxy_url", "")
    assert netconfig.load_policy_from_db()["proxy_url"] == ENVD
    netconfig._policy = dict(netconfig._policy,
                             proxy_url=netconfig.load_policy_from_db()["proxy_url"])
    assert netconfig.proxy_url() == ENVD
