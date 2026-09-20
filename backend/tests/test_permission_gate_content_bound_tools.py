# -*- coding: utf-8 -*-
"""内容级判定工具（file_write / file_edit / schedule_op）的授权闭环回归（A4/A5/A6）。

修复前：这些工具按 meta.risk_level 判定，file_write 写 /root/.ssh/authorized_keys
只算 moderate —— 会话级授权即可放行；"始终允许"写进 allowlist 的是裸工具名
`file_write`，于是**任意路径永久免审**，prod 的"危险操作仅接受预授权"闸门根本不会被
触发（那道闸门在 risk=="dangerous" 分支里）。同一条命令手敲进 shell_exec 则是
hardline 无条件阻断。

修复后：工具报出真实执行命令 → FilesystemGuard 按内容判定 → 授权键绑定该命令
（含义 = 允许写这个路径）。本文件锁住这四件事：植入被阻断、卡片显示真实模板、
换路径重新询问、prod 仍要预登记。

运行: python -m pytest backend/tests/test_permission_gate_content_bound_tools.py -v
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.harness.reins.core import extract_bound_command
from agent.tools import create_registry_for_mode
from agent.tools.builtin.ops_common import base64_write_cmd_for_classification

PLANT = "/root/.ssh/authorized_keys"       # hardline：SSH 植入/提权
NGINX = "/etc/nginx/nginx.conf"            # dangerous：敏感路径写
TMP = "/tmp/scratch.txt"                   # safe：普通路径写


@pytest.fixture
def audits(monkeypatch):
    """捕获审计落库（既不写真 DB，也让我们能断言 blocked/denied 结论）"""
    records = []

    async def _rec(**kw):
        records.append(kw)
        return None

    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    for _m in (_guards_mod, _gate_mod):
        monkeypatch.setattr(_m, "append_record_async", _rec)
    return records


def _saddle():
    return SimpleNamespace(registry=create_registry_for_mode("ssh"))


def _gate():
    return PermissionGate(saddle=_saddle())


def _guard(gate):
    return FilesystemGuard(permission_gate=gate, saddle=_saddle())


def _ctx(environment="test") -> RunContext:
    return RunContext.new(task="测试", mode="ssh", config=AgentConfig(),
                          environment=environment)


def _log(ctx, tool, args):
    ctx.tool_calls_log.append({
        "tool": tool, "args": args, "iteration": len(ctx.tool_calls_log), "success": None,
    })


def _write(tool, path):
    """按工具真实参数拼一次调用（file_edit 还带 old/new，均不参与键）"""
    if tool == "file_write":
        return {"path": path, "content": "hello\n"}
    return {"path": path, "old_string": "a", "new_string": "b"}


async def _chain(ctx, gate, guard):
    """按 priority 升序跑真实链路：PermissionGate(8) → FilesystemGuard(10)"""
    d = await gate.check(ctx)
    if d.intercepted:
        return d
    return await guard.check(ctx)


def _bound(tool, args):
    cmd, _free = extract_bound_command(create_registry_for_mode("ssh"), tool, args)
    return cmd


# ── 1. 植入类写入：无条件阻断，不给审批机会 ──

@pytest.mark.parametrize("tool", ["file_write", "file_edit"])
def test_ssh_plant_via_file_tool_is_blocked(audits, tool):
    gate = _gate()
    guard = _guard(gate)
    ctx = _ctx()
    args = _write(tool, PLANT)
    _log(ctx, tool, args)
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    assert "credential" in d.intercept_reason or "persistence" in d.intercept_reason
    assert not any(e.kind == "permission_needed" for e in d.events), \
        "hardline 不允许出现授权卡片 —— 否则用户手一抖就放行了提权"
    blocked = [r for r in audits if r.get("conclusion") == "blocked"]
    assert blocked and blocked[-1]["action"] == tool
    assert blocked[-1]["command_raw"] == base64_write_cmd_for_classification(PLANT)


def test_allowlist_cannot_unblock_hardline(audits):
    """即使把 file_write 整族永久授权，植入路径仍被阻断（底线在 allowlist 之前）"""
    gate = _gate()
    gate.grant("file_write", scope="always")   # 裸工具名条目（旧语义遗留）
    guard = _guard(gate)
    ctx = _ctx()
    _log(ctx, "file_write", _write("file_write", PLANT))
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted


# ── 2. 敏感路径写入：卡片显示真实落盘模板 ──

def test_sensitive_write_emits_card_with_real_template(audits):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    args = _write("file_write", NGINX)
    _log(ctx, "file_write", args)
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    cards = [e for e in d.events if e.kind == "permission_needed"]
    assert len(cards) == 1
    payload = cards[0].payload
    assert payload["risk"] == "dangerous"
    # 用户看到的必须与实际执行的一致：payload 里的 command 就是授权键绑定的那条串
    assert payload["command"] == base64_write_cmd_for_classification(NGINX)
    assert payload["normalized_command"]
    assert payload["explain"], "英文 reason 对小白不可读，卡片必须带中文后果说明"


# ── 3. 授权键按路径绑定（A5 核心：批了 A 不放行 B） ──

def test_always_allow_binds_path_not_tool(audits):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    args_a = _write("file_write", NGINX)
    cmd_a = _bound("file_write", args_a)
    _log(ctx, "file_write", args_a)
    asyncio.run(_chain(ctx, gate, guard))
    # 前端「始终允许」把 payload 的 command 原文回传（见 AgentRunPanel.grantPermission）
    key = gate.grant("file_write", scope="always", command=cmd_a)
    assert key.startswith("file_write:") and key != "file_write"
    # 同一路径 → 免审放行
    ctx2 = _ctx()
    _log(ctx2, "file_write", args_a)
    assert not asyncio.run(_chain(ctx2, gate, guard)).intercepted
    # 换一条路径 → 必须重新询问
    ctx3 = _ctx()
    _log(ctx3, "file_write", _write("file_write", "/etc/hosts"))
    d3 = asyncio.run(_chain(ctx3, gate, guard))
    assert d3.intercepted, "授权键若退化成裸工具名，这里就会被放行"
    assert any(e.kind == "permission_needed" for e in d3.events)


def test_session_grant_does_not_leak_across_paths(audits):
    """moderate + 可逆性无关：内容 safe 的写入走 gate 的 session 授权，仍按路径绑定"""
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    args_tmp = _write("file_write", TMP)
    _log(ctx, "file_write", args_tmp)
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted and any(e.kind == "permission_needed" for e in d.events), \
        "file_write 不可逆（覆盖已有内容），首次仍要授权"
    cmd = _bound("file_write", args_tmp)
    gate.grant("file_write", scope="session", command=cmd, conversation_id=ctx.conversation_id)
    ctx2 = _ctx()
    _log(ctx2, "file_write", args_tmp)
    assert not asyncio.run(_chain(ctx2, gate, guard)).intercepted
    ctx3 = _ctx()
    _log(ctx3, "file_write", _write("file_write", "/tmp/other.txt"))
    assert asyncio.run(_chain(ctx3, gate, guard)).intercepted


# ── 4. prod：内容 dangerous 的写入只认预登记 ──

def test_prod_sensitive_write_needs_preauthorization(audits):
    gate, ctx = _gate(), _ctx(environment="prod")
    guard = _guard(gate)
    _log(ctx, "file_write", _write("file_write", NGINX))
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted and "生产环境" in d.intercept_reason
    denied = [r for r in audits if r.get("conclusion") == "denied"]
    assert denied and denied[-1]["result"].startswith("prod_deny_no_allowlist")
    # prod 下临时审批通道根本不给（没有 permission_needed 卡片）
    assert not any(e.kind == "permission_needed" for e in d.events)


def test_prod_preauthorized_path_passes(audits):
    gate, ctx = _gate(), _ctx(environment="prod")
    guard = _guard(gate)
    args = _write("file_write", NGINX)
    gate.grant("file_write", scope="always", command=_bound("file_write", args))
    _log(ctx, "file_write", args)
    assert not asyncio.run(_chain(ctx, gate, guard)).intercepted


# ── 5. 审批三态与拒绝记忆对内容工具同样生效 ──

def test_approval_timeout_marks_card_expired(audits):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    args = _write("file_write", NGINX)
    _log(ctx, "file_write", args)
    asyncio.run(_chain(ctx, gate, guard))
    from agent.harness.reins.core import _perm_pending, tool_call_signature
    sig = tool_call_signature(ctx.tool_calls_log[-1])
    ctx.metadata["permission_pending"][sig] = time.monotonic() - 999
    _log(ctx, "file_write", args)
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    assert any(e.kind == "permission_expired" for e in d.events)
    assert not any(e.kind == "permission_needed" for e in d.events)


def test_denied_path_is_not_recarded(audits):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    args = _write("file_write", NGINX)
    cmd = _bound("file_write", args)
    gate.deny_command("file_write", cmd, ctx.run_id)
    _log(ctx, "file_write", args)
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    assert not any(e.kind == "permission_needed" for e in d.events), \
        "用户点过拒绝的目标不得重复弹卡"


# ── 6. 对照组：shell 通道口径未被内容级改造削弱 ──

@pytest.mark.parametrize("command,conclusion,result_prefix", [
    # hardline 仍阻断，dangerous 仍审批 —— shell 通道口径没有被内容级改造削弱
    ("rm -rf /", "blocked", "hardline"),
    ("systemctl restart nginx", "denied", "approval_needed"),
])
def test_shell_channel_behavior_unchanged(audits, command, conclusion, result_prefix):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    _log(ctx, "shell_exec", {"command": command})
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    got = [r for r in audits if r.get("conclusion") == conclusion
           and str(r.get("result", "")).startswith(result_prefix)]
    assert got, f"shell_exec 的 {command!r} 应落 {conclusion}/{result_prefix} 审计，" \
                f"实得 {[(r.get('conclusion'), r.get('result')) for r in audits]}"


def test_job_run_free_form_command_still_default_approval(audits):
    """job_run 的命令是模型自由文本 → 「未知动词默认审批」必须仍然生效"""
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    _log(ctx, "job_run", {"cmd": "python3 rotate_logs.py"})
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted, "未入只读白名单的命令不能因为不是 shell_exec 就免审"


# ── 7. 定时任务：入库前判一次，雷在创建时就被挡 ──

def test_schedule_op_hardline_command_blocked(audits):
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    _log(ctx, "schedule_op", {"action": "create", "task": "清日志",
                              "command": "rm -rf /"})
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted
    assert [r for r in audits if r.get("conclusion") == "blocked"]


def test_schedule_op_non_create_falls_back_to_tool_level(audits):
    """action != create 没有命令可判 → 退回 ToolMeta（moderate 不可逆，仍需会话授权），
    且不会被误记成内容级阻断。"""
    gate, ctx = _gate(), _ctx()
    guard = _guard(gate)
    _log(ctx, "schedule_op", {"action": "list"})
    d = asyncio.run(_chain(ctx, gate, guard))
    assert d.intercepted and "moderate" in d.intercept_reason
    assert not [r for r in audits if r.get("conclusion") == "blocked"]
