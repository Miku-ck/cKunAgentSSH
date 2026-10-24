"""后台任务管理工具集 — 本地子进程 + 远程 nohup/日志/PID。SSH 模式专属。

包含四个 BaseTool 子类：
  - job_run    后台启动长驻/跟随任务，返回 job_id
               target=local  → 本地子进程（参数列表执行，无 shell）
               target=remote → 远程 SSH 会话：脚本落脚本 + setsid 脱离会话
                               后台运行，stdout 落日志文件，回传进程组 PID
  - job_list   列出所有后台任务（远程任务重启后从 agent_jobs 表恢复并对账）
  - job_output 读取任务输出（本地=内存缓冲；远程=增量 tail 日志 + 退出码文件）
  - job_kill   终止任务（本地=TERM→KILL 升级；远程=整进程组 TERM→KILL 升级）

远程实现要点（方案 A：桥接零协议变更）：
  1. cmd 经 base64 解码落脚本 run.sh，彻底规避引号/注入问题
     （与 ops_file 远程 base64 通道同一模式）。
  2. setsid 使 wrapper 成为新进程组 leader（$! 即组号），
     job_kill 用 `kill -- -<pid>` 整组终结，防孤儿子孙进程。
  3. 退出码由 run.sh 在命令结束后写入 rc 文件；日志/脚本/rc 全部在
     /tmp/ckun_agent_jobs/<job_id>/ 下，关 app 任务继续跑、日志留痕。
  4. 注册表持久化到 agent_jobs 表 — backend 重启后 job_list 恢复、
     对账（kill -0）并可继续 kill。

安全约束：
  1. 本地分支一律参数列表创建子进程（create_subprocess_exec），
     严禁 shell=True。
  2. 远程分支 fail-closed：缺 SSH 会话或 Bridge 未就绪直接拒绝
     （复用 ops_common.run_headless，不含任何本地降级）。
  3. 内部命令的审批/安全分类由 FilesystemGuard 按 `cmd` 参数精细判定
     （job_run 已列入 _COMMAND_EXEC_TOOLS），wrapper 不参与分类。
"""
import asyncio
import base64
import calendar
import shlex
import threading
import time
import types
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import parse_int_arg, run_headless, truncate_output
from agent.remote_protocol import EXIT_CODE_PREFIX, is_exit_code_line, remote_parts

# 远程任务落盘根目录（目标机 /tmp 下，重启任务与日志的持久位置）
_JOBS_ROOT = "/tmp/ckun_agent_jobs"

# 内存上限（长驻后端防无界增长）：终态 job 最多保留的条数，超出淘汰最旧的
# （_evict_terminal_locked，只动终态、running 恒保留）；本地 job 输出缓冲上限
# （字符数，近似字节）——超限丢弃头部保留最近内容（见 _read_stream）。
# 后端是长驻服务，job 记录只增不删会随会话数线性膨胀。
_MAX_TERMINAL_JOBS = 100
_LOCAL_OUTPUT_CAP = 1 << 20

# job_run / job_kill 会启动并终止外部进程，具备中等影响，标记 moderate 不可幂等。
_RUN_META = ToolMeta(
    risk_level="moderate",
    reversible=False,
    idempotent=False,
    timeout=30.0,
)
# job_list / job_output 只读不改变状态，安全且可重复执行。
_READ_META = ToolMeta(
    risk_level="safe",
    reversible=True,
    idempotent=True,
    timeout=15.0,
)


@dataclass
class JobEntry:
    """单个后台任务的运行时记录（本地/远程双形态）。"""
    job_id: str
    args: List[str]
    start_ts: float
    proc: Optional[asyncio.subprocess.Process] = None   # 本地形态
    output: Deque[str] = field(default_factory=lambda: deque())  # 本地输出缓冲
    output_bytes: int = 0            # output 当前字节总量（增量维护，避免每行全量重算）
    status: str = "running"          # running / finished / failed / killed / exited
    exit_code: Optional[int] = None
    reader_task: Optional[asyncio.Task] = None
    # ── 远程形态 ──
    remote: bool = False
    ssh_session_id: str = ""
    pid: Optional[int] = None        # setsid 进程组 leader PID
    log_dir: str = ""                # /tmp/ckun_agent_jobs/<job_id>
    run_id: str = ""                 # 所属 agent run（取消联动清理用）
    raw_cmd: str = ""                # 原始命令文本（展示用）
    out_offset: int = 0              # 已读取的日志字节数（增量 tail）
    # 审查R2修复：per-job 输出读锁，串行化「读 offset→await 拉取→推进 offset」
    # （见 _remote_read 的并发说明）。惰性创建，不进构造参数以免调用点跟改。
    read_lock: Optional["asyncio.Lock"] = None


