"""agent_jobs 复用行的残留状态 + 旧库补列顺序。

两处都是「一次脏状态把功能永久埋掉」的形态，且都不报错：
  - save_agent_job 的 upsert 只覆盖 status，不重置 out_offset / exit_code /
    finished_at。job_id 由进程内自增序号分配，重启后从 1 重新开始，而终态行
    永不清理，于是新任务会复用旧任务的 job_id 撞上这一行：游标还是上一次读到
    的字节数（新任务最前面的输出找不回来），exit_code/finished_at 把「已经退出」
    的旧结论挂一条 running 的行上。
  - _fix_model_name_and_id 自己 SELECT id, name, model_id，而 name 正是补列循环
    里的一项。原先它在循环之前跑 —— 一个没有 name 列的旧库会在 init_db 里抛
    OperationalError，init_db 只有 try/finally、main.py 也不接，结果是整个后端
    起不来（不是某个接口 500）。
"""
import sqlite3

import pytest

import database as db


@pytest.fixture
def job_db(tmp_workspace):
    import os

    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "job_carryover.db"))
    yield
    db._close_all_connections()


def _row(job_id: str):
    conn = db._get_conn()
    try:
        return conn.execute(
            "SELECT status, out_offset, exit_code, finished_at, run_id "
            "FROM agent_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def test_upsert_resets_state_carried_over_by_terminal_row(job_db):
    """重启后 job_id 归零复用：新任务不能继承上一轮的游标与终态"""
    db.save_agent_job("1", "run-old", "sess-1", "sleep 1", 111, "/logs/1", "running", 0)
    db.update_agent_job_offset("1", 4096)
    db.update_agent_job_status("1", "finished", exit_code=0)

    status, out_offset, exit_code, finished_at, run_id = _row("1")
    assert (status, out_offset, exit_code) == ("finished", 4096, 0)

    db.save_agent_job("1", "run-new", "sess-1", "sleep 2", 222, "/logs/1", "running", 0)

    status, out_offset, exit_code, finished_at, run_id = _row("1")
    assert status == "running", status
    assert run_id == "run-new", run_id
    # 旧实现：这三项原封不动留着上一轮的样子 —— job_output 从 4096 起给，
    # 新任务前 4096 字节直接丢失；finished_at 让前端按「已结束」渲染一条在跑的任务
    assert out_offset == 0, f"out_offset 残留 {out_offset}"
    assert exit_code is None, f"exit_code 残留 {exit_code}"
    assert finished_at is None, f"finished_at 残留 {finished_at}"


def test_fresh_row_writes_what_it_is_told(job_db):
    """反向锁：重置只针对复用行的残留，正常首写仍按调用方给的 offset 落盘"""
    db.save_agent_job("7", "run-7", "sess-1", "sleep 1", 777, "/logs/7", "running", 128)
    assert _row("7")[1] == 128


def test_migrate_survives_db_without_name_column(tmp_workspace):
    """旧库缺 name 列时补列必须先于读取该列的修正步骤（原顺序整库起不来）

    用真 schema 退化出来的库，而不是手搓一张表：失败发生在 init_db 里，
    init_db 只有 try/finally、main.py 也不接，抛出来就是整个后端不启动。
    """
    import os

    db_path = os.path.join(tmp_workspace, "legacy_models.db")
    db._close_all_connections()
    db.init_db(db_path)
    db.create_model_config(
        name="DeepSeek", base_url="https://api.deepseek.com/v1", api_key="sk-x",
        model_id="deepseek-chat", provider="deepseek")

    conn = sqlite3.connect(db_path)
    for col in ("name", "reasoning_tokens", "top_p", "sort_order"):
        conn.execute(f"ALTER TABLE models_config DROP COLUMN {col}")
    conn.commit()
    conn.close()
    db._close_all_connections()

    db.init_db(db_path)  # 旧顺序在这里抛 OperationalError: no such column: name

    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(models_config)")}
    conn.close()
    assert {"name", "reasoning_tokens", "top_p", "sort_order"} <= cols, sorted(cols)
    # 补完列必须还能按现列集读出来（SELECT 列清单与补列结果一致）
    assert db.list_models_config(), "迁移后模型列表不能为空"
    db._close_all_connections()


def test_exited_是终态_finished_at_要落且不再被恢复(job_db):
    """ops_job 的对账把"进程组已消失但没留下 rc"落成状态 `exited`。

    库侧原先只认 ('finished','failed','killed') 三个字面量：`exited` 行既不落
    finished_at（前端按「已结束」渲染时拿不到结束时间），又被 get_running_agent_jobs
    当未终结任务返回 —— 每次后端重启都把这条已经判死的记录"恢复"成在跑任务，
    再对账一轮、再写一次 exited，永不收敛。
    """
    db.save_agent_job("e1", "run-1", "sess-1", "sleep 1", 11, "/logs/e1", "running", 0)
    db.update_agent_job_status("e1", "exited", None)

    status, _offset, _code, finished_at, _run = _row("e1")
    assert status == "exited"
    assert finished_at, "exited 是终态：finished_at 不能留 NULL"

    restored = {r["job_id"] for r in db.get_running_agent_jobs()}
    assert "e1" not in restored, f"未终结查询把终态行又捞回来了：{restored}"
    # 反向锁：真在跑的任务必须仍在恢复清单里，否则重启就丢任务
    db.save_agent_job("r1", "run-1", "sess-1", "sleep 2", 22, "/logs/r1", "running", 0)
    assert "r1" in {r["job_id"] for r in db.get_running_agent_jobs()}
