# -*- coding: utf-8 -*-
"""设置项端到端回归 — 真实 FastAPI 路由 + 真实 SQLite DB + mock LLM。

与 test_settings_wiring.py（单元级契约断言）互补：本测试走完整
POST /api/agent/run → agent_run → Harness/Driver/Reins → LLM 调用链路，
记录 fake LLM 实际收到的 messages，实证两条曾"纯摆设"的前端设置
真实改变后端出站请求：

  1. chat.contextWindow（上下文窗口）→ 出站历史消息条数变化
     （DB 预置 5 条历史：window=2 只带最后 2 条；缺省 =20 带全部 5 条）。
  2. chat.autoSkillMatch（自动技能匹配）→ 触发词匹配是否执行、
     技能目录是否注入出站 system 消息；显式 skill_ids 关闭时仍注入。

LLM 用 fake 替换（routes.agent.StreamLLMClient 构造点 monkeypatch），
不发真实网络请求；记忆/意识 tick/后处理/反思演化全部旁路。

运行: python -m pytest backend/tests/test_settings_e2e.py -v
"""
import json
import os
import sys
from pathlib import Path

import fastapi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database as db  # noqa: E402

HISTORY_MESSAGES = [f"H-00{i}" for i in range(1, 6)]  # 5 条预置历史
AUTO_SKILL = {
    "id": "sk-auto",
    "name": "自动技能",
    "slug": "auto-skill",
    "enabled": 1,
    "priority": 5,
}
EXPLICIT_SKILL = {
    "id": "sk-explicit",
    "name": "显式技能",
    "slug": "explicit-skill",
    "enabled": 1,
    "priority": 9,
}


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeLLM:
    """记录每次出站 messages 的假 LLM 客户端（符合 LLMClient 协议）。

    返回纯文本（无 tool_calls）→ Driver 首轮即 COMPLETED。
    """

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.calls: list[list[dict]] = []
        FakeLLM.instances.append(self)

    async def call(self, messages, tools, model, temperature, max_tokens,
                   enable_thinking, reasoning_max_tokens=None):
        self.calls.append(messages)
        from agent.harness.contracts import LLMOutput
        return LLMOutput(content="OK", prompt_tokens=10, completion_tokens=2)


def make_fake_llm_client():
    def factory(*args, **kwargs):
        return FakeLLM()
    return factory


class FakeSkillRegistry:
    """记录 match_skills 调用；目录/预载为固定可断言文本。"""

    instances: list = []

    def __init__(self, auto_skill=None, explicit_skill=None):
        self.auto_skill = auto_skill or {}
        self.explicit_skill = explicit_skill or {}
        self.match_calls = 0
        self.catalog_calls = 0
        FakeSkillRegistry.instances.append(self)

    def match_skills(self, message, mode, max_results=3):
        # 仅当任务文本含触发词才命中，模拟真实触发词匹配
        if self.auto_skill and "部署" in (message or ""):
            self.match_calls += 1
            return [self.auto_skill]
        return []

    def build_skill_catalog(self, skill_ids, mode):
        self.catalog_calls += 1
        return "[E2E-CATALOG:" + ",".join(skill_ids) + "]"

    def build_skill_preload(self, skill_ids, mode):
        return "[E2E-PRELOAD:" + ",".join(skill_ids) + "]"


class FakeMemoryManager:
    async def retrieve(self, **kwargs):
        return []

    async def save_episodic(self, **kwargs):
        return None


class FakePostProcessManager:
    def __init__(self, *args, **kwargs):
        self.submitted = []

    async def submit(self, **kwargs):
        self.submitted.append(kwargs)


class FakeTick:
    def ensure_started(self):
        return None


async def _noop_reflection(*args, **kwargs):
    return None


# ── 响应解析 ─────────────────────────────────────────────────────────────────


def parse_sse(text: str):
    """解析 SSE 文本为事件 dict 列表（跳过 heartbeat 空 data 与 [DONE]）。"""
    events = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if not data.strip() or data == "[DONE]":
            continue
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return events


# ── fixture ─────────────────────────────────────────────────────────────────


@pytest.fixture
def app_env(tmp_workspace, monkeypatch):
    """隔离临时 DB + 装配 agent 路由 + 旁路外部依赖。返回 TestClient（已启动）。"""
    db_path = os.path.join(tmp_workspace, "e2e_settings.db")
    # 重置线程本地连接池：上一个测试的连接池绑在已删除的旧库文件上，
    # 不清理则本测试 _get_conn() 复用旧连接 → "no such table"
    db._close_all_connections()
    db.init_db(db_path)

    # DB 预置：默认模型 + 会话 + 5 条历史（created_at 秒级相同，rowid 保证插入序）
    model = db.create_model_config(
        name="fake-model", base_url="http://fake.local/v1",
        api_key="EMPTY", model_id="fake-model",
        is_default_ssh=True, max_tokens=16384,
    )
    conv = db.create_conversation(title="e2e", mode="ssh")
    for i, content in enumerate(HISTORY_MESSAGES):
        db.add_message(conv["id"], "user", content)

    # 旁路外部依赖（全部 monkeypatch，测试结束自动还原）
    import routes.agent as agent_mod
    import skill_registry as reg_mod

    monkeypatch.setattr(agent_mod, "StreamLLMClient", make_fake_llm_client())
    monkeypatch.setattr(agent_mod, "get_memory_manager", lambda **kw: FakeMemoryManager())
    monkeypatch.setattr(agent_mod, "get_global_tick", lambda: FakeTick())
    monkeypatch.setattr(agent_mod, "_run_reflection_and_evolution", _noop_reflection)
    monkeypatch.setattr(
        reg_mod, "get_skill_registry",
        lambda: FakeSkillRegistry(auto_skill=AUTO_SKILL, explicit_skill=EXPLICIT_SKILL),
    )

    import post_processor as pp_mod
    pp = FakePostProcessManager()
    monkeypatch.setattr(pp_mod, "get_post_process_manager", lambda: pp)

    client_app = fastapi.FastAPI()
    client_app.include_router(agent_mod.router, prefix="/api")
    from fastapi.testclient import TestClient
    tc = TestClient(client_app)
    tc.__enter__()
    tc._e2e_ctx = conv["id"]
    tc._e2e_model_id = model["id"]
    tc._e2e_postproc = pp
    try:
        yield tc
    finally:
        tc.__exit__(None, None, None)


