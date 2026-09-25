# -*- coding: utf-8 -*-
"""第 15 轮回归：字节协议通道不得被「给人看的输出」截断损坏。

根因只有一处：exec_remote 对**每条**远程命令的输出无条件做头尾保留截断
（>4000 字符 → 头部 1500 + 中文标记行 + 尾部 1000）。这个长度兜底本意是防
LLM 上下文被刷屏，但它同时覆盖了三个把回传内容当**字节协议**用的调用方：

  1. remote_read_b64 → file_edit：>2.9KB 的文件读回即混入中文标记 →
     b64decode 抛异常 → 报「远程读取文件失败或文件不存在」，把超限/通道异常
     一律误导成文件不存在（模型随即转向 file_write 整体覆写，而那内容只有
     file_read 的截断片段 → 覆盖即丢数据）。
  2. saddle 预变更快照：同一个读通道 → 快照解码失败 → NoOp，回滚能力对
     任何真实配置文件静默消失。
  3. ops_job._remote_read：offset 按「回传字符数」推进，而回传段已被改写 →
     推进量与真实字节错位 → 下一轮 tail -c +n 重复读同一段日志，且标记文本
     被当作任务输出喂给模型。

本文件的测试**跨到 seam 另一侧**：只替换 ElectronBridge（远程执行的物理边界），
让 exec_remote / run_headless / remote_read_b64 / 两个工具的真实实现全部参与，
因此截断逻辑一旦被改回去，这里必然红。
"""
import base64
import re
import shlex
import time

import pytest

import agent.tools.builtin.ops_common as ops_common
import agent.tools.builtin.ops_file as of
import agent.tools.builtin.ops_job as oj
from agent.harness.saddle import FileContentUndo, NoOpUndo, UndoBuilder
from agent.tools import ToolContext

_JOB_ROOT = "/tmp/ckun_agent_jobs"


# ── 虚拟远端：只实现三个命令模板，其余一律报错（防止测试悄悄依赖新形态）──
class _VirtualRemote:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.logs: dict[str, bytes] = {}      # "<log_dir>/out.log" -> 字节
        self.rcs: dict[str, str] = {}         # log_dir -> rc 文本
        self.fails: dict[str, str] = {}       # path -> 读取失败原因（模拟权限等）
        self.calls: list[tuple[str, str]] = []

    async def exec(self, ssh_session_id: str, command: str, timeout: float):
        self.calls.append((ssh_session_id, command))
        cmd = command.strip()

        m = re.match(r"^base64 (.+)$", cmd)
        if m:
            path = shlex.split(m.group(1))[0]
            if path in self.fails:
                return {"success": False, "output": "", "error": self.fails[path],
                        "exit_code": 1}
            if path not in self.files:
                return {"success": False, "output": "",
                        "error": f"base64: {path}: No such file or directory",
                        "exit_code": 1}
            b64 = base64.b64encode(self.files[path]).decode("ascii")
            wrapped = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
            return {"success": True, "output": wrapped + "\n", "error": "", "exit_code": 0}

        m = re.match(r"^printf '%s' '([A-Za-z0-9+/=]*)' \| base64 -d > (.+)$", cmd)
        if m:
            path = shlex.split(m.group(2))[0]
            self.files[path] = base64.b64decode(m.group(1))
            return {"success": True, "output": "", "error": "", "exit_code": 0}

        # job_read_cmd 模板（三段）：读 rc → printf CKUNJOB_RC= → tail -c +n out.log
        tail_m = re.search(r"tail -c \+(\d+) (\S+)/out\.log$", cmd)
        rc_m = re.match(r"^RC=; \[ -f (.+)/rc \]", cmd)
        if tail_m and rc_m:
            start = int(tail_m.group(1))
            data = self.logs.get(f"{rc_m.group(1)}/out.log", b"")[start - 1:]
            rc = self.rcs.get(rc_m.group(1), "")
            body = f"CKUNJOB_RC={rc}\n".encode() + data
            return {"success": True, "output": body.decode("utf-8", errors="replace"),
                    "error": "", "exit_code": 0}

        raise AssertionError(f"虚拟远端未实现的命令模板: {cmd!r}")