class JobManager:
    """模块级单例：管理本地/远程后台任务。

    线程安全（threading.Lock 保护 job 字典）。本地 job 仅存内存；
    远程 job 持久化到 agent_jobs 表，进程重启后由 _ensure_restored
    从 DB 恢复到内存（状态以 kill -0 对账为准）。
    """

    _instance: Optional["JobManager"] = None
    _instance_lock = threading.Lock()
    _seq_lock = threading.Lock()
    _seq = 0
    _restored = False

    def __init__(self):
        self._jobs: dict[str, JobEntry] = {}
        self._reserved: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "JobManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _next_id(self) -> str:
        # 进程重启后 _seq 归零，而 _ensure_restored 会把 agent_jobs 里未终结的
        # job-1…job-N 拉回内存：不跳过已占用的号就会撞号 —— add() 覆盖仍在跑的
        # 条目、save_agent_job 的 upsert 覆盖 DB 行、远程 log_dir 指向同一目录，
        # 原任务的输出与 kill 通道一起丢掉。
        with self._seq_lock:
            while True:
                self._seq += 1
                candidate = str(self._seq)
                if self.get(candidate) is None and candidate not in self._reserved:
                    # S6-02：号一经发出立刻占坑。原实现等到 await 远程启动（最长 20s）
                    # 之后才 add()，第二个并发 job_run 看到同号空闲 ⇒ 两个作业撞进同一个
                    # <jobs_root>/<id>：启动模板的 `>` 截断前一份 out.log、覆写 run.sh/rc，
                    # 且随后的 mgr.add 替换掉第一个条目 —— 前一个作业的 pid 句柄与 kill
                    # 通道一起丢失。启动失败时坑位不回收（只是跳号，无害）。
                    self._reserved.add(candidate)
                    return candidate

    def add(self, entry: JobEntry) -> None:
        with self._lock:
            self._reserved.discard(entry.job_id)
            self._jobs[entry.job_id] = entry

    def get(self, job_id: str) -> Optional[JobEntry]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[JobEntry]:
        with self._lock:
            return list(self._jobs.values())

    def update_status(self, job_id: str, status: str, exit_code: Optional[int]) -> None:
        with self._lock:
            entry = self._jobs.get(job_id)
            if entry is None:
                return
            # 竞态防护：已标记 killed 的 job 不再被 reader/对账降级
            if entry.status == "killed":
                entry.exit_code = exit_code
                return
            entry.status = status
            entry.exit_code = exit_code
            # 终态清单取 database.JOB_TERMINAL_STATUSES（单一事实源，惰性导入与
            # _persist_status 同一口径）：内存判终态、库侧判落 finished_at、恢复查询判
            # "还没结束"的三处必须同一个答案，此前各抄一份，`exited` 只在前一处算终态。
            import database as db
            if status in db.JOB_TERMINAL_STATUSES:
                self._evict_terminal_locked()
        if entry.remote:
            _persist_status(entry.job_id, status, exit_code)

    def _evict_terminal_locked(self) -> None:
        """终态 job 超出上限时淘汰最旧的（调用方须持有 ``_lock``）。

        只动终态（非 running）记录：运行中任务必须保留供 kill/对账；
        淘汰仅影响 job_list 的展示与 job_output 的可读性（job 已终结）。
        """
        terminal = [e for e in self._jobs.values() if e.status != "running"]
        if len(terminal) <= _MAX_TERMINAL_JOBS:
            return
        terminal.sort(key=lambda e: e.start_ts)
        for e in terminal[: len(terminal) - _MAX_TERMINAL_JOBS]:
            self._jobs.pop(e.job_id, None)

    def _ensure_restored(self) -> None:
        """从 agent_jobs 表恢复未终结的远程任务（进程重启后一次）。

        失败静默（DB 不可用/未初始化时远程任务列表为空即可）。
        """
        if JobManager._restored:
            return
        with JobManager._instance_lock:
            if JobManager._restored:
                return
            try:
                import database as db
                rows = db.get_running_agent_jobs()
            except Exception:
                # 审查R2修复：原实现先置 _restored=True 再读 DB，一次 DB 抖动
                # （锁/损坏）就让本进程终身不再恢复远程任务。仅在成功读行后置位，
                # 失败留给下一个工具入口重试。
                return
            JobManager._restored = True
        for row in rows:
            job_id = row.get("job_id") or ""
            if not job_id or self.get(job_id):
                continue
            try:
                # created_at 由 database._now() 写成 **UTC** 字符串，而 time.mktime 按
                # **本地时区**解释 struct_time：UTC+8 主机上刚建的任务恢复后"已运行
                # 28800 秒"，这个读数还会进模型上下文。timegm 是 UTC 口径的 mktime。
                start_ts = calendar.timegm(time.strptime(
                    row.get("created_at") or "", "%Y-%m-%d %H:%M:%S"))
            except Exception:
                start_ts = time.time()
            cmd = row.get("cmd") or ""
            self.add(JobEntry(
                job_id=job_id,
                args=[cmd] if cmd else [],
                start_ts=start_ts,
                status=row.get("status") or "running",
                remote=True,
                ssh_session_id=row.get("session_id") or "",
                pid=row.get("pid"),
                log_dir=row.get("log_dir") or f"{_JOBS_ROOT}/{job_id}",
                run_id=row.get("run_id") or "",
                raw_cmd=cmd,
                out_offset=int(row.get("out_offset") or 0),
            ))


# ── 远程持久化辅助（DB 不可用时静默降级，不影响任务本身）─────────
def _persist_save(entry: JobEntry) -> None:
    try:
        import database as db
        db.save_agent_job(
            job_id=entry.job_id,
            run_id=entry.run_id,
            session_id=entry.ssh_session_id,
            cmd=entry.raw_cmd,
            pid=entry.pid,
            log_dir=entry.log_dir,
            status="running",
            out_offset=entry.out_offset,
        )
    except Exception:
        pass


def _persist_status(job_id: str, status: str, exit_code: Optional[int]) -> None:
    try:
        import database as db
        db.update_agent_job_status(job_id, status, exit_code)
    except Exception:
        pass


