"""run_checkpoints 存储层 — 挂起/续跑的持久化载体。

守卫的是「恢复能不能成立」这件事本身，而不是 CRUD 覆盖率：
- 往返：四个 JSON 列必须原样回来（messages 顺序/结构不能被动过）；
- 二进制剥离：内联 base64 图片必须换成占位文案，且**不修改调用方入参**；
- 截断：保留最近 N 条，且**绝不留孤儿 tool 消息**（assistant.tool_calls 与
  role=tool 的配对是 vLLM / OpenAI 的硬约束，断了就是 400）；
- set_pending({}) 是「清除」而不是「不修改」；
- list_waiting_checkpoints 只捞「有待确认项」的行。
"""
from pathlib import Path

import pytest

import db as dbpkg
import db._conn as _conn_mod
from db import run_checkpoints as ck


@pytest.fixture
def ck_db(tmp_workspace):
    """独立库 + 连接池切换（与 test_run_history_list.runs_db 同法）。

    db 包的连接池是 keep-alive：只改路径不换连接，第二个用例起会继续读上一个库。
    """
    original = _conn_mod._db_path
    path = Path(tmp_workspace) / "run-checkpoints.db"
    dbpkg._close_all_connections()
    dbpkg.init_db(str(path))
    ck._reset_memo_for_tests()
    try:
        yield path
    finally:
        dbpkg._close_all_connections()
        if original:
            dbpkg.init_db(original)
        dbpkg._close_all_connections()
        ck._reset_memo_for_tests()


def _pair(index: int) -> list[dict]:
    """一组合法的 OpenAI 工具配对：assistant(tool_calls) + 紧随其后的 tool 结果。"""
    call_id = f"call_{index}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "shell_exec", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "ok"},
    ]


def _assert_pairs_consistent(messages: list[dict]) -> None:
    """每条 tool 消息都必须有一个仍在前面的 owner assistant（否则就是孤儿）。"""
    live_ids: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            live_ids = [tc["id"] for tc in (message.get("tool_calls") or [])]
        elif message.get("role") == "tool":
            assert message.get("tool_call_id") in live_ids, (
                f"孤儿 tool 消息（owner 已被裁掉）: {message.get('tool_call_id')}"
            )


def test_save_load_roundtrip(ck_db):
    ck.save_checkpoint(
        "run-1",
        conv_id="conv-1",
        messages=[{"role": "user", "content": "清理临时日志"}],
        harness={"model": "m-1", "permission_mode": "normal", "max_iterations": 100},
        pending={"kind": "approval", "tool": "shell_exec", "command": "rm -rf /tmp/x"},
        partial={"content": "正在检查目录", "tool_calls": []},
        iteration=3,
    )

    data = ck.load_checkpoint("run-1")
    assert data is not None
    assert data["conv_id"] == "conv-1"
    assert data["messages"] == [{"role": "user", "content": "清理临时日志"}]
    assert data["harness"]["permission_mode"] == "normal"
    assert data["pending"]["command"] == "rm -rf /tmp/x"
    assert data["partial"]["content"] == "正在检查目录"
    assert data["iteration"] == 3
    assert data["created_at"] and data["updated_at"]
    # 不存在必须返回 None（恢复入口据此判断「这个 run 没有可续上下文」）
    assert ck.load_checkpoint("run-missing") is None


def test_binary_stripped_without_mutating_input(ck_db):
    original = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
        },
        {
            "role": "user",
            "content": [{"type": "image", "source": {"type": "base64", "data": "AAAABBBB"}}],
        },
    ]
    ck.save_checkpoint("run-2", messages=original)

    loaded = ck.load_checkpoint("run-2")["messages"]
    # data URI → 占位；http(s) 地址只有几十字节，保留原样
    assert loaded[0]["content"][1] == {"type": "text", "text": ck.BINARY_PLACEHOLDER}
    assert loaded[0]["content"][0] == {"type": "text", "text": "看这张图"}
    assert loaded[1]["content"][0]["image_url"]["url"] == "https://example.com/a.png"
    assert loaded[2]["content"][0] == {"type": "text", "text": ck.BINARY_PLACEHOLDER}

    # 入参不可被就地改写（调用方还要拿原对象继续跑这一轮）
    assert original[0]["content"][1]["image_url"]["url"].startswith("data:")


