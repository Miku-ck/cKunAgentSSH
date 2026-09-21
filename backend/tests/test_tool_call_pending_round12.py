"""半截工具调用不能在轨迹回放里显示成「成功」（round 12）。

add_tool_call 只在调用**开始前**被调用（routes/agent.py 的 tool_call_start 分支），
结果由 tool_call_result 分支的 update_tool_call 回写。原默认参数
(result="", success=True) 让崩溃/中止时从未回填的那一条留下 success=1，
而 /agent/runs/{id}/trace 的投影又写 `bool(tc.get("success", 1))` 把 NULL 折成 True
⇒ 回放卡片说这条命令调用成功了，同一轮的内联快照却标它「进行中」——
一个事实两种读数（缺陷家族「同一判据写两处」+「成功形状的结果缺失」）。

现在：开始前落 NULL，投影三态透传，前端把 null 归一到 undefined 走「进行中/未回写」。
"""
from pathlib import Path

import database as db


def _fresh_db(tmp_workspace):
    original = db._db_path
    db._thread_local.__dict__.clear()
    db.init_db(str(Path(tmp_workspace) / "t.db"))
    return original


def test_pending_tool_call_row_is_null_not_success(tmp_workspace):
    original = _fresh_db(tmp_workspace)
    try:
        conv = db.create_conversation(title="c1")
        db.create_agent_run("run-p", conv["id"], "task")
        tc_id = db.add_tool_call("run-p", 0, "shell_exec", '{"command": "ls"}')

        row = db.get_tool_calls("run-p")[0]
        assert row["result"] is None, "未回填的结果不能写成空串——空串是合法结果"
        assert row["success"] is None, "未回填的成败不能默认成成功"
        assert row["id"] == tc_id

        # 回写后必须恢复成明确读数（True/False 两向都测，防止改成「只写 NULL 不写值」）
        db.update_tool_call(tc_id, "ok", True, 12)
        done = db.get_tool_calls("run-p")[0]
        assert done["result"] == "ok"
        assert bool(done["success"]) is True

        failed = db.add_tool_call("run-p", 1, "file_read", "{}")
        db.update_tool_call(failed, "boom", False)
        assert bool(db.get_tool_calls("run-p")[-1]["success"]) is False
    finally:
        db._thread_local.__dict__.clear()
        db.init_db(original)


def test_trace_endpoint_reports_unfinished_call_as_unknown(tmp_workspace):
    from routes.agent import get_agent_run_trace

    original = _fresh_db(tmp_workspace)
    try:
        conv = db.create_conversation(title="c2")
        db.create_agent_run("run-q", conv["id"], "task")
        pending = db.add_tool_call("run-q", 0, "shell_exec", "{}")
        answered = db.add_tool_call("run-q", 1, "file_read", "{}")
        db.update_tool_call(answered, "boom", False)

        payload = get_agent_run_trace("run-q")
        by_id = {tc["id"]: tc for tc in payload["tool_calls"]}
        assert by_id[pending]["success"] is None, "NULL 被折成 True → 半截调用回放成成功"
        assert by_id[pending]["result"] is None
        assert by_id[answered]["success"] is False, "明确失败必须保持 False，不能被三态化吞掉"
    finally:
        db._thread_local.__dict__.clear()
        db.init_db(original)