def run_agent(client, payload: dict) -> str:
    resp = client.post("/api/agent/run", json=payload)
    assert resp.status_code == 200, f"status={resp.status_code} body={resp.text[:500]}"
    return resp.text


# ── contextWindow：出站历史条数 ──────────────────────────────────────────────


def _llm_seen_messages(client) -> list[dict]:
    fakes = FakeLLM.instances
    assert fakes, "LLM 未被调用"
    assert fakes[-1].calls, "LLM 未收到任何请求"
    return fakes[-1].calls[0]


def _history_contents(messages: list[dict]) -> list[str]:
    return [
        m["content"] for m in messages
        if m.get("role") == "user" and m.get("content") in HISTORY_MESSAGES
    ]


def test_context_window_limits_history(app_env):
    """window=2：LLM 只收到最后 2 条历史（H-004/H-005），更早的 H-001~H-003 不出现。"""
    FakeLLM.instances.clear()
    payload = {"task": "当前任务T1", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id, "context_window": 2}
    text = run_agent(app_env, payload)
    events = parse_sse(text)
    assert any(e.get("type") == "done" for e in events), f"无 done 事件: {events[-3:]}"

    contents = _history_contents(_llm_seen_messages(app_env))
    assert contents == ["H-004", "H-005"], f"window=2 应只带最后2条历史: {contents}"
    assert "H-001" not in contents and "H-002" not in contents and "H-003" not in contents


def test_context_window_default_loads_all(app_env):
    """缺省 window=20：5 条历史全部入站（默认值与请求契约一致）。"""
    FakeLLM.instances.clear()
    payload = {"task": "当前任务T2", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id}
    run_agent(app_env, payload)

    contents = _history_contents(_llm_seen_messages(app_env))
    assert contents == HISTORY_MESSAGES, f"缺省 window 应带全部 5 条: {contents}"


def test_context_window_middle_value(app_env):
    """window=3：只带最后 3 条。"""
    FakeLLM.instances.clear()
    payload = {"task": "当前任务T3", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id, "context_window": 3}
    run_agent(app_env, payload)

    contents = _history_contents(_llm_seen_messages(app_env))
    assert contents == ["H-003", "H-004", "H-005"], f"window=3: {contents}"


def test_context_window_out_of_bounds_422(app_env):
    """越界值由请求契约拒绝（422），不会打到 DB 查询。"""
    FakeLLM.instances.clear()
    payload = {"task": "t", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id, "context_window": 0}
    resp = app_env.post("/api/agent/run", json=payload)
    assert resp.status_code == 422
    assert not FakeLLM.instances, "422 时不应触发 LLM 调用"


# ── autoSkillMatch：触发词匹配与技能注入 ─────────────────────────────────────


def _system_texts(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "system"]


def test_auto_skill_match_on_injects_catalog(app_env):
    """默认开启：任务含触发词 → match_skills 被调用，技能目录注入出站 system。"""
    FakeLLM.instances.clear()
    payload = {"task": "帮我部署服务到生产", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id}
    run_agent(app_env, payload)

    reg = FakeSkillRegistry.instances[-1]
    assert reg.match_calls == 1, "开启时 match_skills 应被调用 1 次"
    sys_texts = _system_texts(_llm_seen_messages(app_env))
    joined = "\n".join(sys_texts)
    assert "[E2E-CATALOG:sk-auto]" in joined, f"技能目录应注入 system: {joined[-400:]}"


def test_auto_skill_match_off_skips_matching(app_env):
    """关闭：match_skills 不被调用、目录不注入（设置真正生效的核心断言）。"""
    FakeLLM.instances.clear()
    payload = {"task": "帮我部署服务到生产", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id, "auto_skill_match": False}
    run_agent(app_env, payload)

    reg = FakeSkillRegistry.instances[-1]
    assert reg.match_calls == 0, "关闭时不得调用 match_skills"
    joined = "\n".join(_system_texts(_llm_seen_messages(app_env)))
    assert "[E2E-CATALOG:" not in joined, "关闭时技能目录不得入站"


def test_auto_skill_match_off_still_injects_explicit(app_env):
    """关闭 + 显式 skill_ids：自动匹配跳过，但用户显式选择的技能仍注入。"""
    FakeLLM.instances.clear()
    payload = {"task": "帮我部署服务到生产", "conversation_id": app_env._e2e_ctx,
               "model_id": app_env._e2e_model_id, "auto_skill_match": False,
               "skill_ids": ["sk-explicit"]}
    run_agent(app_env, payload)

    reg = FakeSkillRegistry.instances[-1]
    assert reg.match_calls == 0
    joined = "\n".join(_system_texts(_llm_seen_messages(app_env)))
    assert "sk-explicit" in joined, "显式技能必须注入"
    assert "sk-auto" not in joined, "自动技能不得注入"
