"""allowlist 主机维度 — 「在测试机上批准」不得等于「在生产机上免审」。

背景：授权键原本只有 `工具:命令哈希` 两个维度，`_confirmed_permanent` 是全局集合。
于是一次在测试机上的「始终允许」会让生产机上的同一条命令直接免审 —— 而 prod 强化
的说法是「危险命令必须预登记」，「在任意一台机器上登记过」并不等于「为这台机器
登记过」。本文件钉三件事：

  1. 写入：带 host 的授权落成 `@host|键` 一行，旧的无前缀条目语义不变（全局）；
  2. 匹配：主机限域条目只对该主机命中，全局条目对所有主机命中；
  3. 撤销：撤销某台主机不误伤其他主机，「全部撤销」也不会漏掉 `@host|` 行
     （这是最容易写错的一处：scoped 条目的 startswith 永远匹配不上工具名前缀）。

运行: python -m pytest backend/tests/test_allowlist_host_scope.py -v
"""
import asyncio

import fastapi
import pytest

import agent.harness.reins.gate as gate_mod
from agent.harness.reins.gate import (
    PermissionGate,
    is_valid_allowlist_entry,
    normalize_host,
    scoped_key,
    split_scoped_key,
)

CMD = "systemctl restart nginx"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = str(tmp_path / "allowlist.txt")
    monkeypatch.setattr(gate_mod, "ALLOWLIST_PATH", path)
    g = PermissionGate()
    g.allowlist_ttl_hours = 0
    return g


def _allows(g: PermissionGate, host: str, command: str = CMD) -> bool:
    return g.check_permission("shell_exec", {"command": command},
                              risk="dangerous", host=host)


def _lines() -> list[str]:
    with open(gate_mod.ALLOWLIST_PATH, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln]


async def _noop_async(*_a, **_k):
    """审计写在纯授权语义的用例里不相关（必须是 async：被测端点会 await 它）"""


# ── 写入与读回 ──────────────────────────────────────────────────────────────

def test_主机限域授权落成带前缀的一行(gate):
    key = gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    assert key.startswith("shell_exec:")
    assert "@" not in key, "grant 返回值是裸键，主机前缀只存在于文件/集合条目里"
    assert _lines() == [f"@db-01|{key}"]

    reloaded = PermissionGate()
    assert f"@db-01|{key}" in reloaded._confirmed_permanent, \
        "写进去的主机限域条目必须能被重新加载回来"


def test_旧的无前缀条目仍按全局读回(gate):
    """向后兼容：升级前写入的文件没有主机段，语义必须保持「全局」。"""
    gate.grant("view_file", scope="always")
    reloaded = PermissionGate()
    assert "view_file" in reloaded._confirmed_permanent
    assert reloaded.check_permission("view_file", {}, host="any-host") is True


def test_主机标识大小写与空白归一化(gate):
    gate.grant("shell_exec", scope="always", command=CMD, host="  DB-01 ")
    assert _lines() == [f"@db-01|{gate._grant_key('shell_exec', CMD)}"]
    assert _allows(gate, "db-01") is True
    assert normalize_host(" DB-01 ") == "db-01"
    assert scoped_key("", "k") == "k"


# ── 匹配：只能收紧，不能放宽 ────────────────────────────────────────────────

def test_主机限域只对该主机命中(gate):
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    assert _allows(gate, "db-01") is True
    assert _allows(gate, "db-02") is False, "另一台机器不得继承这条预授权"
    assert _allows(gate, "") is False, "主机未核实时不得命中主机限域条目"


def test_全局条目对所有主机命中(gate):
    gate.grant("shell_exec", scope="always", command=CMD)
    assert _allows(gate, "db-01") is True
    assert _allows(gate, "db-02") is True
    assert _allows(gate, "") is True


def test_prod_预授权判定同样按主机(gate):
    """prod 分支走的是 is_permanently_allowed，不能绕过主机维度。"""
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    assert gate.is_permanently_allowed(
        "shell_exec", {"command": CMD}, host="db-01") is True
    assert gate.is_permanently_allowed(
        "shell_exec", {"command": CMD}, host="db-02") is False


def test_不同主机的同一条命令互不覆盖(gate):
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    gate.grant("shell_exec", scope="always", command=CMD, host="db-02")
    assert _lines() == [
        f"@db-01|{gate._grant_key('shell_exec', CMD)}",
        f"@db-02|{gate._grant_key('shell_exec', CMD)}",
    ]


# ── 撤销 ────────────────────────────────────────────────────────────────────

def test_撤销某主机不误伤其他主机(gate):
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    gate.grant("shell_exec", scope="always", command=CMD, host="db-02")
    gate.revoke("shell_exec", command=CMD, host="db-01")
    assert _allows(gate, "db-01") is False
    assert _allows(gate, "db-02") is True


def test_全部撤销必须覆盖主机限域条目(gate):
    """startswith("shell_exec:") 匹配不到 `@db-01|shell_exec:...`，
    漏了这一层就是「点了全部撤销、文件原封不动」。"""
    key = gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    gate.grant("shell_exec", scope="always", command=CMD)  # 全局条目
    gate.revoke("shell_exec")
    assert _lines() == [], f"主机限域条目未被撤销（key={key}）"
    assert _allows(gate, "db-01") is False
    assert _allows(gate, "") is False