def _persist_offset(job_id: str, out_offset: int) -> None:
    """推进已读日志偏移（重启后 job_output 不从头重读已见日志）。"""
    try:
        import database as db
        db.update_agent_job_offset(job_id, out_offset)
    except Exception:
        pass


def _session_ctx(ssh_session_id: str):
    """为已恢复任务构造 run_headless 所需的最小上下文。"""
    return types.SimpleNamespace(ssh_session_id=ssh_session_id)


def _parse_remote_pid(output: str) -> Optional[int]:
    """从启动命令输出解析 PID（忽略 run_headless 追加的 [exit_code=..] 行）。"""
    for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
        if is_exit_code_line(line):
            continue
        if line.isdigit():
            return int(line)
        break
    return None


# ── 远端命令构建（单一事实来源）──────────────────────────────
# 契约源：与远端白名单执行器（scripts/deploy/agent-exec.py）对齐，
# 由 backend/tests/test_remote_whitelist_contract.py 跨端校验。

def job_launch_cmd(job_id: str, cmd: str) -> str:
    """job_run 远端启动模板：mkdir 目录 → 清残留 rc → base64 落 run.sh → setsid 后台运行 → echo $!。

    S6-05：必须先 rm -f rc。终态行（finished/failed/killed）不参与启动恢复，
    于是它们的号在后端重启后就是空闲号，而远端 <dir>/rc 还躺着上一任的退出码 ⇒
    复用该号的新作业第一次 job_output 就解析出陈旧 CKUNJOB_RC，刚启动即被判
    「已完成」并挂上死作业的退出码，随后 job_kill 以「已不在运行」拒止 —— 而进程还在跑。
    out.log 由 `>` 截断，无需额外清理。
    """
    d = f"{_JOBS_ROOT}/{job_id}"
    script = f"{d}/run.sh"
    log = f"{d}/out.log"
    rc = f"{d}/rc"
    script_content = f"#!/bin/bash\n{cmd}\necho $? > {rc}\n"
    sb64 = base64.b64encode(script_content.encode("utf-8")).decode("ascii")
    return (
        f"mkdir -p {d}\n"
        f"rm -f {rc}\n"
        f"echo {sb64} | base64 -d > {script}\n"
        f"setsid bash {script} > {log} 2>&1 < /dev/null &\n"
        f"echo $!"
    )


def job_read_cmd(log_dir: str, offset: int) -> str:
    """job_output 远端模板：读退出码文件 + 报日志字节总数 + 增量 tail 日志。

    S6-04：SZ 行是 **远端权威读数**。offset 的推进量原本按「回传字符串重新编码后的
    长度」计算，而 tail 的内容在 Electron 侧是按 TCP 分片 `toString('utf-8')` 拼出来
    的（无跨分片状态机），跨包的多字节字符与非 UTF-8 日志都会塌成 U+FFFD（3 字节）
    ⇒ 推进量虚高 ⇒ `tail -c +n` 永久跳丢字节，且漂移被 _persist_offset 落盘、跨重启
    存活。读数改由 `wc -c` 在 tail **之前**取，字节守恒就不再依赖字符串的编码往返。
    """
    d = log_dir
    n = offset + 1
    return (
        f"RC=; [ -f {d}/rc ] && RC=$(cat {d}/rc); "
        f"SZ=0; [ -f {d}/out.log ] && SZ=$(wc -c < {d}/out.log); "
        f"printf 'CKUNJOB_RC=%s\\nCKUNJOB_SZ=%s\\n' \"$RC\" \"$SZ\"; "
        f"[ -f {d}/out.log ] && tail -c +{n} {d}/out.log"
    )


def job_kill_cmd(pid: int) -> str:
    """job_kill 远端模板：整进程组 TERM → 3s → KILL 升级。

    S6-03：验证生死必须与发信号同口径 —— 打的是进程组 `-- -{pid}`，查的也得是
    进程组。原实现用 `kill -0 {pid}`（组长）验证：被包命令自我 daemon 化
    （nginx、redis-server --daemonize yes、任何以 & 收尾的）后组长已退出而组成员
    还活着 ⇒ 判成 CKUNJOB_DEAD ⇒ 作业标 killed、回「已终止（进程组已退出）」，
    而 SIGKILL 一次都没发出。当年 R2 的 STILL 诚实性修复只覆盖了「组长还活着」那支。
    S6-21 / S6-16：pid 取自 DB 且要拼进远端命令串，此处强制 int 化，
    脏值直接抛错而不是把任意文本送进 shell。
    """
    pid = int(pid)
    return (
        f"kill -TERM -- -{pid} 2>/dev/null; sleep 3; "
        f"if kill -0 -- -{pid} 2>/dev/null; then "
        f"kill -KILL -- -{pid} 2>/dev/null; sleep 1; "
        f"kill -0 -- -{pid} 2>/dev/null && echo CKUNJOB_STILL || echo CKUNJOB_DEAD; "
        f"else echo CKUNJOB_DEAD; fi"
    )


def job_reconcile_alive_cmd(pids) -> str:
    """job_list 对账模板①：按进程组批量判活（与 job_kill 同口径，见 S6-03）。"""
    pid_list = " ".join(str(int(p)) for p in pids)
    return (
        f"for p in {pid_list}; do kill -0 -- -$p 2>/dev/null && echo \"$p ALIVE\" "
        f"|| echo \"$p DEAD\"; done"
    )


