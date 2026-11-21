"""远程后台任务注册表（agent_jobs）— P2-3 从 database.py 拆出。

终态清单的单一事实源 JOB_TERMINAL_STATUSES 也在此（ops_job 复用）。
函数体与入口签名不变。
"""
from db import _conn as _conn_mod


# ── 远程后台任务注册表（agent_jobs）──────────────────────────────
# 终态清单的单一事实源：`update_agent_job_status`（要不要落 finished_at）、
# `get_running_agent_jobs`（哪些行算"还没结束"）、`ops_job.JobManager.update_status`
# （内存侧要不要触发淘汰）三处共用同一份。此前各抄一遍字面量，而 `exited`
# 只在内存侧算终态 —— 库里它 finished_at 恒 NULL 且每次重启都被"恢复"成在跑任务。
JOB_TERMINAL_STATUSES: tuple[str, ...] = ("finished", "failed", "killed", "exited")


def save_agent_job(job_id: str, run_id: str, session_id: str, cmd: str,
                   pid: int | None, log_dir: str, status: str = "running",
                   out_offset: int = 0) -> None:
    """写入/更新一条远程后台任务记录（启动时调用）。

    UPDATE 分支必须把游标与终态字段一起重置：job_id 由进程内的自增序号分配，
    重启后从 1 重新开始，而 agent_jobs 里的**终态**行永远不会被清理
    （_ensure_restored 只捞未终结行，全仓也没有 DELETE FROM agent_jobs），
    于是新任务会复用旧任务的 job_id 撞上这一行。不重置的话
    out_offset 还是上一次读到的字节数 —— job_output 会从那个偏移之后才开始给，
    新任务最前面的输出直接找不回来；exit_code / finished_at 也会把
    「已经退出了」的旧结论挂在一条 running 的行上。
    """
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            """INSERT INTO agent_jobs
                   (job_id, run_id, session_id, cmd, pid, log_dir, status, out_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   run_id = excluded.run_id,
                   session_id = excluded.session_id,
                   cmd = excluded.cmd,
                   pid = excluded.pid,
                   log_dir = excluded.log_dir,
                   status = excluded.status,
                   out_offset = excluded.out_offset,
                   exit_code = NULL,
                   finished_at = NULL,
                   created_at = excluded.created_at""",
            (job_id, run_id, session_id, cmd, pid, log_dir, status, out_offset),
        )
        conn.commit()
    finally:
        conn.close()


def update_agent_job_status(job_id: str, status: str,
                            exit_code: int | None = None) -> None:
    """更新任务状态（结束/被杀时调用；终态时落 finished_at）。

    只动状态/退出码，不碰 out_offset（运行期由 update_agent_job_offset 维护）。
    """
    conn = _conn_mod._get_conn()
    try:
        terminal = status in JOB_TERMINAL_STATUSES
        if terminal:
            conn.execute(
                """UPDATE agent_jobs SET status = ?, exit_code = ?,
                       finished_at = datetime('now')
                   WHERE job_id = ?""",
                (status, exit_code, job_id),
            )
        else:
            conn.execute(
                "UPDATE agent_jobs SET status = ?, exit_code = ? WHERE job_id = ?",
                (status, exit_code, job_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_agent_job_offset(job_id: str, out_offset: int) -> None:
    """推进 job_output 已读日志的字节偏移（重启后恢复时避免从头重读）。"""
    conn = _conn_mod._get_conn()
    try:
        conn.execute(
            "UPDATE agent_jobs SET out_offset = ? WHERE job_id = ?",
            (out_offset, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_running_agent_jobs() -> list[dict]:
    """列出全部未终结的远程任务（backend 重启后恢复注册表用）。

    终态集合取 `JOB_TERMINAL_STATUSES`，与写侧 `update_agent_job_status` 同源 ——
    写侧认终态、这里不认，那条已判死的记录就会在每次重启时被"恢复"成在跑任务。
    `created_at` 只到秒（`_now()`），同秒并列是常态，补 `job_id` 让恢复顺序确定。
    """
    conn = _conn_mod._get_conn()
    try:
        marks = ",".join("?" for _ in JOB_TERMINAL_STATUSES)
        rows = conn.execute(
            f"""SELECT job_id, run_id, session_id, cmd, pid, log_dir, status,
                      out_offset, created_at
               FROM agent_jobs
               WHERE status NOT IN ({marks})
               ORDER BY created_at, job_id""",
            JOB_TERMINAL_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
