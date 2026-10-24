"""第 13b 轮回归：后处理的失败语义与配置同源。

覆盖三类缺陷（均为「成功形状的失败」或「一个值两处定义」家族）：
1. auto 技能数量保护：COUNT 查询异常不得被吞成 0 后继续生成（最该挡的时候形同虚设）
2. 标题为 NULL 的会话：等价于默认标题，不能判成「用户已自定义」而永久早退
3. PostProcessManager.submit：常驻单例持有的 config 是构造时快照，
   设置面板 PUT 只换模块级 _config → 关掉后处理仍会放行任务

运行: python -m pytest backend/tests/test_post_process_failure_semantics.py -v
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
        conversation_id="conv-13b",
        user_message="怎么查 nginx 错误日志",
        assistant_content="用 tail -f /var/log/nginx/error.log 查看",
        assistant_reasoning="",
        model_id="model-1",
        turn_index=turn_index,
    )


# ── 1. auto 技能数量保护：查询失败 = 不放行 ──


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


# ── 2. NULL 标题 ──


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


# ── 2b. 标题 fallback 降级路径（两处重复块收敛为 _apply_fallback_title 后补测）──


def test_title_fallback_when_model_unknown(monkeypatch):
    """unknown model_id 分支：应落到本地规则摘要写标题，而非静默停在 New Chat。"""
    written: list = []
    monkeypatch.setattr(pp.db, "get_conversation",
                        lambda cid: {"id": cid, "title": "New Chat"})
    monkeypatch.setattr(pp.db, "get_model_config", lambda mid, mask_key=False: None)
    monkeypatch.setattr(
        pp.db, "try_set_default_conversation_title",
        lambda cid, title, defaults: (written.append(title), True)[1])

    asyncio.run(_generate_title(_ctx(turn_index=2), PostProcessConfig()))
    assert len(written) == 1 and written[0], (
        f"模型不可用时未走 fallback 写标题: {written}")


def test_title_fallback_when_llm_returns_empty(monkeypatch):
    """LLM 返回空文本分支：同样必须落到 fallback，与上面收敛到同一 helper。"""
    written: list = []

    async def fake_llm(**kwargs):
        return ""

    monkeypatch.setattr(pp.db, "get_conversation", lambda cid: {"id": cid, "title": ""})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid, "base_url": "u", "api_key": "k"})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    monkeypatch.setattr(
        pp.db, "try_set_default_conversation_title",
        lambda cid, title, defaults: (written.append(title), True)[1])

    asyncio.run(_generate_title(_ctx(turn_index=2), PostProcessConfig()))
    assert len(written) == 1 and written[0], (
        f"LLM 空返回时未走 fallback 写标题: {written}")


def test_apply_fallback_title_respects_windowed_rename(monkeypatch):
    """条件写返回 False（窗口期用户已改名）：helper 不得抛，只放弃覆盖。"""
    seen: list = []
    monkeypatch.setattr(
        pp.db, "try_set_default_conversation_title",
        lambda cid, title, defaults: (seen.append(title), False)[1])
    asyncio.run(pp._apply_fallback_title(_ctx()))
    assert len(seen) == 1 and seen[0], "helper 应先算标题再尝试条件写"


def test_apply_fallback_title_swallows_db_error(monkeypatch):
    """fallback 写库异常必须被吞（后处理不得冒泡打断主流程）。"""
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(pp.db, "try_set_default_conversation_title", boom)
    asyncio.run(pp._apply_fallback_title(_ctx()))  # 不抛即通过


# ── 3. submit 读活配置 ──


def test_submit_follows_live_config_not_construction_snapshot(monkeypatch):
    """构造后关闭后处理（PUT 只换模块级 _config）→ submit 必须立即不再放行任务"""
    original = pp.get_post_process_config()
    try:
        pp.set_post_process_config(PostProcessConfig(enabled=True))
        mgr = PostProcessManager(PostProcessConfig(enabled=True))
        assert mgr.config.enabled is True

        pp.set_post_process_config(PostProcessConfig(enabled=False))
        task_id = asyncio.run(mgr.submit(
            conversation_id="c1",
            user_message="q", assistant_content="a",
        ))
        assert task_id == "", "后处理已关闭，submit 仍按构造时的旧快照放行任务"
    finally:
        pp.set_post_process_config(original)
