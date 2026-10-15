"""C-review M11 回归：session 级授权按 conversation_id 键控，不得跨会话泄漏。

修复前：PermissionGate._confirmed_session 是进程级全局 set — 会话 A 的
session 授权在会话 B 的 run 中同样免审。
修复后：_confirmed_session: dict[conversation_id, set[grant_key]]，
check_permission 只查本会话命名空间。

运行: python -m pytest backend/tests/test_session_scope_conversation.py -v
"""
import pytest

from agent.harness.reins import PermissionGate


@pytest.fixture(autouse=True)
def _no_allowlist_io(monkeypatch):
    """隔离 ~/.ckunagentic/allowlist.txt 文件 IO（沙箱 + 避免读入开发机真实 allowlist）"""
    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)


def _gate() -> PermissionGate:
    return PermissionGate()


def test_session_grant_scoped_to_conversation():
    g = _gate()
    g.grant("web_fetch", scope="session", conversation_id="convA")
    # 本会话免审
    assert g.check_permission("web_fetch", {}, conversation_id="convA") is True
    # 其他会话不得免审（M11 核心）
    assert g.check_permission("web_fetch", {}, conversation_id="convB") is False
    # 无会话上下文（旧调用方默认空串）也不得命中
    assert g.check_permission("web_fetch", {}) is False


def test_session_grant_command_binds_key():
    """shell_exec 命令级 session 授权同样按会话键控"""
    g = _gate()
    cmd = "systemctl restart nginx"
    g.grant("shell_exec", scope="session", command=cmd, conversation_id="convA")
    # 同命令 + 同会话（dangerous 忽略 session 缓存，用非 dangerous 风险验证键控）
    assert g.check_permission("shell_exec", {"command": cmd},
                              conversation_id="convA") is True
    # 同命令 + 不同会话 → 不免审
    assert g.check_permission("shell_exec", {"command": cmd},
                              conversation_id="convB") is False


def test_dangerous_still_ignores_session(monkeypatch):
    """B-6 契约保持：dangerous 风险即使本会话 session 授权也不放行"""
    g = _gate()
    g.grant("web_fetch", scope="session", conversation_id="convA")
    assert g.check_permission("web_fetch", {},
                              risk="dangerous", conversation_id="convA") is False


def test_revoke_scoped_conversation():
    g = _gate()
    g.grant("web_fetch", scope="session", conversation_id="convA")
    g.grant("web_fetch", scope="session", conversation_id="convB")
    g.revoke("web_fetch", conversation_id="convA")
    # convA 已撤，convB 保留
    assert g.check_permission("web_fetch", {}, conversation_id="convA") is False
    assert g.check_permission("web_fetch", {}, conversation_id="convB") is True


def test_revoke_without_conversation_removes_all():
    """默认 revoke（conversation_id=None）保持旧语义：撤所有会话"""
    g = _gate()
    g.grant("web_fetch", scope="session", conversation_id="convA")
    g.grant("web_fetch", scope="session", conversation_id="convB")
    g.revoke("web_fetch")
    assert g.check_permission("web_fetch", {}, conversation_id="convA") is False
    assert g.check_permission("web_fetch", {}, conversation_id="convB") is False


def test_once_and_always_unaffected_by_conversation():
    """once 计数器与 permanent allowlist 不受会话键控影响（语义不变）"""
    g = _gate()
    g.grant("web_fetch", scope="once", conversation_id="convA", run_id="runA")
    # once 跨会话仍可消费（计数语义，同 run 内一次性）
    assert g.check_permission("web_fetch", {}, conversation_id="convB", run_id="runA") is True
    assert g.check_permission("web_fetch", {}, conversation_id="convB", run_id="runA") is False

    g.grant("web_fetch2", scope="always", conversation_id="convA")
    assert g.check_permission("web_fetch2", {}, conversation_id="convZ") is True