def test_truncate_keeps_tool_pairs(ck_db):
    # 3 条前导 + 15 组配对 = 33 条。组 i 的 assistant 落在 3+2i、tool 落在 4+2i：
    # 保留 20 条 → 丢弃前 13 条 → 切点恰在 assistant-5（33-20=13），无孤儿。
    messages = [{"role": "user", "content": f"msg-{i}"} for i in range(3)]
    for i in range(15):
        messages.extend(_pair(i))

    out = ck._truncate_messages(messages)

    assert len(out) == 20, "切点落在 assistant 上时不产生孤儿，整 20 条保留"
    assert out[0]["role"] == "assistant", "裁头后首条不能是 tool 消息"
    _assert_pairs_consistent(out)

    # 孤儿路径：保留 21 条 → 丢弃前 12 条 → 切点落在 tool-4 上，
    # 这条孤儿 tool 的 owner（assistant-4）已被裁掉 → 必须整条丢弃。
    out2 = ck._truncate_messages(messages, max_count=21)
    assert len(out2) == 20, "孤儿 tool 被整条丢弃后回到 20 条"
    assert out2[0]["role"] == "assistant"
    assert out2[0].get("tool_calls"), "丢弃孤儿后首条必须是带 tool_calls 的 assistant"
    _assert_pairs_consistent(out2)

    # 单条超限：只收缩 content，配对结构（tool_call_id）必须留着。
    # 注：单独一条 tool 消息（无 owner）是生产中不存在的输入 —— ctx.messages
    # 首条永远是 user/assistant，孤儿兜底会把它清掉；这里按真实形状成对构造。
    big_pair = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_big", "type": "function",
            "function": {"name": "shell_exec", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_big", "content": "x" * 200_000},
    ]
    ck.save_checkpoint("run-3", messages=big_pair)
    loaded = ck.load_checkpoint("run-3")["messages"]
    assert len(loaded) == 2, "超限只收缩 content，配对消息不允许被丢"
    assert len(ck._dumps(loaded[1]).encode("utf-8")) <= ck.MAX_MESSAGE_BYTES
    assert ck._TRUNCATED_SUFFIX in loaded[1]["content"]
    assert loaded[1]["tool_call_id"] == "call_big"
    assert loaded[0]["tool_calls"][0]["id"] == "call_big"


def test_set_pending_empty_clears(ck_db):
    ck.save_checkpoint("run-4", pending={"kind": "clarification", "tool": "file_write"})
    assert ck.load_checkpoint("run-4")["pending"]["kind"] == "clarification"

    # 空 dict = 清除待确认项（不是「不修改」）
    ck.set_pending("run-4", {})
    assert ck.load_checkpoint("run-4")["pending"] == {}
    assert ck.list_waiting_checkpoints() == []


def test_list_waiting_only_returns_pending(ck_db):
    ck.save_checkpoint(
        "wait-1", conv_id="conv-9",
        pending={"kind": "approval", "tool": "shell_exec", "command": "rm -rf /tmp/x"},
    )
    ck.save_checkpoint("wait-2", pending={})
    ck.save_checkpoint("wait-3")  # 未传 pending → 默认 '{}'

    rows = ck.list_waiting_checkpoints()
    assert [row["run_id"] for row in rows] == ["wait-1"]
    assert rows[0]["conv_id"] == "conv-9"


def test_update_messages_and_clear(ck_db):
    ck.save_checkpoint("run-5", messages=[{"role": "user", "content": "a"}], iteration=1)

    # iteration 不传 = 保持原值（COALESCE），传了才改
    ck.update_messages("run-5", [{"role": "user", "content": "b"}])
    data = ck.load_checkpoint("run-5")
    assert data["messages"][-1]["content"] == "b"
    assert data["iteration"] == 1

    ck.update_messages("run-5", [{"role": "user", "content": "c"}], 7)
    assert ck.load_checkpoint("run-5")["iteration"] == 7

    ck.clear_checkpoint("run-5")
    assert ck.load_checkpoint("run-5") is None
    ck.clear_checkpoint("run-5")  # 幂等：重复清理不抛

    # 行不存在时 update / set_pending 是 no-op，不该凭空插行
    ck.update_messages("run-5", [{"role": "user", "content": "d"}])
    ck.set_pending("run-5", {"kind": "approval"})
    assert ck.load_checkpoint("run-5") is None