def test_只撤全局不碰主机条目(gate):
    gate.grant("shell_exec", scope="always", command=CMD)
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    gate.revoke("shell_exec", command=CMD, host="")
    assert _allows(gate, "") is False
    assert _allows(gate, "db-01") is True


# ── 条目格式：读侧与写侧同源 ────────────────────────────────────────────────

@pytest.mark.parametrize("line,ok", [
    ("@db-01|shell_exec", True),
    ("@db-01|shell_exec:a1b2c3d4e5f67890", True),
    ("@10.0.0.7|file_write", True),
    ("@2001:db8::1|shell_exec", True),
    ("@|shell_exec", False),          # 主机段为空
    ("@db 01|shell_exec", False),      # 空白
    ("@db-01|shell exec", False),      # 裸键非法
    ("@db-01|shell_exec:zz", False),   # 哈希不是 16 位 hex
    ("db-01|shell_exec", False),       # 缺 @ 前缀 = 整行不是合法裸键
])
def test_主机条目的读写同口径(line, ok):
    valid, _reason = PermissionGate._validate_allowlist_entry(line, 1)
    assert valid is ok
    assert is_valid_allowlist_entry(line) is ok


def test_split_对旧条目原样返回():
    assert split_scoped_key("shell_exec:a1b2") == ("", "shell_exec:a1b2")
    assert split_scoped_key("@db-01|shell_exec") == ("db-01", "shell_exec")


def test_非法主机在写侧被拒且不落盘(gate):
    with pytest.raises(ValueError):
        gate.grant("shell_exec", scope="always", command=CMD, host="db 01")
    import os
    if os.path.exists(gate_mod.ALLOWLIST_PATH):
        assert _lines() == []


# ── 端点接线：grant 取 run 的主机，allow 接受 host ──────────────────────────

def test_grant_端点在_prod_主机上写入主机限域条目(gate, monkeypatch):
    import routes.agent as agent_mod
    from agent.environment_policy import EnvironmentResolution

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    monkeypatch.setitem(
        agent_mod._RUN_ENV, "run-1",
        EnvironmentResolution(effective="prod", requested="prod",
                              source="connection", host="prod-db"))

    asyncio.run(agent_mod.grant_tool_permission(
        agent_mod.PermissionGrantRequest(
            tool="shell_exec", scope="always", command=CMD, run_id="run-1")))

    assert _lines() == [f"@prod-db|{gate._grant_key('shell_exec', CMD)}"], \
        "审批卡上对 prod 主机点的「始终允许」只能落在这台主机上"
    assert _allows(gate, "prod-db") is True
    assert _allows(gate, "test-box") is False


def test_grant_端点主机未知时落全局条目(gate, monkeypatch):
    """对称性回归：host 为空时写入端落全局，匹配端也只查全局 —— 不能出现
    「授权生效但永不命中」的静默故障。"""
    import routes.agent as agent_mod

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    agent_mod._RUN_ENV.pop("run-x", None)
    asyncio.run(agent_mod.grant_tool_permission(
        agent_mod.PermissionGrantRequest(
            tool="shell_exec", scope="always", command=CMD, run_id="run-x")))

    assert _lines() == [gate._grant_key("shell_exec", CMD)]
    assert _allows(gate, "any-host") is True


def test_run_结束后补点始终允许仍限在原主机(gate, monkeypatch):
    """审批卡不随 run 结束消失：run 终态后补点「始终允许」也得落 `@host|`。

    _RUN_ENV 在 event_stream 的 finally 就被清掉，没有墓碑的话晚点几分钟批准
    就拿不到 host → 落成全局条目，主机维度被静默绕过。
    """
    import routes.agent as agent_mod
    from agent.environment_policy import EnvironmentResolution

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    # 墓碑是模块级全局：整只换掉，用例之间互不污染
    monkeypatch.setattr(agent_mod, "_RUN_ENV_GRAVE", {}, raising=True)
    monkeypatch.setitem(
        agent_mod._RUN_ENV, "run-late",
        EnvironmentResolution(effective="prod", requested="prod",
                              source="connection", host="prod-db"))
    agent_mod._forget_run_environment("run-late")  # 模拟 run 终态的 finally

    asyncio.run(agent_mod.grant_tool_permission(
        agent_mod.PermissionGrantRequest(
            tool="shell_exec", scope="always", command=CMD, run_id="run-late")))

    assert _lines() == [f"@prod-db|{gate._grant_key('shell_exec', CMD)}"]
    assert _allows(gate, "prod-db") is True
    assert _allows(gate, "test-box") is False


def test_allow_端点接受主机并落主机条目(gate, monkeypatch):
    import routes.agent as agent_mod

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    out = asyncio.run(agent_mod.allow_tool_permission(
        agent_mod.AllowPermissionRequest(tool="shell_exec", command=CMD, host="db-01")))
    assert out["granted"] is True
    assert out["host"] == "db-01"
    assert _lines() == [f"@db-01|{gate._grant_key('shell_exec', CMD)}"]


