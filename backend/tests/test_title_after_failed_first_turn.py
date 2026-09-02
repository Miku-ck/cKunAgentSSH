# -*- coding: utf-8 -*-
"""M5 回归：首轮失败后标题门槛不得永久关闭。

修复前：_generate_title 只在 is_first_turn（turn_index==0）生成标题，但首轮
失败（user 消息已入库、无 assistant 回复）后，次轮 turn_index=1 → 门槛永久
关闭，会话名永远停在 "New Chat"。
修复后：门槛改为「标题仍是默认值」（幂等），失败轮次之后的任何一轮都能补生成。

运行: python -m pytest backend/tests/test_title_after_failed_first_turn.py -v
"""
import asyncio

import post_processor as pp
from post_processor import PostProcessConfig, TurnContext, _generate_title


def _ctx(turn_index: int) -> TurnContext:
    return TurnContext(
        mode="ssh",
        conversation_id="conv1",
        user_message="怎么查 nginx 错误日志",
        assistant_content="用 tail -f /var/log/nginx/error.log 查看",
        assistant_reasoning="",
        model_id="model-1",
        turn_index=turn_index,
        is_first_turn=(turn_index == 0),
    )


def _patch(monkeypatch, current_title: str, updated: dict, llm_result: str = "nginx 日志查看"):
    async def fake_llm(messages, model_cfg, model_id, temperature, max_tokens,
                       chunk_timeout, total_timeout, task_name):
        return llm_result

    monkeypatch.setattr(pp.db, "get_conversation",
                        lambda cid: {"id": cid, "title": current_title})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    monkeypatch.setattr(pp.db, "update_conversation_title",
                        lambda cid, title: updated.setdefault("title", title) or True)


def test_title_generated_after_failed_first_turn(monkeypatch):
    """首轮失败后次轮 turn_index=1、标题仍默认 → 必须补生成"""
    updated: dict = {}
    _patch(monkeypatch, "New Chat", updated)
    ctx = _ctx(turn_index=1)
    asyncio.run(_generate_title(ctx, PostProcessConfig()))
    assert updated.get("title") == "nginx 日志查看", (
        f"首轮失败后标题未补生成: updated={updated}"
    )


def test_first_turn_still_generates(monkeypatch):
    """正常首轮（turn_index=0）行为不变"""
    updated: dict = {}
    _patch(monkeypatch, "New Chat", updated)
    ctx = _ctx(turn_index=0)
    asyncio.run(_generate_title(ctx, PostProcessConfig()))
    assert updated.get("title") == "nginx 日志查看"


def test_custom_title_never_overwritten(monkeypatch):
    """用户自定义标题不得被后处理覆盖（幂等保护）"""
    updated: dict = {}
    llm_called: dict = {}

    async def fake_llm(messages, model_cfg, model_id, temperature, max_tokens,
                       chunk_timeout, total_timeout, task_name):
        llm_called["n"] = True
        return "不应使用的标题"

    monkeypatch.setattr(pp.db, "get_conversation",
                        lambda cid: {"id": cid, "title": "用户起的名字"})
    monkeypatch.setattr(pp.db, "get_model_config",
                        lambda mid, mask_key=False: {"id": mid})
    monkeypatch.setattr(pp, "_call_llm_with_timeout", fake_llm)
    monkeypatch.setattr(pp.db, "update_conversation_title",
                        lambda cid, title: updated.setdefault("title", title) or True)

    ctx = _ctx(turn_index=3)
    asyncio.run(_generate_title(ctx, PostProcessConfig()))
    assert not llm_called, "自定义标题存在时不应调用 LLM 生成标题"
    assert not updated, "自定义标题不得被覆盖"
