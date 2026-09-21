# -*- coding: utf-8 -*-
"""第 13b 轮回归：后处理/意识心跳的失败语义与配置同源。

覆盖三类缺陷（均为「成功形状的失败」或「一个值两处定义」家族）：
1. auto 技能数量保护：COUNT 查询异常不得被吞成 0 后继续生成（最该挡的时候形同虚设）
2. 记忆保留上限：post_process 的 prune 必须读记忆设置，不能再硬编码 100
   （同一保留值写两处 → 面板调到 500 仍被每轮削回 100）
3. 标题为 NULL 的会话：等价于默认标题，不能判成「用户已自定义」而永久早退
4. PostProcessManager.submit：常驻单例持有的 config 是构造时快照，
   设置面板 PUT 只换模块级 _config → 关掉后处理仍会放行任务
5. _extract_facts：逗号工具列表只留首位 → 提取出的「常用工具」永远是第一个

运行: python -m pytest backend/tests/test_post_process_round13b.py -v
"""
import asyncio

import pytest

import post_processor as pp
from post_processor import (
    PostProcessConfig,
    PostProcessManager,
    TurnContext,
    _count_auto_skills,
    _generate_skill,
    _generate_title,
)


def _ctx(turn_index: int = 1) -> TurnContext:
    return TurnContext(
        mode="ssh",
        conversation_id="conv-13b",
        user_message="怎么查 nginx 错误日志",
        assistant_content="用 tail -f /var/log/nginx/error.log 查看",
        assistant_reasoning="",
        model_id="model-1",
        turn_index=turn_index,
    )


# ── 1/2. auto 技能数量保护：查询失败 = 不放行 ──


def test_count_auto_skills_propagates_db_error(monkeypatch):
    """_count_auto_skills 不再把异常吞成 0 —— 吞掉之后调用方的保护分支不可达"""
    def boom(source: str) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(pp.db, "count_skills_by_source", boom)
    with pytest.raises(RuntimeError):
        _count_auto_skills()


def test_generate_skill_fails_closed_when_count_query_fails(monkeypatch):
    """数不出来就跳过本轮生成，不得继续走 LLM 写库"""
    def boom() -> int:
        raise RuntimeError("database is locked")

    called: dict = {}

    def fake_get_model_config(model_id, mask_key=False):
        called["model"] = model_id
        return {"id": model_id}

    monkeypatch.setattr(pp, "_count_auto_skills", boom)
    monkeypatch.setattr(pp.db, "get_model_config", fake_get_model_config)

    asyncio.run(_generate_skill(_ctx(turn_index=0), PostProcessConfig()))
    assert not called, "COUNT 查询失败后仍继续生成技能（保护在最容易出问题时失效）"


def test_generate_skill_still_caps_at_limit(monkeypatch):
    """正向：未达上限才放行；达上限仍要拦住（fail-closed 不能顺手把保护删掉）"""
    called: dict = {}

    def fake_get_model_config(model_id, mask_key=False):
        called["model"] = model_id
        return {"id": model_id}

    monkeypatch.setattr(pp.db, "count_skills_by_source", lambda source: 5)
    monkeypatch.setattr(pp.db, "get_model_config", fake_get_model_config)

    cfg = PostProcessConfig(max_skills_per_conversation=5)
    asyncio.run(_generate_skill(_ctx(turn_index=0), cfg))
    assert not called, "已达上限（5/5）仍生成了技能"


# ── 记忆 prune 与设置同源 ──


def test_memory_prune_uses_configured_max_items(monkeypatch):
    """保留上限只有一个来源：记忆设置的 max_items，而不是硬编码 100"""
    async def fake_llm(**kwargs):
        return '{"memories": ["nginx 日志路径是 /var/log/nginx/error.log"]}'

    class FakeMemMgr:
        async def save_semantic(self, **kwargs):
            return 1

    pruned: dict = {}

    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid, "base_url": "u", "api_key": "k"})
    monkeypatch.setattr(pp.db, "get_default_model",
                        lambda mode, mask_key=False: {"base_url": "u", "api_key": "k", "model_id": "m"})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)

    async def not_dup(*args, **kwargs):
        return False

    monkeypatch.setattr(pp, "_is_memory_duplicate", not_dup)
    import agent.memory as am
    monkeypatch.setattr(am, "get_memory_manager", lambda **kwargs: FakeMemMgr())
    monkeypatch.setattr(pp.db, "get_memory_settings", lambda: {"max_items": 500})
    monkeypatch.setattr(pp.db, "prune_agent_memories",
                        lambda mode, mem_type, max_count: pruned.update({"max_count": max_count}))

    asyncio.run(pp._extract_memories(_ctx(), PostProcessConfig()))
    assert pruned.get("max_count") == 500, (
        f"prune 未采用记忆设置的保留条数: {pruned}"
    )


# ── 3. NULL 标题 ──


def test_null_title_treated_as_default(monkeypatch):
    """title 为 NULL 的会话要能补出标题，不能永久停在无标题状态"""
    written: dict = {}

    async def fake_llm(**kwargs):
        return "nginx 日志查看"

    monkeypatch.setattr(pp.db, "get_conversation", lambda cid: {"id": cid, "title": None})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid, "base_url": "u", "api_key": "k"})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    monkeypatch.setattr(
        pp.db, "try_set_default_conversation_title",
        lambda cid, title, defaults: written.setdefault("title", title))

    asyncio.run(_generate_title(_ctx(turn_index=2), PostProcessConfig()))
    assert written.get("title") == "nginx 日志查看", (
        f"NULL 标题被判成用户自定义，未补生成: {written}"
    )


# ── 4. submit 读活配置 ──


def test_submit_follows_live_config_not_construction_snapshot(monkeypatch):
    """构造后关闭后处理（PUT 只换模块级 _config）→ submit 必须立即不再放行任务"""
    original = pp.get_post_process_config()
    try:
        pp.set_post_process_config(PostProcessConfig(enabled=True))
        mgr = PostProcessManager(PostProcessConfig(enabled=True))
        assert mgr.config.enabled is True

        pp.set_post_process_config(PostProcessConfig(enabled=False))
        task_id = asyncio.run(mgr.submit(
            mode="ssh", conversation_id="c1",
            user_message="q", assistant_content="a",
        ))
        assert task_id == "", "后处理已关闭，submit 仍按构造时的旧快照放行任务"
    finally:
        pp.set_post_process_config(original)


# ── 5. _extract_facts 逗号列表 ──


def test_extract_facts_keeps_whole_tool_list():
    from agent.consciousness_tick import _extract_facts

    facts = _extract_facts("任务: 排查 nginx 502 | 使用工具: shell_exec, file_grep, log_read")
    tool_facts = [f for f in facts if f.startswith("常用工具:")]
    assert tool_facts, f"未提取出常用工具: {facts}"
    assert tool_facts[0] == "常用工具: shell_exec, file_grep, log_read", (
        f"逗号列表被截断，只留下首个工具: {tool_facts[0]}"
    )