@pytest.fixture()
def remote(monkeypatch):
    vr = _VirtualRemote()

    class _Bridge:
        """exec_remote 只用到 is_available() 与 await exec_ssh_command(**kwargs)。"""

        def is_available(self):
            return True

        async def exec_ssh_command(self, **kwargs):
            return await vr.exec(**kwargs)

    monkeypatch.setattr(ops_common, "ElectronBridge", _Bridge)
    return vr


def _ctx(**kw) -> ToolContext:
    base = dict(ssh_session_id="sess-1", environment="test",
                visible_terminal=False, agent_run_id="run-1")
    base.update(kw)
    return ToolContext(**base)


# 一份体量真实的配置：默认 nginx.conf 就有 ~4.5KB，base64 后远超 4000 字符
def _big_conf(n_lines: int = 120) -> bytes:
    mid = n_lines // 2
    lines = []
    for i in range(n_lines):
        if i == mid:
            lines.append("    keepalive_timeout  65;   # 唯一可定位的一行\n")
        else:
            lines.append(f"    proxy_read_timeout 60; # 第 {i} 行占位配置内容\n")
    return ("user nginx;\nworker_processes auto;\n" + "".join(lines)).encode("utf-8")


# ══════════════════════════════════════════════════════════════
#  1. exec_remote：默认仍截断（LLM 上下文兜底没被拆掉），keep_full 逐字节
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_default_channel_still_truncates(remote):
    """控制组：不传 keep_full 时仍然是头尾保留截断 —— 上下文兜底没被这次修复拆掉。"""
    remote.files["/srv/app/big.txt"] = b"L" * 20000
    ok, out = await ops_common.run_headless(
        _ctx(), ops_common.base64_read_cmd("/srv/app/big.txt"), 15)
    assert ok is True
    assert "输出已截断" in out
    assert len(out) < 5000


@pytest.mark.asyncio
async def test_keep_full_returns_every_byte(remote):
    remote.files["/srv/app/big.txt"] = b"L" * 20000
    cmd = ops_common.base64_read_cmd("/srv/app/big.txt")
    ok, out = await ops_common.run_headless(_ctx(), cmd, 15, keep_full=True)
    assert ok is True
    assert "截断" not in out
    body = ops_common.strip_remote_tail(out)
    assert base64.b64decode(body) == b"L" * 20000


# ══════════════════════════════════════════════════════════════
#  2. file_edit：>2.9KB 的真实配置文件必须可编辑，且中间内容不得丢
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_file_edit_on_large_remote_file(remote):
    path = "/srv/app/nginx.conf"
    original = _big_conf()
    remote.files[path] = original
    assert len(base64.b64encode(original)) > ops_common.TRUNC_THRESHOLD * 2, \
        "用例前提：文件必须大于会触发截断的体量"

    res = await of.FileEditTool().execute(
        _ctx(), path=path,
        old_string="keepalive_timeout  65;", new_string="keepalive_timeout  120;",
    )
    assert res.success is True, f"大文件编辑被拒: {res.error}"
    got = remote.files[path]
    assert b"keepalive_timeout  120;" in got
    # 旧实现下这里会是「头 1500 + 标记 + 尾 1000」的拼接体：中间全丢
    assert got == original.replace(
        b"keepalive_timeout  65;", b"keepalive_timeout  120;"), "回写内容非原文件的最小改动"