def job_reconcile_rcs_cmd(log_dirs) -> str:
    """job_list 对账模板②：批量读退出码文件。"""
    dir_list = " ".join(log_dirs)
    return (
        f"for J in {dir_list}; do R=''; [ -f $J/rc ] && R=$(cat $J/rc); "
        f"printf '%s\\n' \"$R\"; done"
    )


async def _remote_launch(context, job_id: str, cmd: str) -> tuple[bool, Optional[int], str]:
    """远程启动后台任务，返回 (ok, pid, error)。

    命令结构（一次 exec，立即返回）：
      mkdir 目录 → base64 落 run.sh → setsid 后台运行 → echo $!
    run.sh 内容：原始命令 + 退出码落 rc 文件。
    """
    launch_cmd = job_launch_cmd(job_id, cmd)
    ok, out = await run_headless(context, launch_cmd, timeout=20)
    if not ok:
        return False, None, f"远程启动失败: {out}"
    pid = _parse_remote_pid(out)
    if pid is None:
        return False, None, f"无法解析远程进程 PID（输出: {out[:200]!r}）。" \
            "目标机可能缺少 setsid，请改用 shell_exec 的 nohup 方式。"
    return True, pid, ""


def _strip_sentinel(tail: str) -> str:
    """从日志段无损剥离 [exit_code=..] 哨兵，返回展示用 content。

    真实形态：rfind 已剥掉末行独立哨兵，本函数直接返回；旧测试 mock 形态
    （哨兵紧随 RC 值）：整体是哨兵，返回空串。不用 splitlines+rejoin 剥
    末行——那会把日志里的 CRLF/孤立 CR 归一成 \\n，损坏读回内容。
    """
    if is_exit_code_line(tail):
        return ""
    return tail


async def _remote_read(context, entry: JobEntry) -> tuple[str, Optional[int]]:
    """增量读取远程任务输出，返回 (new_output, rc|None)。

    审查R2修复：本函数是「读 entry.out_offset → await run_headless → += 推进」，
    await 前后跨两次事件循环调度，并发对同一 job 的调用会各自从同一 offset 读、
    各自推进一次 —— offset 双倍前进，中间一段日志被永久跳丢。选 per-job
    asyncio.Lock 而不是"await 前按请求偏移条件推进"：无锁方案下两个并发读仍会
    各自把同一段内容返回给不同调用方（重复投递），且后完成者覆盖先完成者的
    offset 时推进量可能偏短导致整段重读；锁让第二个调用直接等待、读到的是
    真正的"新字节"，语义与单调用路径完全一致。Lock 惰性创建：创建过程无
    await，单事件循环内不会有两个调用同时看到 None。
    """
    if entry.read_lock is None:
        entry.read_lock = asyncio.Lock()
    async with entry.read_lock:
        return await _remote_read_locked(context, entry)


async def _remote_read_locked(context, entry: JobEntry) -> tuple[str, Optional[int]]:
    """真正的增量读取（由 _remote_read 在持 per-job 读锁时调用）。

    字节守恒不变量：offset 每次推进的量必须等于 **out.log** 自上次 offset
    以来新增的字节数。少推 → 下一轮 tail -c +n 重复读；多推 → 永久跳丢日志。
    推进量取自远端 `wc -c` 读数（CKUNJOB_SZ 行，S6-04）——不回传字符串的编码
    长度：那条路径会把跨 TCP 分片的多字节字符与非 UTF-8 日志塌成 U+FFFD，
    重新编码后虚高。RC/SZ 行本身是 printf 格式字节、不在 out.log 里，不计入。
    剥哨兵不用 splitlines+rejoin：那会把日志里的 CRLF/孤立 CR 归一成 \\n，
    损坏读回内容且错位 offset。
    """
    cmd = job_read_cmd(entry.log_dir, entry.out_offset)
    # keep_full：这里读的是「日志字节」，offset 记账按回传字节数推进。若走默认的
    # 头尾保留截断（>4000 字符触发，紧跟一轮大输出的常见场景），回传段中间会被
    # 插入中文标记行 —— 推进量与真实字节错位，下一轮 tail -c +n 重复读同一段，
    # 且标记文本被当日志内容喂给模型。展示侧另有 truncate_output 兜着（见调用方）。
    ok, out = await run_headless(context, cmd, timeout=15, keep_full=True)
    if not ok:
        return f"读取远程输出失败: {out}", None
    # 剥离 run_headless 追加在末行的 [exit_code=..] 哨兵（tail 命令自身退出码，
    # 不是任务的）
    i = out.rfind(f"\n{EXIT_CODE_PREFIX}")
    body = out[:i] if i != -1 else out
    rc: Optional[int] = None
    size: Optional[int] = None
    log_segment = body
    if body.startswith("CKUNJOB_RC="):
        head, _sep, log_segment = body.partition("\n")
        val = head[len("CKUNJOB_RC="):].strip()
        if val.lstrip("-").isdigit():
            rc = int(val)
        # SZ 行紧随 RC 行；对端模板过旧（没有这一行）时 size 保持 None，走下面的
        # 保守分支
        if log_segment.startswith("CKUNJOB_SZ="):
            sz_line, _sep2, rest = log_segment.partition("\n")
            raw = sz_line[len("CKUNJOB_SZ="):].strip()
            if raw.isdigit():
                size = int(raw)
            log_segment = rest
    # 日志段 = 头部行之后的全部字节（tail -c +n 的原始输出）
    content = _strip_sentinel(log_segment)
    if size is not None:
        # S6-04：字节守恒改由远端读数决定，不再经过「字符串 ↔ 编码」往返。
        # SZ 在 tail **之前**测得：若 tail 期间作业又写了字节，读数只会偏小
        # ⇒ 下一轮重复读几个字节。偏小是本不变量唯一允许的误差方向（少推=重复，
        # 多推=永久跳丢）。
        entry.out_offset = size
    else:
        # 意外形态（无 RC/SZ 行）：按回传字节数推进，宁可重复也不跳丢
        entry.out_offset += len(log_segment.encode("utf-8"))
    if content:
        # offset 推进立即落盘：中途崩溃/重启后不从头重读已见日志
        # （best-effort，DB 不可用时静默跳过，行为与修复前一致）
        _persist_offset(entry.job_id, entry.out_offset)
    return content, rc


