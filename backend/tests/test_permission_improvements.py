# -*- coding: utf-8 -*-
"""权限边界五项改进回归测试。

覆盖：
  问题 1 — once 授权按 run_id 隔离，跨 run 竞态消除；cleanup_run_once 只清本 run
  问题 3 — allowlist 行校验（_validate_allowlist_entry）
  问题 4 — visible_terminal 拒绝后 deny_command 标记：
           check_permission 拒绝优先于 permanent/once；FilesystemGuard 不再重发卡片
  问题 5 — prod 环境 destructive action（service_manage stop/restart）风险升级 dangerous

运行: python -m pytest backend/tests/test_permission_improvements.py -v
"""
import asyncio

import pytest

import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.tools import ToolMeta
from agent.tools.builtin.ops_service import ServiceManageTool

CMD = "systemctl restart nginx"  # 运维动词 → dangerous（走审批路径）
CMD2 = "rm -rf /tmp/scratch"


@pytest.fixture(autouse=True)
def _no_file_io(monkeypatch):
    """隔离 allowlist 文件 IO + 审计落库"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)

    async def _noop_audit(**kw):
        return None
    for _m in (_guards_mod, _gate_mod):
        monkeypatch.setattr(_m, "append_record_async", _noop_audit)


def _ctx(environment: str = "test") -> RunContext:
    return RunContext.new(task="测试", mode="ssh", config=AgentConfig(),
                          environment=environment)


def _gate() -> PermissionGate:
    return PermissionGate()


# ── 问题 1：once 授权按 run_id 隔离 ──

def test_once_grant_scoped_to_run():
    g = _gate()
    g.grant("web_fetch", scope="once", run_id="runA")
    # 本 run 可消费
    assert g.check_permission("web_fetch", {}, run_id="runA") is True
    # 消费后归零，重发需再授权
    assert g.check_permission("web_fetch", {}, run_id="runA") is False
    # 授权在 runA，runB 不得消费（竞态核心）
    g.grant("web_fetch2", scope="once", run_id="runA")
    assert g.check_permission("web_fetch2", {}, run_id="runB") is False


def test_once_grant_requires_run_id():
    """once 授权必须携带 run_id：空 run_id 直接拒绝（原全局池 fallback 已删除，
    防止 A run 未消费的授权被 B run 跨 run 竞态消费）"""
    g = _gate()
    with pytest.raises(ValueError):
        g.grant("web_fetch", scope="once")  # run_id 缺失 → 拒绝
    # 未写入任何池
    assert g.check_permission("web_fetch", {}, run_id="runX") is False


def test_deny_command_requires_run_id():
    with pytest.raises(ValueError):
        _gate().deny_command("shell_exec", "rm -rf /tmp/x", "")


def test_cleanup_run_once_only_clears_target_run():
    g = _gate()
    g.grant("t1", scope="once", run_id="runA")
    g.grant("t2", scope="once", run_id="runA")
    g.grant("t3", scope="once", run_id="runB")
    released = g.cleanup_run_once("runA")
    assert released == 2, "runA 有两条未消费 once 授权"
    # runA 清空，runB 不受影响
    assert g.check_permission("t1", {}, run_id="runA") is False
    assert g.check_permission("t3", {}, run_id="runB") is True
    # 重复清理返回 0
    assert g.cleanup_run_once("runA") == 0


# ── 问题 3：allowlist 行校验 ──

def test_validate_allowlist_entry():
    """合法格式：纯工具名 / 工具名:16位hex命令哈希；其余一律拒绝"""
    validate = PermissionGate._validate_allowlist_entry
    # 合法
    ok, reason = validate("web_fetch", 1)
    assert ok and not reason
    ok, reason = validate("shell_exec:a1b2c3d4e5f6a7b8", 2)
    assert ok and not reason
    # 非法：空行 / 注释 / 含空格 / 哈希段非 16 位 hex
    for bad in ("", "  ", "# comment", "bad key with space",
                "shell_exec:short", "shell_exec:A1B2C3D4E5F6A7B8"):
        ok, reason = validate(bad, 3)
        assert not ok and reason, f"{bad!r} 应被判为非法"


# ── 问题 4：拒绝标记机制 ──

def test_deny_scoped_to_run():
    g = _gate()
    g.deny_command("shell_exec", CMD, "runA")
    # 本 run 拒绝生效
    assert g.is_denied("shell_exec", CMD, "runA") is True
    # 其他 run 不受影响
    assert g.is_denied("shell_exec", CMD, "runB") is False


def test_denied_overrides_permanent_and_once():
    """拒绝标记优先级最高：即使命令已永久授权或有未消费的 once 授权，本 run 仍拦截"""
    g = _gate()
    # 预置：permanent allowlist 已含该命令 + once 授权一条
    g._confirmed_permanent.add(g._grant_key("shell_exec", CMD))
    g.grant("shell_exec", scope="once", command=CMD, run_id="runA")

    # 拒绝前：permanent 直接放行
    assert g.check_permission("shell_exec", {"command": CMD}, run_id="runA") is True

    # 用户在 visible_terminal 拒绝 → 标记
    g.deny_command("shell_exec", CMD, "runA")

    # 拒绝后：即使 permanent + once 都在，也直接拒绝
    assert g.check_permission("shell_exec", {"command": CMD}, run_id="runA") is False
    # 其他 run 不受该拒绝影响（permanent 依旧放行）
    g2_key = g._grant_key("shell_exec", CMD)
    assert g2_key in g._confirmed_permanent


def test_deny_strictly_run_scoped():
    """拒绝记录严格按 run 隔离（原 run_id="" 全局拒绝语义已随全局池一并删除：
    空 run_id 拒绝无法随 run 终态清理，改为直接拒绝写入）"""
    g = _gate()
    with pytest.raises(ValueError):
        g.deny_command("shell_exec", CMD2, "")
    g.deny_command("shell_exec", CMD2, "runA")
    assert g.is_denied("shell_exec", CMD2, "runA") is True
    # 其他 run 不受该拒绝影响（跨 run 不再泄漏）
    assert g.is_denied("shell_exec", CMD2, "runAny") is False
    # 同 run 内 check_permission 直接拒绝
    assert g.check_permission("shell_exec", {"command": CMD2}, run_id="runA") is False


def test_once_grant_after_deny_lifts_deny():
    """拒绝后再点「允许本次」→ 显式授权必须生效。

    回归点：无命令可绑定的工具（授权键是裸工具名，如 ops_* 的只读类工具）一次
    拒绝即整 run 禁用 —— check_permission 先查 is_denied 直接 False，之后每次
    授权只写进无人消费的池子，卡片反复弹到审批超时，而审计却记为 approved。
    """
    g = _gate()
    g.deny_command("web_fetch", None, "runA")
    assert g.check_permission("web_fetch", {"url": "https://a"}, run_id="runA") is False

    g.grant("web_fetch", scope="once", run_id="runA")
    assert g.check_permission("web_fetch", {"url": "https://a"}, run_id="runA") is True


def test_deny_after_once_grant_still_blocks():
    """顺序相反时优先权在拒绝：先授权后拒绝 → 残留的 once 额度也不得消费"""
    g = _gate()
    g.grant("web_fetch", scope="once", run_id="runA")
    g.deny_command("web_fetch", None, "runA")
    assert g.check_permission("web_fetch", {"url": "https://a"}, run_id="runA") is False


def test_session_grant_does_not_lift_deny():
    """session/always 授权不解除拒绝标记：本 run 的拒绝仍优先于一揽子授权（原契约不变）"""
    g = _gate()
    g.deny_command("web_fetch", None, "runA")
    g.grant("web_fetch", scope="session", conversation_id="c1", run_id="runA")
    assert g.check_permission(
        "web_fetch", {"url": "https://a"}, conversation_id="c1", run_id="runA") is False


def test_cleanup_run_denied():
    g = _gate()
    g.deny_command("shell_exec", CMD, "runA")
    g.deny_command("shell_exec", CMD2, "runA")
    g.deny_command("shell_exec", "ls -la", "runB")
    cleared = g.cleanup_run_denied("runA")
    assert cleared == 2
    assert g.is_denied("shell_exec", CMD, "runA") is False
    assert g.is_denied("shell_exec", CMD2, "runA") is False
    assert g.is_denied("shell_exec", "ls -la", "runB") is True
    assert g.cleanup_run_denied("runA") == 0


def _guard() -> FilesystemGuard:
    gate = PermissionGate()
    gate._confirmed_permanent = set()
    return FilesystemGuard(permission_gate=gate)


def _log(ctx: RunContext, command: str = CMD) -> None:
    ctx.tool_calls_log.append({
        "tool": "shell_exec",
        "args": {"command": command},
        "iteration": len(ctx.tool_calls_log),
        "success": None,
    })


def test_filesystem_guard_denied_no_card_and_intercepted():
    """拒绝过的命令重发：拦截但不发 permission_needed 卡片，注入「已被拒绝」提示"""
    ctx = _ctx()
    _log(ctx)
    guard = _guard()
    # 用户此前在 visible_terminal 拒绝了该命令
    guard.permission_gate.deny_command("shell_exec", CMD, ctx.run_id)

    d = asyncio.run(guard.check(ctx))
    assert d.intercepted, "拒绝过的命令必须拦截"
    assert not any(e.kind == "permission_needed" for e in d.events), \
        "拒绝过的命令不得重发授权卡片（问题 4 核心）"
    assert d.inject_messages, "必须注入拒绝提示引导 LLM 换路"
    assert any("拒绝" in m.get("content", "") for m in d.inject_messages)


def test_filesystem_guard_not_denied_still_emits_card():
    """对照组：未拒绝的 dangerous 命令仍走正常审批卡片流程"""
    ctx = _ctx()
    _log(ctx)
    d = asyncio.run(_guard().check(ctx))
    assert d.intercepted
    assert any(e.kind == "permission_needed" for e in d.events)


# ── 问题 5：prod 环境 destructive action 风险升级 ──

def test_service_manage_meta_declares_destructive_actions():
    meta = ServiceManageTool.meta
    assert meta.risk_level == "moderate"
    assert "stop" in meta.destructive_actions
    assert "restart" in meta.destructive_actions
    assert "status" not in meta.destructive_actions
    assert "start" not in meta.destructive_actions


def _mk_meta(risk: str = "moderate", destructive: frozenset = frozenset()) -> ToolMeta:
    return ToolMeta(risk_level=risk, reversible=False,
                    destructive_actions=destructive)


def test_effective_risk_prod_upgrades_destructive():
    g = _gate()
    meta = _mk_meta(destructive=frozenset({"stop", "restart"}))
    # prod + stop → dangerous
    assert g._get_effective_risk("service_manage", meta,
                                 {"action": "stop", "name": "nginx"}, "prod") == "dangerous"
    # prod + restart → dangerous
    assert g._get_effective_risk("service_manage", meta,
                                 {"action": "restart", "name": "nginx"}, "prod") == "dangerous"


def test_effective_risk_no_upgrade_cases():
    g = _gate()
    meta = _mk_meta(destructive=frozenset({"stop", "restart"}))
    # prod + 非破坏 action（status/start）→ 保持 moderate
    assert g._get_effective_risk("service_manage", meta,
                                 {"action": "status", "name": "nginx"}, "prod") == "moderate"
    assert g._get_effective_risk("service_manage", meta,
                                 {"action": "start", "name": "nginx"}, "prod") == "moderate"
    # test 环境即使破坏 action 也不升级（升级仅限 prod）
    assert g._get_effective_risk("service_manage", meta,
                                 {"action": "stop", "name": "nginx"}, "test") == "moderate"
    # 空参数 → base
    assert g._get_effective_risk("service_manage", meta, {}, "prod") == "moderate"
    # 无 destructive 声明的工具 → base
    plain = _mk_meta()
    assert g._get_effective_risk("any_tool", plain, {"action": "stop"}, "prod") == "moderate"


def test_effective_risk_dangerous_ignores_session_in_check_permission():
    """升级后的 dangerous 走 check_permission(risk="dangerous") 路径 → session 缓存失效。
    间接验证：PermissionGate.check_permission 对 dangerous 风险即使有 session 授权也拒绝。"""
    g = _gate()
    g.grant("service_manage", scope="session", conversation_id="convA")
    # destructive 升级为 dangerous 后，session 缓存不生效
    assert g.check_permission("service_manage", {"action": "stop"},
                              risk="dangerous", conversation_id="convA") is False
    # 非 dangerous（如 status）走正常路径，session 缓存生效
    assert g.check_permission("service_manage", {"action": "status"},
                              conversation_id="convA") is True