def test_allow_端点拒绝非法主机(gate, monkeypatch):
    import routes.agent as agent_mod

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    with pytest.raises(fastapi.HTTPException) as ei:
        asyncio.run(agent_mod.allow_tool_permission(
            agent_mod.AllowPermissionRequest(tool="shell_exec", host="a|b")))
    assert ei.value.status_code == 400
    assert "主机" in str(ei.value.detail)


# ── 端点接线：revoke 的 host 三态（面板的「撤销」按钮就这么传）───────────

def test_revoke_端点传主机名只撤该主机(gate, monkeypatch):
    import routes.agent as agent_mod

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")
    gate.grant("shell_exec", scope="always", command=CMD, host="db-02")

    asyncio.run(agent_mod.revoke_tool_permission(
        "shell_exec", command=CMD, host="db-01"))

    assert _allows(gate, "db-01") is False
    assert _allows(gate, "db-02") is True, "撤销一台主机不得挟带另一台"


def test_revoke_端点空主机只撤全局(gate, monkeypatch):
    """前端全局组的「撤销」传的就是 `host=`（空串，不是不传）。

    空串必须被当成「只撤全局条目」而不是「没指定 → 全撤」：否则点一下全局行的
    撤销，各主机辛苦登记的预授权会一并消失。
    """
    import routes.agent as agent_mod

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    gate.grant("shell_exec", scope="always", command=CMD)
    gate.grant("shell_exec", scope="always", command=CMD, host="db-01")

    asyncio.run(agent_mod.revoke_tool_permission(
        "shell_exec", command=CMD, host=""))

    assert _allows(gate, "") is False
    assert _allows(gate, "db-01") is True


# ── session 授权的主机维度（与永久池同构：会话是跨主机的，主机不是）─────────

def _session_allows(g: PermissionGate, host: str, conv: str = "conv-1",
                    command: str = CMD) -> bool:
    # session 只对非 dangerous 生效（dangerous 每次都要授权），故用 sensitive
    return g.check_permission("shell_exec", {"command": command},
                              risk="sensitive", conversation_id=conv, host=host)


def test_session_授权只对登记它的主机免审(gate):
    """在测试机上点「本会话允许」，同一个对话切到生产机后必须重新询问。

    会话（conversation_id）是跨主机的：一个对话里先连测试机、再连生产机是常态，
    所以 session 不能只按会话键控 —— 否则「测试机上批过一次」变成「这个对话里
    的所有机器都免审」，与 allowlist 修掉的缺口同构（只是风险等级低一阶：
    危险命令本来就不认 session）。
    """
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1",
               host="test-box")
    assert _session_allows(gate, "test-box") is True
    assert _session_allows(gate, "prod-db") is False


def test_session_授权无主机时对所有主机生效(gate):
    """host 为空（本地 run / 桥接不可用）落裸键，保持旧语义 —— 与永久池对称。"""
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1")
    assert _session_allows(gate, "") is True
    assert _session_allows(gate, "prod-db") is True


def test_撤销某主机的_session_授权不误伤另一台(gate):
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1",
               host="db-01")
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1",
               host="db-02")
    gate.revoke("shell_exec", command=CMD, conversation_id="conv-1", host="db-01")
    assert _session_allows(gate, "db-01") is False
    assert _session_allows(gate, "db-02") is True


def test_全部撤销_也能清掉主机限域的_session_授权(gate):
    """撤销按钮走 revoke(tool)（不带 command）时的前缀遍历。

    `@h|tool:hash` 的 startswith(tool + ':') 永远匹配不上 —— 漏了这层就等于
    「点了撤销，授权仍在」，与永久池踩过的坑是同一个。
    """
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1",
               host="db-01")
    gate.grant("shell_exec", scope="session", command=CMD, conversation_id="conv-1",
               host="db-02")
    gate.revoke("shell_exec", conversation_id="conv-1")
    assert _session_allows(gate, "db-01") is False
    assert _session_allows(gate, "db-02") is False


def test_session_授权端点也落主机作用域(gate, monkeypatch):
    """审批卡点「本会话允许」时，host 也会从 run 判定结果带到 session 键上。

    端点原本只给 always 传 host（session 分支写死空串），等于门面改了一半：
    gate 支持了，端点不传，生产机上仍然会因测试机的一次批准而免审。
    """
    import routes.agent as agent_mod
    from agent.environment_policy import EnvironmentResolution

    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    monkeypatch.setitem(agent_mod._RUN_ENV, "run-s", EnvironmentResolution(
        effective="test", requested="test", source="connection", host="db-01"))

    out = asyncio.run(agent_mod.grant_tool_permission(agent_mod.PermissionGrantRequest(
        tool="shell_exec", scope="session", command=CMD,
        run_id="run-s", conversation_id="conv-1")))

    assert out["host"] == "db-01"
    assert _session_allows(gate, "db-01") is True
    assert _session_allows(gate, "db-02") is False
