"""后端 API/DB 级冒烟测试：会话锁与启动对账（临时库，不碰真实数据）"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db

failures = []


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="ckun-test-")
    db.init_db(os.path.join(tmp, "test.db"))

    # 1. create_conversation 基础
    conv = db.create_conversation(title="测试会话", mode="ssh")
    cid = conv["id"]
    check("create_conversation 返回 id", bool(cid))
    check("新会话 status=completed", conv.get("status") == "completed")

    # 2. 滞留 in_progress 的启动对账复位（直接 DB 置 in_progress 模拟崩溃残留）
    conn = db._get_conn()
    try:
        conn.execute("UPDATE conversations SET status='in_progress' WHERE id=?", (cid,))
        conn.commit()
    finally:
        conn.close()
    n = db.reset_stale_in_progress_conversations()
    check("reset_stale_in_progress_conversations 复位 1 条", n == 1)
    cur = db.get_conversation(cid)
    check("复位后 status=completed", cur.get("status") == "completed")

    # 4. mark_stale_agent_runs_failed 对 agent_runs 的对账
    now = db._now()
    conn = db._get_conn()
    try:
        rid = "test-run-1"
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, task, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (rid, cid, "测试任务", "running", now),
        )
        conn.commit()
    finally:
        conn.close()
    n = db.mark_stale_agent_runs_failed()
    check("mark_stale_agent_runs_failed 标记滞留 run", n >= 1)
    run = db.get_agent_run("test-run-1")
    check("滞留 run 已置 failed", run.get("status") == "failed")

    # 5. has_active_run 新语义：注册表为唯一权威，注册表外滞留行对账为 failed
    from routes.agent import has_active_run, _in_flight_runs

    rid2 = "test-run-stale"
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_runs (id, conversation_id, task, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (rid2, cid, "滞留任务", "running", now),
        )
        conn.commit()
    finally:
        conn.close()
    check("注册表外滞留 run 判定不活跃", has_active_run(cid) is False)
    run2 = db.get_agent_run(rid2)
    check("注册表外滞留 run 已对账为 failed", run2.get("status") == "failed")

    _in_flight_runs[cid] = "fake-run"
    try:
        check("注册表有占位判定活跃", has_active_run(cid) is True)
    finally:
        _in_flight_runs.pop(cid, None)

    print()
    if failures:
        print(f"结果: {len(failures)} 项失败 -> {failures}")
        sys.exit(1)
    print("结果: 全部通过")


if __name__ == "__main__":
    main()
