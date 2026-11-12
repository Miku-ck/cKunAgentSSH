"""定时任务（schedules）与执行历史（schedule_runs）CRUD — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
prune_agent_runs / prune_conversations 等跨域清理维护暂留 database.py，待对应域迁移时一并处理。
"""
from typing import Optional

from db._conn import _get_conn
from db._util import _new_id, _now

_SCHEDULE_COLS = (
    "id, name, task_type, expert_slug, schedule_type, interval_minutes, time_hour, time_minute, "
    "weekday, day_of_month, target_host, task_prompt, enabled, next_run_at, "
    "last_run_at, last_status, last_run_id, last_abnormal_at, created_at, updated_at"
)


def list_schedules() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_schedule(schedule_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_enabled_schedules() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_schedule(data: dict) -> dict:
    now = _now()
    sid = data.get("id") or _new_id()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO schedules (
                id, name, task_type, expert_slug, schedule_type, interval_minutes, time_hour, time_minute,
                weekday, day_of_month, target_host, task_prompt, enabled,
                next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid,
                (data.get("name") or "").strip(),
                # spec 2026-10-27-schedule-task-type：分类缺省归运维；update_schedule 的
                # allowed 集不含 task_type，故类型创建后不可变（即便 patch 带也被丢弃）。
                data.get("task_type") or "ops",
                # spec §6.2：专家绑定缺省归通用；存量旧库由迁移一次性回填 general_ops。
                data.get("expert_slug") or "general",
                data.get("schedule_type") or "interval",
                int(data.get("interval_minutes") or 60),
                # None 判定而非 `or`：0（午夜）是合法 time_hour，`0 or 9` 会把每日/每周/
                # 每月任务静默挪到 09:00（与 compute_next_run 同源修复，勿回退成 or）。
                int(data["time_hour"]) if data.get("time_hour") is not None else 9,
                int(data.get("time_minute") or 0),
                int(data.get("weekday") or 0),
                int(data.get("day_of_month") or 1),
                data.get("target_host") or "",
                data.get("task_prompt") or "",
                1 if data.get("enabled", True) else 0,
                data.get("next_run_at"),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_schedule(sid)


def update_schedule(schedule_id: str, fields: dict) -> Optional[dict]:
    now = _now()
    allowed = {
        "name", "schedule_type", "interval_minutes", "time_hour", "time_minute",
        "weekday", "day_of_month", "target_host", "task_prompt", "enabled",
        "expert_slug",
    }
    cols, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        cols.append(f"{k} = ?")
        vals.append(v)
    if not cols:
        return get_schedule(schedule_id)
    cols.append("updated_at = ?")
    vals.append(now)
    vals.append(schedule_id)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE schedules SET {', '.join(cols)} WHERE id = ?", vals
        )
        conn.commit()
    finally:
        conn.close()
    return get_schedule(schedule_id)


def set_schedule_next_run(schedule_id: str, next_run_at: Optional[str]) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, _now(), schedule_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_schedule_run(
    schedule_id: str,
    status: str,
    last_run_at: Optional[str] = None,
    last_run_id: str = "",
) -> None:
    """回写调度行的上次执行状态：一次调用只发一条 UPDATE。

    原先是三条 —— 同一行、同一事务、三个写操作加三次索引维护，而 updated_at 只写在
    第一条里。`last_run_at` / `last_run_id` 的「不传或传空串 = 保持原值」是调用方唯一
    在用的形态（schedule_engine.py:324/340/349 收尾那几支只带状态过来），合并成一条时
    必须原样保留，否则面板「上次执行」那一列每次跑完都被抹空。
    """
    sets, params = ["last_status = ?", "updated_at = ?"], [status, _now()]
    if last_run_at:
        sets.append("last_run_at = ?")
        params.append(last_run_at)
    if last_run_id:
        sets.append("last_run_id = ?")
        params.append(last_run_id)
    params.append(schedule_id)
    conn = _get_conn()
    try:
        # 拼进 SQL 的只有上面这几个字面量，值一律走占位符
        conn.execute(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", params
        )
        conn.commit()
    finally:
        conn.close()


def set_schedule_last_abnormal(schedule_id: str, ts: str) -> None:
    """记录巡检异常沉淀时间（方案 Phase 3）。只在判读为异常时调用，正常巡检零痕迹。"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE schedules SET last_abnormal_at = ?, updated_at = ? WHERE id = ?",
            (ts, _now(), schedule_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_schedule(schedule_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def create_schedule_run(schedule_id: str, run_id: str) -> dict:
    rid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO schedule_runs (id, schedule_id, run_id, status, started_at, created_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (rid, schedule_id, run_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": rid, "schedule_id": schedule_id, "run_id": run_id,
        "status": "running", "summary": "", "error": "",
        "started_at": now, "finished_at": None, "created_at": now,
    }


def update_schedule_run(
    run_key: str,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """run_key 既可能是 schedule_runs.id 也可能是 harness run_id"""
    cols, vals = [], []
    if status is not None:
        cols.append("status = ?")
        vals.append(status)
        if status in ("completed", "failed"):
            cols.append("finished_at = ?")
            vals.append(_now())
    if summary is not None:
        cols.append("summary = ?")
        vals.append(summary)
    if error is not None:
        cols.append("error = ?")
        vals.append(error)
    if not cols:
        return
    vals.append(run_key)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE schedule_runs SET {', '.join(cols)} WHERE id = ? OR run_id = ?",
            vals + [run_key],
        )
        conn.commit()
    finally:
        conn.close()


def fail_running_schedule_runs(schedule_id: str, error: str) -> int:
    """把某任务所有还在 running 的执行历史标为 failed，返回受影响行数。"""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE schedule_runs SET status = 'failed', error = ?, finished_at = ?
               WHERE schedule_id = ? AND status = 'running'""",
            (error, _now(), schedule_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_schedule_runs(schedule_id: str, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT id, schedule_id, run_id, status, summary, error,
                      started_at, finished_at, created_at
               FROM schedule_runs WHERE schedule_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (schedule_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def prune_schedule_runs(max_count: int = 2000) -> int:
    """删除最旧的 schedule_runs，每个 schedule 各保留最近 max_count 条，
    返回删除数量。

    每次到期触发都会插一行（interval 任务 1 分钟一次 = 一天 1440 行），
    不清理会随运行时间无界增长。由调度引擎低频调用（每 6h），不放在
    热路径上。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM schedule_runs WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY schedule_id
                                  ORDER BY created_at DESC
                              ) AS rn
                       FROM schedule_runs
                   ) WHERE rn > ?
               )""",
            (max_count,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_schedule_runs(schedule_id: str) -> int:
    """清空某任务的全部执行历史（保留任务本身），返回删除行数。

    与 delete_schedule 的区分：那条删整实体、由 FK CASCADE 顺带带走历史；本函数
    只清历史列表行。不做 running 检查（由路由层的 has_running_schedule_run 闸把关，
    避免删掉仍在写回的执行记录）。SQL 静态字面量 + 参数绑定。
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id = ?",
            (schedule_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def has_running_schedule_run(schedule_id: str) -> bool:
    """该任务是否有仍处 running 的执行历史（清空前的进行中闸）。

    与 fail_running_schedule_runs 同一非终态口径：schedule_runs 只有 running 是非终态
    （completed/failed 为终态）。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM schedule_runs WHERE schedule_id = ? AND status = 'running' LIMIT 1",
            (schedule_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()
