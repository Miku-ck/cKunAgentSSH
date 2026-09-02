# -*- coding: utf-8 -*-
"""回归测试：LLM 流停顿 > HEARTBEAT_INTERVAL 时不得静默截断（审查 C-review M1）。

缺陷路径（已复现）：原实现 asyncio.wait_for(chunk_iter.__anext__(), timeout)
超时会取消 __anext__ 任务，CancelledError 抛进 async generator 帧将其永久关闭，
下一轮 __anext__ 立即 StopAsyncIteration → 半截内容按"正常完成"入库。
修复：chunk 任务 ensure_future + shield 保活，超时只发心跳。
"""
import asyncio
import json
from types import SimpleNamespace

import routes.chat as chat_module


def _run_stream(monkeypatch, pause_seconds: float, heartbeat: float = 0.3):
    """跑一次 _generate_stream，返回 (内容块列表, 心跳数, 状态更新序列)"""
    monkeypatch.setattr(chat_module, "HEARTBEAT_INTERVAL", heartbeat)

    statuses: list[str] = []

    async def fake_chat_stream(**kwargs):
        yield SimpleNamespace(content="part1", reasoning=None)
        await asyncio.sleep(pause_seconds)  # 模拟 prefill/思考/GPU 忙停顿
        yield SimpleNamespace(content="part2", reasoning=None)

    def fake_add_message(conv_id, role, content, reasoning=None, model_used=None, **kw):
        return 1

    def fake_update_status(conv_id, status):
        statuses.append(status)

    class _PPStub:
        async def submit(self, **kw):
            return None

    monkeypatch.setattr(chat_module, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(chat_module, "db_get_model_config", lambda mid: {"reasoning_tokens": None})
    monkeypatch.setattr(chat_module.db, "add_message", fake_add_message)
    monkeypatch.setattr(chat_module.db, "touch_conversation", lambda c: None)
    monkeypatch.setattr(chat_module.db, "update_conversation_status", fake_update_status)
    monkeypatch.setattr(chat_module.db, "count_messages_by_role", lambda c, r: 1)
    monkeypatch.setattr(chat_module.db, "get_conversation", lambda c: None)
    import post_processor
    monkeypatch.setattr(
        post_processor, "get_post_process_manager", lambda: _PPStub(),
    )

    async def main():
        contents: list[str] = []
        heartbeats = 0
        gen = chat_module._generate_stream(
            base_url="http://x", api_key="k", model="m", messages=[],
            conversation_id="c1", user_message="hi", model_id="mid",
            mode="ssh", enable_thinking=False, request=None,
        )
        async for item in gen:
            if item == ": heartbeat\n\n":
                heartbeats += 1
            elif item == "data: [DONE]\n\n":
                continue
            else:
                data = json.loads(item[len("data: "):])
                if data.get("content"):
                    contents.append(data["content"])
                if data.get("error"):
                    raise AssertionError(f"流中出现错误事件: {data['error']}")
        return contents, heartbeats

    return *asyncio.run(main()), statuses


def test_pause_longer_than_heartbeat_keeps_full_stream(monkeypatch):
    """停顿（0.8s）> 心跳间隔（0.3s）：必须收到全部 2 个块，且状态 completed"""
    contents, heartbeats, statuses = _run_stream(
        monkeypatch, pause_seconds=0.8, heartbeat=0.3,
    )
    assert contents == ["part1", "part2"], f"流被截断: {contents}"
    assert heartbeats >= 1, "停顿期间应至少发出 1 个心跳"
    assert statuses[-1] == "completed"


def test_normal_stream_unaffected(monkeypatch):
    """无停顿的快流不受 shield 改动影响（无心跳、全量内容、completed）"""
    contents, heartbeats, statuses = _run_stream(monkeypatch, pause_seconds=0.0)
    assert contents == ["part1", "part2"]
    assert heartbeats == 0
    assert statuses[-1] == "completed"