async def _remote_kill(context, entry: JobEntry) -> str:
    """远程终止：整进程组 TERM → 3s → KILL 升级，返回 DEAD/STILL/错误。"""
    if not entry.pid:
        return "NO_PID"
    pid = entry.pid
    cmd = job_kill_cmd(pid)
    ok, out = await run_headless(context, cmd, timeout=15)
    if not ok:
        return f"远程 kill 失败: {out}"
    if "CKUNJOB_STILL" in out:
        return "STILL"
    if "CKUNJOB_DEAD" in out:
        return "DEAD"
    return f"未知结果: {out[:100]!r}"


async def _read_dead_rc(context, entry: JobEntry) -> Optional[int]:
    """进程组已消失时回读一次 rc 文件，区分"我们杀的"与"它自己跑完的"。

    `job_kill_cmd` 回的 `CKUNJOB_DEAD` 只说明进程组不在了，两种成因分不出来：远程任务
    在 job_list 对账之前内存里一直是 running，所以"用户点终止时任务早就结束了"是常态。
    run.sh 只有走完最后一步才会写 rc —— 整进程组被 KILL 时 run.sh 自己也一起死了，
    所以 **有 rc = 它自己跑完的，没 rc = 我们杀的**。不回读就直接落 killed 的话，
    `update_status` 的 killed 粘滞防护（防 reader 把 killed 降级）会让这条永不回退，
    真实退出码就此丢失。

    复用对账的 `job_reconcile_rcs_cmd` 模板（远端白名单已接受该串，不需要重新下发
    wrapper），并按字节协议取回（keep_full，理由见 `_reconcile_remote`）。
    """
    ok, out = await run_headless(
        context, job_reconcile_rcs_cmd([entry.log_dir]), timeout=10, keep_full=True)
    if not ok:
        return None
    body, _err, _rc = remote_parts(out)
    for line in body.splitlines():
        text = line.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
        if text:
            return None  # rc 文件里不是个退出码：宁可当"没读到"，不编造
    return None


async def kill_remote_jobs_for_run(run_id: str) -> int:
    """run 取消/断连时清理该 run 名下全部在跑的远程任务（best-effort）。

    返回已落终态的数量（被终止或确认已自行结束）；单个失败不阻断其余。
    """
    if not run_id:
        return 0
    mgr = JobManager.instance()
    mgr._ensure_restored()
    targets = [
        e for e in mgr.list()
        if e.remote and e.status == "running" and e.run_id == run_id
    ]
    if not targets:
        return 0
    async def _kill_one(e: JobEntry) -> str:
        return await _remote_kill(_session_ctx(e.ssh_session_id), e)

    results = await asyncio.gather(
        *[_kill_one(e) for e in targets],
        return_exceptions=True,
    )
    killed = 0
    for e, r in zip(targets, results):
        if isinstance(r, str) and r == "DEAD":
            rc = await _read_dead_rc(_session_ctx(e.ssh_session_id), e)
            if rc is None:
                mgr.update_status(e.job_id, "killed", None)
            else:
                mgr.update_status(e.job_id, "finished" if rc == 0 else "failed", rc)
            killed += 1
    return killed


def _split_cmd(cmd: str) -> List[str]:
    """将单条命令拆为参数列表。

    优先 shlex.split（含引号/转义语义），失败时退化为按空白分词，
    保证 Windows 下也能得到可用的参数清单。
    """
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return parts or []


async def _drain(proc: asyncio.subprocess.Process, entry: JobEntry) -> None:
    """后台 reader task：持续回收 stdout/stderr 到内存缓冲。

    逐行读取并 utf-8( errors="replace") 解码追加进 entry.output；
    任一流结束即停止对应循环，最后根据 returncode 落状态与退出码。
    """
    stdout_task = asyncio.create_task(_read_stream(proc.stdout, entry))
    stderr_task = asyncio.create_task(_read_stream(proc.stderr, entry))
    await asyncio.gather(stdout_task, stderr_task)
    # 等待进程完全退出以读取最终退出码
    try:
        returncode = await proc.wait()
    except Exception:
        returncode = proc.returncode
    mgr = JobManager.instance()
    if returncode == 0:
        mgr.update_status(entry.job_id, "finished", returncode)
    elif entry.status == "killed":
        mgr.update_status(entry.job_id, "killed", returncode)
    elif returncode is None:
        mgr.update_status(entry.job_id, "running", None)
    else:
        mgr.update_status(entry.job_id, "failed", returncode)