@pytest.mark.asyncio
async def test_file_edit_oversize_reports_the_real_reason(remote):
    path = "/srv/app/huge.conf"
    remote.files[path] = b"x" * (ops_common.B64_READ_MAX_BYTES + 4096)
    res = await of.FileEditTool().execute(
        _ctx(), path=path, old_string="xx", new_string="yy")
    assert res.success is False
    assert "上限" in res.error, f"应报载荷超限，实际: {res.error}"
    assert "不存在" not in res.error, "超限不得被说成文件不存在"
    assert remote.files[path].startswith(b"xxx"), "拒绝编辑后原文件必须未被改写"


@pytest.mark.asyncio
async def test_file_edit_missing_file_says_so(remote):
    res = await of.FileEditTool().execute(
        _ctx(), path="/srv/app/nope.conf", old_string="a", new_string="b")
    assert res.success is False
    assert "No such file" in res.error


# ══════════════════════════════════════════════════════════════
#  3. saddle 预变更快照：大文件要有快照；读失败而无「不存在」证据时禁止删除
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_snapshot_of_large_file_is_usable(remote):
    original = _big_conf()
    remote.files["/srv/app/nginx.conf"] = original
    call = type("C", (), {"name": "file_edit", "args": {"path": "/srv/app/nginx.conf"}})()
    undo = await UndoBuilder._build_file_undo("file_edit", {"path": "/srv/app/nginx.conf"}, _ctx())
    assert isinstance(undo, FileContentUndo), "大文件应有内容级快照，而不是 NoOp"
    assert undo.existed is True
    assert base64.b64decode(undo.snapshot_b64) == original
    assert call.name == "file_edit"


@pytest.mark.asyncio
async def test_snapshot_read_failure_without_absence_evidence_is_noop(remote):
    """权限拒绝 ≠ 文件不存在：判成 existed=False 会让回滚 rm -f 掉一个存在的文件。"""
    path = "/etc/sudoers"
    remote.files[path] = b"root ALL=(ALL) ALL\n" * 500
    remote.fails[path] = "base64: /etc/sudoers: Permission denied"
    undo = await UndoBuilder._build_file_undo("file_write", {"path": path}, _ctx())
    assert isinstance(undo, NoOpUndo), f"必须 NoOp，实际 {type(undo).__name__} existed=" \
        f"{getattr(undo, 'existed', '-')}"
    assert getattr(undo, "existed", True) is not False


@pytest.mark.asyncio
async def test_snapshot_for_new_file_still_maps_to_delete(remote):
    undo = await UndoBuilder._build_file_undo(
        "file_write", {"path": "/srv/app/new.conf"}, _ctx())
    assert isinstance(undo, FileContentUndo)
    assert undo.existed is False and undo.snapshot_b64 is None


# ══════════════════════════════════════════════════════════════
#  4. job_output 远程 tail：offset 必须按真实字节推进，跨截断阈值也不重复读
# ══════════════════════════════════════════════════════════════

def _job_entry(log_dir_job: str = "j1") -> oj.JobEntry:
    e = oj.JobEntry(
        job_id=log_dir_job, args=["sleep", "300"], start_ts=time.time(),
        remote=True, ssh_session_id="sess-1", pid=4242,
        log_dir=f"{_JOB_ROOT}/{log_dir_job}", run_id="run-1", raw_cmd="sleep 300",
    )
    oj.JobManager.instance().add(e)
    return e


@pytest.mark.asyncio
async def test_job_read_offset_exact_over_truncation_threshold(remote):
    log = ("".join(f"line {i:05d} 构建输出正文\n" for i in range(400))).encode("utf-8")
    assert len(log) > ops_common.TRUNC_THRESHOLD * 2, "用例前提：单轮新日志超过截断阈值"
    e = _job_entry()
    remote.logs[f"{e.log_dir}/out.log"] = log

    content, rc = await oj._remote_read(_ctx(), e)
    assert rc is None
    assert "截断" not in content, "标记文本不得混进任务日志"
    assert content.encode("utf-8") == log
    assert e.out_offset == len(log), f"offset {e.out_offset} != 真实字节 {len(log)}"

    # 关键后果：再来一轮不得重复读回同一段
    again, rc2 = await oj._remote_read(_ctx(), e)
    assert again == "" and rc2 is None
    assert e.out_offset == len(log)


