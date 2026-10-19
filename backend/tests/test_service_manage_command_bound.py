"""回归：service_manage 的授权与否决都必须绑定真实 systemctl 命令。

修复前 ServiceManageTool 不实现 bound_command ⇒ extract_bound_command 回
(None, False)：FilesystemGuard 因 bound 为空整段跳过内容级判定，而 _grant_key
退化成裸工具名。后果是跨动作跨 unit 的授权串用 —— 在 test 环境对
`service_manage(status, nginx)` 点「始终允许」，allowlist 里存的是 `service_manage`；
prod 环境下 `service_manage(stop, sshd)`（risk 已按 destructive_actions 升级为
dangerous）走同一把键 ⇒ is_permanently_allowed 直接命中，一条都没问过就把生产
服务停掉。file_write / file_edit / schedule_op 早已实现这一路，理由写在
gate._grant_key 的 docstring 里（"没有这一路…等于允许写任意路径"）。

同时钉住相反方向：授权要逐条收紧，否决要整体生效 —— 用户点「拒绝」记下的
工具级键，必须继续覆盖该工具的命令级调用（S2-01 的不变量）。
"""
import pytest

import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.reins import PermissionGate
from agent.harness.reins.core import extract_bound_command
from agent.tools import create_registry


@pytest.fixture
def audits(monkeypatch):
    records = []

    async def _rec(**kw):
        records.append(kw)
        return None

    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    for _m in (_guards_mod, _gate_mod):
        monkeypatch.setattr(_m, "append_record_async", _rec)
    return records


def _registry():
    return create_registry()


def _gate():
    from types import SimpleNamespace
    return PermissionGate(saddle=SimpleNamespace(registry=_registry()))


STATUS_NGINX = {"action": "status", "name": "nginx"}
STOP_SSHD = {"action": "stop", "name": "sshd"}


def test_service_manage_reports_the_real_command():
    """bound_command 必须与 execute 用的是同一条模板，否则键与真实动作脱钩。"""
    from agent.tools.builtin.ops_service import service_cmd
    registry = _registry()

    for args in (STATUS_NGINX, STOP_SSHD):
        bound, is_free_form = extract_bound_command(registry, "service_manage", args)
        assert bound == service_cmd(args["action"], args["name"])
        # 模板由代码固定、动词不由模型选 ⇒ 走 constructed 口径，不是自由文本
        assert is_free_form is False

    # 参数非法（不会被执行）时保持 None，与 file_write 的越界路径同法
    assert extract_bound_command(registry, "service_manage", {"action": "halt", "name": "x"})[0] is None
    assert extract_bound_command(registry, "service_manage", {"action": "stop", "name": "a b; rm -rf /"})[0] is None


def test_status_grant_does_not_authorize_stop_in_prod(audits):
    """缺陷本体：status 换来的「始终允许」不能成为 prod 停服务的预授权。"""
    gate = _gate()
    run_id = "run-svc-1"

    gate.grant("service_manage", scope="always",
               command=extract_bound_command(_registry(), "service_manage", STATUS_NGINX)[0],
               conversation_id="c1", run_id=run_id)

    bound_stop = extract_bound_command(_registry(), "service_manage", STOP_SSHD)[0]
    assert bound_stop == "systemctl stop sshd"
    assert gate.is_permanently_allowed("service_manage", STOP_SSHD, bound_command=bound_stop) is False

    # 同一条命令重复调用才算预授权命中（收紧但不误伤）
    bound_status = extract_bound_command(_registry(), "service_manage", STATUS_NGINX)[0]
    assert gate.is_permanently_allowed("service_manage", STATUS_NGINX, bound_command=bound_status) is True


def test_tool_level_denial_still_covers_command_bound_calls(audits):
    """否决与授权方向相反：工具级拒绝必须压住该工具的每一次命令级调用。"""
    gate = _gate()
    run_id = "run-svc-2"
    key = gate.deny_command("service_manage", None, run_id)
    assert key == "service_manage"

    bound_stop = extract_bound_command(_registry(), "service_manage", STOP_SSHD)[0]
    assert gate.is_denied("service_manage", bound_stop, run_id) is True
    # 拒绝按 run 隔离，别的 run 不受影响
    assert gate.is_denied("service_manage", bound_stop, "other-run") is False
