"""后台任务管理工具集 — 本地子进程为主，远程尽可能而为。SSH 模式专属。

包含四个 BaseTool 子类：
  - job_run    在后台启动一个本地子进程长驻/跟随任务，返回 job_id
  - job_list   列出所有（含已结束保留的）后台任务
  - job_output 读取指定任务的累积输出缓冲（可短暂阻塞等待新输出）
  - job_kill   终止指定任务（先 terminate，再 escalate kill）

安全约束：
  1. 一律以参数列表方式创建子进程（asyncio.create_subprocess_exec），
     严禁 shell=True / join 命令字符串，规避 shell 注入与 Windows 解析歧义。
  2. 用户输入命令经 shlex.split 拆为参数清单，本地子进程在受限权限下运行，
     输出与副作用隔离在内存缓冲，不落库。
  3. 后台任务以本地子进程为主；远程 SSH 会话因桥接缺少远程 kill 通道，
     远程大任务仅"尽可能而为"，仍以本地进程形态执行。
"""
import asyncio
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from agent.tools import BaseTool, ToolResult, ToolMeta
from agent.tools.builtin.ops_common import truncate_output

# job_run / job_kill 会启动并终止外部进程，具备中等影响，标记 moderate 不可幂等。
_RUN_META = ToolMeta(
    risk_level="moderate",
    reversible=False,
    idempotent=False,
    timeout=30.0,
    mode_scope=["ssh"],
)
# job_list / job_output 只读不改变状态，安全且可重复执行。
_READ_META = ToolMeta(
    risk_level="safe",
    reversible=True,
    idempotent=True,
    timeout=15.0,
    mode_scope=["ssh"],
)


@dataclass
class JobEntry:
    """单个后台任务的运行时记录。"""
    job_id: str
    args: List[str]
    start_ts: float
    proc: asyncio.subprocess.Process
    output: List[str] = field(default_factory=list)
    status: str = "running"          # running / finished / failed / killed
    exit_code: Optional[int] = None
    reader_task: Optional[asyncio.Task] = None


class JobManager:
    """模块级单例：以本地子进程管理后台任务。

    线程安全（threading.Lock 保护 job 字典），活动 job 仅存内存不落库。
    已结束的 job 保留在字典中以便 job_list / job_output 追溯。
    """

    _instance: Optional["JobManager"] = None
    _instance_lock = threading.Lock()
    _seq_lock = threading.Lock()
    _seq = 0

    def __init__(self):
        self._jobs: dict[str, JobEntry] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "JobManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _next_id(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return str(self._seq)

    def add(self, entry: JobEntry) -> None:
        with self._lock:
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
            # 竞态防护：已标记 killed 的 job 不再被 _drain 降级为 failed/finished，
            # 保证用户显式终止的语义不被后台 reader 覆盖。
            if entry.status == "killed":
                entry.exit_code = exit_code
                return
            entry.status = status
            entry.exit_code = exit_code


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
    """读取单个输出流并追加到缓冲（带简单缓冲累计，靠近行边界追加）。"""
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


def _running_seconds(entry: JobEntry, now: float) -> int:
    return max(0, int(now - entry.start_ts))


class JobRunTool(BaseTool):
    name = "job_run"
    description = (
        "在后台启动一个本地子进程长驻/跟随任务并返回 job_id。"
        "命令以参数列表执行（非 shell），支持本地长驻；远程会话仅尽力而为。"
        "可用 job_output 查看输出、job_kill 终止。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "要后台执行的单条命令，将按空白/引号拆为参数列表"},
            "cwd": {"type": "string", "description": "可选工作目录，缺省用工作区根目录"},
        },
        "required": ["cmd"],
    }
    meta = _RUN_META

    async def execute(self, context, **kwargs) -> ToolResult:
        cmd = (kwargs.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(success=False, output="", error="cmd 不能为空")
        args = _split_cmd(cmd)
        if not args:
            return ToolResult(success=False, output="", error="cmd 无法解析为参数列表")

        cwd = kwargs.get("cwd")
        if not cwd:
            workspace = getattr(context, "workspace_path", "") or ""
            cwd = workspace or None

        mgr = JobManager.instance()
        job_id = mgr._next_id()
        entry = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
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

        out = f"已后台启动: {cmd}\njob_id={job_id}"
        if getattr(context, "ssh_session_id", ""):
            out += (
                "\n注：远程后台大任务仅尽力而为，本实现以本地子进程执行"
                "（缺少远程 kill 通道）。"
            )
        return ToolResult(success=True, output=out)


class JobListTool(BaseTool):
    name = "job_list"
    description = "列出所有后台任务（含已结束仍在 manager 保留的）：id | 状态 | 运行秒数 | cmd。只读。"
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    meta = _READ_META

    async def execute(self, context, **kwargs) -> ToolResult:
        mgr = JobManager.instance()
        jobs = mgr.list()
        if not jobs:
            return ToolResult(success=True, output="（暂无后台任务）")
        now = time.time()
        lines = []
        for e in jobs:
            lines.append(
                f"{e.job_id} | {e.status} | {_running_seconds(e, now)}s | {' '.join(e.args)}"
            )
        return ToolResult(success=True, output="\n".join(lines))


class JobOutputTool(BaseTool):
    name = "job_output"
    description = (
        "读取指定后台任务的累积输出缓冲。wait>0 时最多阻塞等待 wait 秒的新输出"
        "或进程结束；job 不存在返回错误。"
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
        wait = int(kwargs.get("wait") or 0)
        wait = max(0, min(wait, 60))

        mgr = JobManager.instance()
        entry = mgr.get(job_id)
        if entry is None:
            return ToolResult(success=False, output="", error=f"job 不存在: {job_id}")

        start_len = len(entry.output)
        if wait > 0:
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
        "终止指定后台任务：先 terminate（SIGTERM），等待约 2 秒仍未退出则"
        " kill（SIGKILL），标记状态 killed，返回已终止信息。"
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
        entry = mgr.get(job_id)
        if entry is None:
            return ToolResult(success=False, output="", error=f"job 不存在: {job_id}")
        if entry.status != "running":
            return ToolResult(
                success=True,
                output=f"job {job_id} 已不在运行（状态={entry.status}）",
            )

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