@pytest.mark.asyncio
async def test_job_read_after_growth_resumes_from_exact_offset(remote):
    first = ("a" * 6000 + "\n").encode()      # 超阈值，旧实现下必然错位
    second = ("b" * 900 + "\n").encode()
    e = _job_entry()
    key = f"{e.log_dir}/out.log"
    remote.logs[key] = first
    c1, _ = await oj._remote_read(_ctx(), e)
    remote.logs[key] = first + second
    c2, _ = await oj._remote_read(_ctx(), e)
    assert c1.encode("utf-8") == first
    assert c2.encode("utf-8") == second, "错位后下一轮会重复/跳丢日志"
    assert e.out_offset == len(first) + len(second)


# ══════════════════════════════════════════════════════════════
#  5. job_list 对账：多会话必须逐会话继续（旧实现两处 return 会中断整轮）
# ══════════════════════════════════════════════════════════════

def _mk_job(job_id: str, pid: int, session: str) -> oj.JobEntry:
    e = oj.JobEntry(
        job_id=job_id, args=["x"], start_ts=time.time(), remote=True,
        ssh_session_id=session, pid=pid, log_dir=f"{_JOB_ROOT}/{job_id}",
        run_id="run-1", raw_cmd="sleep 1",
    )
    oj.JobManager.instance().add(e)
    return e


@pytest.fixture(autouse=True)
def _fresh_mgr():
    with oj.JobManager._instance_lock:
        oj.JobManager._instance = None
    yield
    with oj.JobManager._instance_lock:
        oj.JobManager._instance = None


def _session_fake(monkeypatch, per_session):
    """per_session: session_id -> (alive_output|None, rc_output)；None 表示桥不可达。"""
    async def fake(context, command, timeout=30, keep_full=False):
        sid = getattr(context, "ssh_session_id", "")
        if sid not in per_session:
            return False, "拒绝执行（fail-closed）：Electron Bridge 未就绪"
        alive_out, rc_out = per_session[sid]
        if "kill -0" in command:
            if alive_out is None:
                return False, "远程命令执行失败: timed out"
            return True, alive_out
        return True, rc_out

    monkeypatch.setattr(oj, "run_headless", fake)


@pytest.mark.asyncio
async def test_reconcile_continues_to_next_session_when_all_alive(monkeypatch):
    """会话 A 的任务都活着（正常态）→ 会话 B 已退出的任务仍要被落终态。"""
    _mk_job("a1", 111, "sess-A")
    _mk_job("b1", 222, "sess-B")
    _session_fake(monkeypatch, {
        "sess-A": ("111 ALIVE\n[exit_code=0]", ""),
        "sess-B": ("222 DEAD\n[exit_code=0]", "0\n[exit_code=0]"),
    })
    res = await oj.JobListTool().execute(_ctx(ssh_session_id="sess-A"))
    assert res.success is True
    mgr = oj.JobManager.instance()
    assert mgr.get("a1").status == "running"
    assert mgr.get("b1").status == "finished", \
        "前一个会话无死掉任务就 return，会让后面的会话永远停留在 running"


@pytest.mark.asyncio
async def test_reconcile_unreachable_session_does_not_block_others(monkeypatch):
    _mk_job("a1", 111, "sess-A")
    _mk_job("b1", 222, "sess-B")
    _session_fake(monkeypatch, {
        "sess-B": ("222 DEAD\n[exit_code=0]", "7\n[exit_code=0]"),
    })
    await oj.JobListTool().execute(_ctx(ssh_session_id="sess-A"))
    mgr = oj.JobManager.instance()
    assert mgr.get("a1").status == "running", "不可达会话保持原状态"
    assert mgr.get("b1").status == "failed"
    assert mgr.get("b1").exit_code == 7
