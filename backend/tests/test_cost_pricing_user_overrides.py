"""定价覆盖的优先级与脏值防御回归 + /settings/cost-pricing 端点契约。

守四件事：
1. 用户覆盖按模型 ID 精确匹配（小写归一）优先于内置关键词表 —— 新型号/自定义
   型号不再被 fallback 定价虚增；
2. 本地部署（Ollama/vLLM 自有硬件）记 [0, 0] 后成本恒为 0，不会被 fallback
   定价算出假成本提前触发熔断；
3. 落库值脏（坏 JSON / 非法条目）按空表处理，内置表照常可用 —— 读侧 fail-safe；
   写侧任一条非法整体拒绝，绝不半表落库；
4. 新补录的内置条目更具体的型号必须排在裸系列名前（keyword 边界允许 "-" 命中，
   排序错了会拿错价）。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database as db
from agent import cost_tracker


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """每个用例从空缓存起步：覆盖表的懒加载缓存不跨用例串扰。"""
    monkeypatch.setattr(cost_tracker, "_pricing_overrides_cache", None)


def _stub_persisted(monkeypatch, payload):
    monkeypatch.setattr(db, "get_app_setting",
                        lambda key, default=None: payload if key == "cost.pricing_overrides" else None)


# ── 1/2. 覆盖优先级与本地零成本 ──


def test_override_exact_match_beats_builtin(monkeypatch):
    _stub_persisted(monkeypatch, json.dumps({"gpt-4o": [1.0, 2.0]}))
    assert cost_tracker.get_model_pricing("gpt-4o") == (1.0, 2.0)
    # 大小写归一到同一覆盖键
    assert cost_tracker.get_model_pricing("GPT-4O") == (1.0, 2.0)
    # 未覆盖的型号仍走内置表
    assert cost_tracker.get_model_pricing("gpt-4o-mini") == (0.15, 0.60)


def test_local_model_zero_pricing(monkeypatch):
    _stub_persisted(monkeypatch, json.dumps({"qwen3:32b": [0, 0]}))
    assert cost_tracker.get_model_pricing("qwen3:32b") == (0.0, 0.0)
    assert cost_tracker.calc_cost(1000, 1000, "qwen3:32b") == 0.0


# ── 3. 读侧 fail-safe / 写侧整体拒绝 ──


def test_dirty_persisted_json_falls_back_to_builtin(monkeypatch):
    _stub_persisted(monkeypatch, "{not-json")
    assert cost_tracker.get_pricing_overrides() == {}
    assert cost_tracker.get_model_pricing("gpt-4o") == (2.50, 10.00)


def test_set_overrides_persists_and_updates_cache(monkeypatch):
    calls = {}
    monkeypatch.setattr(db, "set_app_setting",
                        lambda key, value: calls.update({key: value}))
    effective = cost_tracker.set_pricing_overrides({"GLM-4.6": [1.5, 3.0]})
    assert effective == {"glm-4.6": (1.5, 3.0)}
    assert calls["cost.pricing_overrides"] == json.dumps(
        {"glm-4.6": [1.5, 3.0]}, ensure_ascii=False, separators=(",", ":"))
    # 缓存即时生效，无需重读库
    assert cost_tracker.get_model_pricing("glm-4.6") == (1.5, 3.0)


@pytest.mark.parametrize("bad", [
    {"": [1, 1]},            # 空 ID
    {"m": [1]},              # 价格长度错
    {"m": [1, "x"]},         # 非数字
    {"m": [1, True]},        # bool 不是数字（bool 是 int 子类，须显式拦）
    {"m": [-1, 1]},          # 负价
    {"m": [1, 10 ** 9]},     # 超上限的脏值
    ["m"],                   # 整表不是 dict
])
def test_set_overrides_rejects_invalid(bad):
    with pytest.raises(ValueError):
        cost_tracker.set_pricing_overrides(bad)


# ── 4. 内置条目排序（更具体的在前）──


def test_builtin_specific_before_generic():
    assert cost_tracker.get_model_pricing("gpt-4.1-mini") == (0.40, 1.60)
    assert cost_tracker.get_model_pricing("gpt-4.1") == (2.00, 8.00)
    # deepseek 系维持家族级粗粒度（dash 边界归属家族行，见
    # test_ai_loop_core_regression.test_pricing_dash_still_counts_as_boundary）
    assert cost_tracker.get_model_pricing("deepseek-chat") == (0.14, 0.28)
    assert cost_tracker.get_model_pricing("deepseek-reasoner") == (0.14, 0.28)
    # 未知型号仍走 fallback
    assert cost_tracker.get_model_pricing("some-unknown-model") == (0.50, 1.50)


# ── 端点契约 ──


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(db, "set_app_setting", lambda key, value: None)
    from routes.settings import router as settings_router
    app = FastAPI()
    app.include_router(settings_router, prefix="/api")
    return TestClient(app)


def test_endpoint_put_then_get_roundtrip(client, monkeypatch):
    put = client.put("/api/settings/cost-pricing",
                     json={"overrides": {"glm-4.6": [1.5, 3.0]}})
    assert put.status_code == 200
    assert put.json()["overrides"] == {"glm-4.6": [1.5, 3.0]}
    # GET 读的是 set 后的缓存（读写同源）
    got = client.get("/api/settings/cost-pricing")
    assert got.status_code == 200
    assert got.json()["overrides"] == {"glm-4.6": [1.5, 3.0]}


def test_endpoint_rejects_invalid_entry(client):
    resp = client.put("/api/settings/cost-pricing",
                      json={"overrides": {"m": [-1, 2]}})
    assert resp.status_code == 422
    assert "超出" in resp.json()["detail"] or "非法" in resp.json()["detail"]