async def _read_stream(stream, entry: JobEntry) -> None:
    """读取单个输出流并追加到缓冲（带缓冲上限，超限丢头部保留最近内容）。"""
    buf_list = entry.output  # 直接持有引用，追加受 manager 锁保护
    while True:
        try:
            line = await stream.readline()
        except Exception:
            break
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        with JobManager.instance()._lock:
            buf_list.append(text)
            entry.output_bytes += len(text)
            # 上限：丢弃最旧的行保留最近内容（job_output 展示的是尾部，
            # 头部旧日志对排障价值最低；丢行不保证行边界完整，仅防无界增长）。
            # PERF：原实现每行对整个缓冲 sum(len) + pop(0)（均 O(n)），缓冲打满后
            # 万条目稳态下每行万次扫描且直接跑在事件循环上；改为增量计数 + deque
            # popleft，每行 O(1)
            while entry.output_bytes > _LOCAL_OUTPUT_CAP and len(buf_list) > 1:
                entry.output_bytes -= len(buf_list.popleft())


def _running_seconds(entry: JobEntry, now: float) -> int:
    return max(0, int(now - entry.start_ts))


class JobRunTool(BaseTool):
    name = "job_run"
    description = (
        "在后台启动长驻/跟随任务并返回 job_id，用 job_output 查看输出、job_kill 终止。"
        "target=local：本地子进程（参数列表执行，非 shell）。"
        "target=remote：在远程 SSH 会话脱离会话后台运行（nohup 语义，关窗口继续跑），"
        "返回进程组 PID，日志落远程 /tmp/ckun_agent_jobs/<job_id>/。"
        "预计执行超过 60 秒的任务一律用 remote，不要用 shell_exec 同步等待。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "要后台执行的单条命令"},
            "target": {
                "type": "string",
                "enum": ["local", "remote"],
                "description": "local=后端主机本地子进程（默认）；remote=远程 SSH 会话",
            },
            "cwd": {"type": "string", "description": "可选工作目录（仅 local），缺省用后端进程当前目录"},
        },
        "required": ["cmd"],
    }
    meta = _RUN_META

    async def execute(self, context, **kwargs) -> ToolResult:
        # 这条工具只认 cmd，而判定层认 command/shell/cmd 三键（reins.core）。模型若同时
        # 给两个不同的键，「卡片上被批准的」和「真正跑的」就不是同一条命令 —— 那是审批
        # 绕过，不是参数宽容。在这里拒绝，让歧义根本不存在（shell_exec 是位置签名，多余
        # 键天然 TypeError 失败；两条命令入口必须同样拒绝，否则修一条漏一条）。
        aliases = [k for k in ("command", "shell") if str(kwargs.get(k) or "").strip()]
        if aliases:
            return ToolResult(
                success=False, output="",
                error=f"拒绝执行：job_run 只接受 cmd，多余命令参数键 {aliases} 会让"
                      f"审批对象与执行对象不一致，请只保留 cmd")
        cmd = (kwargs.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(success=False, output="", error="cmd 不能为空")
        target = (kwargs.get("target") or "local").strip().lower()
        if target not in ("local", "remote"):
            return ToolResult(success=False, output="", error="target 只能是 local 或 remote")

        mgr = JobManager.instance()
        # 与 job_list/job_output/job_kill 同一前置：先恢复 DB 里未终结的远程 job，
        # 否则 _next_id 看不到已占用的号，重启后首发会撞回复用中的 job_id
        mgr._ensure_restored()
        job_id = mgr._next_id()

        # ── 远程分支 ──
        if target == "remote":
            # 入口显式 fail-closed（不依赖 run_headless 内部检查，测试可精确断言）
            if not (getattr(context, "ssh_session_id", "") or ""):
                return ToolResult(
                    success=False, output="",
                    error="拒绝执行：缺少 SSH 会话，远程后台任务仅在 SSH 会话内执行",
                )
            entry = JobEntry(
                job_id=job_id,
                args=[cmd],
                start_ts=time.time(),
                remote=True,
                ssh_session_id=getattr(context, "ssh_session_id", "") or "",
                log_dir=f"{_JOBS_ROOT}/{job_id}",
                run_id=getattr(context, "agent_run_id", "") or "",
                raw_cmd=cmd,
            )
            ok, pid, err = await _remote_launch(context, job_id, cmd)
            if not ok:
                return ToolResult(success=False, output="", error=err)
            entry.pid = pid
            mgr.add(entry)
            _persist_save(entry)
            return ToolResult(
                success=True,
                output=(
                    f"已远程后台启动: {cmd}\n"
                    f"job_id={job_id} pid={pid}\n"
                    f"日志: {entry.log_dir}/out.log（退出码文件 rc）\n"
                    f"用 job_output(job_id='{job_id}') 轮询输出，job_kill 终止"
                ),
            )

        # ── 本地分支（沙箱隔离执行）──
        # 本地执行是唯一的本机命令入口，必须经沙箱做能力限制（只读根 + 无网络 +
        # 资源上限 + 降权 + 丢权能）。无沙箱后端且策略要求沙箱 → fail-closed 拒绝。
        args = _split_cmd(cmd)
        if not args:
            return ToolResult(success=False, output="", error="cmd 无法解析为参数列表")
        # cwd 未显式指定时保持 None → 沙箱在进程当前目录下执行
        cwd = kwargs.get("cwd") or None
        try:
            from agent.harness.sandbox_exec import (
                run_sandboxed,
                SandboxUnavailable,
                SandboxLimits,
                ALLOW_UNSANDBOXED_LOCAL,
            )
            proc = await run_sandboxed(
                args,
                workdir=cwd,
                limits=SandboxLimits(),
                # 生产默认 fail-closed；开发环境设
                # CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL=1 可放行裸跑
                require_sandbox=not ALLOW_UNSANDBOXED_LOCAL,
            )
        except SandboxUnavailable as e:
            return ToolResult(
                success=False, output="",
                error=f"本地任务启动被拒绝（沙箱不可用，fail-closed）：{e}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"启动后台任务失败: {e}")

        entry = JobEntry(
            job_id=job_id,
            args=args,
            start_ts=time.time(),
            proc=proc,
        )
        entry.reader_task = asyncio.create_task(_drain(proc, entry))
        mgr.add(entry)
        return ToolResult(success=True, output=f"已本地后台启动: {cmd}\njob_id={job_id}")


class JobListTool(BaseTool):
    name = "job_list"
    description = (
        "列出所有后台任务（本地 + 远程，含已结束仍在 manager 保留的）："
        "id | 状态 | 运行秒数 | cmd。远程在跑任务会顺带做存活对账（kill -0）。只读。"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    meta = _READ_META

    async def execute(self, context, **kwargs) -> ToolResult:
        mgr = JobManager.instance()
        mgr._ensure_restored()
        jobs = mgr.list()
        if not jobs:
            return ToolResult(success=True, output="（暂无后台任务）")

        # 远程在跑任务按会话分组做一次批量对账（best-effort，失败不阻断列表）
        running_remote = [e for e in jobs if e.remote and e.status == "running" and e.pid]
        if running_remote:
            await _reconcile_remote(mgr, running_remote)

        now = time.time()
        lines = []
        for e in jobs:
            tag = "远程" if e.remote else "本地"
            cmd_txt = e.raw_cmd if e.remote else " ".join(e.args)
            lines.append(
                f"{e.job_id} | {e.status} | {tag} | {_running_seconds(e, now)}s | {cmd_txt}"
            )
        return ToolResult(success=True, output="\n".join(lines))


async def _reconcile_remote(mgr: JobManager, entries: List[JobEntry]) -> None:
    """批量对账远程在跑任务：kill -0 判存活，死掉的任务读 rc 落终态。

    按 ssh_session_id 分组，每会话最多两次 exec（判活 + 读 rc）。
    """
    by_session: dict[str, List[JobEntry]] = {}
    for e in entries:
        by_session.setdefault(e.ssh_session_id or "", []).append(e)
    for sid, group in by_session.items():
        if not sid:
            for e in group:
                mgr.update_status(e.job_id, "exited", None)
            continue
        ctx = _session_ctx(sid)
        pid_list = [e.pid for e in group]
        # 两次回传都要 keep_full（字节协议通道口径，见 exec_remote）：默认通道会
        # truncate_output 头尾保留，把标记行插进正文中间 ——
        #   · 判活清单被截 → 被截掉的 pid 不在 alive 集合里 → 活着的任务被判成已退出；
        #   · rc 清单是被 `dead` **按下标**索引的 → 截断后整体错位，把 A 的退出码记到
        #     B 头上（下面那条"只跳真正的哨兵行"的过滤防的是哨兵行错位，这是同一根因另一条路）。
        # 单行只有 1~10 字节，越界要约几百上千条同会话任务同时存在，属"一次写错、
        # 平时不发作"的隐患，所以从结构上断掉而不是加行数上限。
        ok, out = await run_headless(
            ctx, job_reconcile_alive_cmd(pid_list), timeout=15, keep_full=True)
        if not ok:
            # 本会话桥接不可达 — 保持原状态下次再对账。必须是 continue：
            # return 会让排在后面的会话整轮不被检查，而那些会话完全正常。
            continue
        alive = {
            int(ln.split()[0]) for ln in out.splitlines()
            if ln.strip().endswith("ALIVE") and ln.split()[0].isdigit()
        }
        dead = [e for e in group if e.pid not in alive]
        if not dead:
            # 同上：本会话的任务都还活着是正常态，不代表别的会话也没有已退出的任务
            continue
        # 死掉的任务读 rc 文件定终态
        dead_dirs = [e.log_dir for e in dead]
        ok2, out2 = await run_headless(
            ctx, job_reconcile_rcs_cmd(dead_dirs), timeout=15, keep_full=True)
        rcs = out2.splitlines() if ok2 else []
        # 只跳真正的哨兵行（含 `=`）：少一个 `=` 的宽松写法会把恰好以
        # "[exit_code" 开头的 rc 数据行一起删掉，后续 rcs[i] 与 dead_dirs 的
        # 下标对齐就错了 —— 把 A 任务的退出码记到 B 任务头上。
        rcs = [ln for ln in rcs if not is_exit_code_line(ln)]
        for i, e in enumerate(dead):
            rc_raw = rcs[i].strip() if i < len(rcs) else ""
            if rc_raw.lstrip("-").isdigit():
                code = int(rc_raw)
                mgr.update_status(e.job_id, "finished" if code == 0 else "failed", code)
            else:
                mgr.update_status(e.job_id, "exited", None)


class JobOutputTool(BaseTool):
    name = "job_output"
    description = (
        "读取指定后台任务的输出。本地：累积内存缓冲；远程：增量 tail 日志"
        "（返回上次读取后的新内容）。wait>0 时最多阻塞等待 wait 秒的新输出或结束。"
        "任务退出后返回其退出码。job 不存在返回错误。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "后台任务 id"},
            "wait": {"type": "integer", "description": "可选等待秒数（0-60，默认 0）"},
        },
        "required": ["job_id"],
    }
    meta = _READ_META

    async def execute(self, context, **kwargs) -> ToolResult:
        job_id = str(kwargs.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(success=False, output="", error="job_id 必填")
        wait, err = parse_int_arg(kwargs.get("wait"), "wait", 0)
        if err:
            return ToolResult(success=False, output="", error=err)
        wait = max(0, min(wait, 60))

        mgr = JobManager.instance()
        mgr._ensure_restored()
        entry = mgr.get(job_id)
        if entry is None:
            return ToolResult(success=False, output="", error=f"job 不存在: {job_id}")

        # ── 远程分支 ──
        if entry.remote:
            ctx = _session_ctx(entry.ssh_session_id)
            chunks: List[str] = []
            if wait > 0:
                deadline = time.time() + wait
                while time.time() < deadline and entry.status == "running":
                    content, rc = await _remote_read(ctx, entry)
                    if rc is not None:
                        mgr.update_status(job_id,
                                          "finished" if rc == 0 else "failed", rc)
                    if content:
                        chunks.append(content)
                        break
                    await asyncio.sleep(0.5)
            content, rc = await _remote_read(ctx, entry)
            if rc is not None and entry.status == "running":
                mgr.update_status(job_id, "finished" if rc == 0 else "failed", rc)
            if content:
                chunks.append(content)
            body = "".join(chunks)
            if not body.strip():
                body = "（暂无新输出" + ("，任务已退出）" if entry.status != "running"
                              else "，任务仍在运行）")
            header = f"[job {job_id} 状态={entry.status}"
            if entry.exit_code is not None:
                header += f" exit_code={entry.exit_code}"
            header += f" pid={entry.pid}]"
            return ToolResult(success=True, output=f"{header}\n" + truncate_output(body))

        # ── 本地分支（原实现）──
        start_len = len(entry.output)
        if wait > 0 and entry.reader_task is not None:
            deadline = time.time() + wait
            while time.time() < deadline and not entry.reader_task.done():
                if len(entry.output) > start_len:
                    break
                await asyncio.sleep(0.2)

        content = "".join(entry.output)
        header = f"[job {job_id} 状态={entry.status}"
        if entry.exit_code is not None:
            header += f" exit_code={entry.exit_code}"
        header += "]"
        return ToolResult(success=True, output=f"{header}\n" + truncate_output(content))


class JobKillTool(BaseTool):
    name = "job_kill"
    description = (
        "终止指定后台任务：先 SIGTERM，等待约 2~3 秒仍未退出则升级 SIGKILL，"
        "标记状态 killed 并返回结果。远程任务按进程组整组终结（含子孙进程）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "后台任务 id"},
        },
        "required": ["job_id"],
    }
    meta = _RUN_META

    async def execute(self, context, **kwargs) -> ToolResult:
        job_id = str(kwargs.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(success=False, output="", error="job_id 必填")

        mgr = JobManager.instance()
        mgr._ensure_restored()
        entry = mgr.get(job_id)
        if entry is None:
            return ToolResult(success=False, output="", error=f"job 不存在: {job_id}")
        if entry.status != "running":
            return ToolResult(
                success=True,
                output=f"job {job_id} 已不在运行（状态={entry.status}）",
            )

        # ── 远程分支 ──
        if entry.remote:
            ctx = _session_ctx(entry.ssh_session_id)
            result = await _remote_kill(ctx, entry)
            if result == "DEAD":
                # DEAD 只说明进程组不在了，不说明是谁让它不在的：回读 rc 定真实终态，
                # 否则一条早就跑完并退出码 0 的任务会被永久记成「被你终止」
                rc = await _read_dead_rc(ctx, entry)
                if rc is None:
                    mgr.update_status(job_id, "killed", None)
                    return ToolResult(
                        success=True,
                        output=f"已终止远程 job {job_id}（进程组 {entry.pid} 已退出）",
                    )
                mgr.update_status(job_id, "finished" if rc == 0 else "failed", rc)
                return ToolResult(
                    success=True,
                    output=f"job {job_id} 的进程组在收到终止信号前已自行退出，"
                           f"退出码 {rc}（按真实退出码记账，未记成 killed）",
                )
            if result == "STILL":
                # 审查R2修复：SIGKILL 后进程组仍在，原实现却标 killed —— 谎报终态：
                # update_status 的竞态防护会让 killed 永不回退，job_list 对账也只查
                # running，进程实际还活着却永远不会被再次核实/清理。
                # 选择保持 running（而非新增 kill_failed 态）：进程确实还在跑是事实，
                # 状态机不扩新态，用户可按错误提示重试 kill，最终退出由对账落终态。
                return ToolResult(
                    success=False,
                    output="",
                    error=f"远程 job {job_id}（pid={entry.pid}）SIGKILL 后仍未退出，"
                          "可能存在守护/重拉机制，请登录核查",
                )
            return ToolResult(success=False, output="", error=result)

        # ── 本地分支（原实现）──
        proc = entry.proc
        try:
            proc.terminate()
        except ProcessLookupError:
            return ToolResult(success=True, output=f"job {job_id} 进程已结束")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"terminate 失败: {e}")

        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass

        # 读取最终退出码（_drain 会据此置状态；这里主动落 killed）
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass
        mgr.update_status(job_id, "killed", proc.returncode)

        rc = proc.returncode
        rc_txt = f"，exit_code={rc}" if rc is not None else ""
        return ToolResult(success=True, output=f"已终止 job {job_id}{rc_txt}")
