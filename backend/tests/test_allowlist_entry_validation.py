"""授权清单条目在**写入侧**的格式校验（读侧一直有，写侧没有）。

`_validate_allowlist_entry` 只在加载时校验，`grant(scope="always")` 却把
`tool_name` 原样落进 `allowlist.txt`（一行一条）。两种坏形态都能写进去：
  - 带尾空格的名字：`check_permission` 永远匹配不上它，面板显示「已授权」，
    事故当天才发现这条预登记是空的；重启后读侧还会按非法行丢掉并告警。
  - 带 `\\n` 的名字：一次授权写成**两行**，等于凭空多出一条没人批准过的
    预授权记录 —— 手动登记入口是设置面板里的自由文本框。
本文件双向钉：坏名字必须进不了文件，而合法名字（含 `.`/`-`/`_`）照旧一次写入、
一次读回。路由侧同样钉住 400 而不是 500 —— 那条路径的输入是用户手打的文本。

运行: python -m pytest backend/tests/test_allowlist_entry_validation.py -v
"""
import pytest

import agent.harness.reins.gate as gate_mod
from agent.harness.reins.gate import PermissionGate, is_valid_allowlist_entry
from pathlib import Path


async def _noop_async(*_a, **_k):
    """审计写在上面几条用例里不相关，桩掉它聚焦授权本身"""

BAD_NAMES = [
    "shell_exec ",          # 尾空格：写进去匹配不上，读回来被判非法丢弃
    " shell_exec",
    "shell\nexec",          # 换行：一次授权落两行 = 伪造第二条预授权
    "shell_exec\n",
    "tool;rm -rf /",        # 分隔符不是工具名字符集
    "路径/工具",              # 非 ASCII 同样无法 round-trip
    "",
]


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = str(tmp_path / "allowlist.txt")
    monkeypatch.setattr(gate_mod, "ALLOWLIST_PATH", path)
    g = PermissionGate()
    g.allowlist_ttl_hours = 0
    return g


@pytest.mark.parametrize("name", BAD_NAMES)
def test_malformed_tool_name_never_grants(gate, name):
    with pytest.raises(ValueError):
        gate.grant(name, scope="always")
    # 抛错之外还必须什么都没落盘 —— 半条脏记录比没有更糟
    import os
    if os.path.exists(gate_mod.ALLOWLIST_PATH):
        assert open(gate_mod.ALLOWLIST_PATH, encoding="utf-8").read().strip() == ""


@pytest.mark.parametrize("name", ["shell_exec", "ops_file", "my_tool.v2-x", "read_image"])
def test_legitimate_tool_names_round_trip(gate, name):
    """反向锁：收紧只挡不合法格式，注册表里的真实工具名一次写入、一次读回"""
    key = gate.grant(name, scope="always")
    assert key == name
    reloaded = PermissionGate()
    assert key in reloaded._confirmed_permanent, "写进去的键必须能被读回来"


def test_command_bound_key_still_writes_single_line(gate):
    """命令级绑定键（tool:16hex）是合法形态，不能被写侧校验误伤"""
    key = gate.grant("shell_exec", scope="always", command="systemctl restart nginx")
    assert key.startswith("shell_exec:") and len(key.split(":")[1]) == 16
    lines = [ln for ln in open(gate_mod.ALLOWLIST_PATH, encoding="utf-8").read().splitlines() if ln]
    assert lines == [key], f"一次授权应对应一行，实得 {lines}"


def test_newline_name_would_have_fabricated_a_second_line(gate):
    """把守卫摘掉就能看出危害：这条用例证明校验真的在挡东西"""
    with pytest.raises(ValueError):
        gate.grant("shell_exec\nops_file", scope="always")
    # 若未拦截，文件里会出现两条独立记录（第二条从没人批准过）
    content = ""
    import os
    if os.path.exists(gate_mod.ALLOWLIST_PATH):
        content = open(gate_mod.ALLOWLIST_PATH, encoding="utf-8").read()
    assert "ops_file" not in content


@pytest.mark.parametrize("line,ok", [
    ("shell_exec", True),
    ("shell_exec:a1b2c3d4e5f67890", True),
    ("my_tool.v2-x", True),
    ("shell_exec ", False),
    ("路径", False),
    ("shell_exec:zz", False),
])
def test_read_and_write_sides_share_one_verdict(line, ok):
    """读侧 _validate_allowlist_entry 与写侧 is_valid_allowlist_entry 必须同口径，
    否则「写得进、读不回」或「读得出、写不进」都会重新出现"""
    valid, _reason = PermissionGate._validate_allowlist_entry(line, 1)
    assert valid is ok
    assert is_valid_allowlist_entry(line) is ok


def test_grant_model_bounds_tool_length():
    """长度上限落在请求模型（allowlist 是无界追加写的文本文件，字符集管不住长度）"""
    import pydantic
    from routes.agent import AllowPermissionRequest, PermissionGrantRequest
    with pytest.raises(pydantic.ValidationError):
        PermissionGrantRequest(tool="a" * 200, scope="always")
    with pytest.raises(pydantic.ValidationError):
        AllowPermissionRequest(tool="a" * 200)
    # 反向锁：正常长度照旧通过
    assert PermissionGrantRequest(tool="shell_exec", scope="always").tool == "shell_exec"


@pytest.mark.asyncio
async def test_allow_endpoint_answers_400_not_500(gate, monkeypatch):
    """手动登记走面板自由文本：坏名字必须 400 带原因。
    原实现不接 gate 的 ValueError —— 同样的输入直接 500（用户只看到一个红叉）。
    这里直接调路由函数而不走 TestClient：本沙箱里起 HTTP 栈会挂。"""
    import fastapi
    import routes.agent as agent_mod
    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    req = agent_mod.AllowPermissionRequest(tool="shell_exec ")
    with pytest.raises(fastapi.HTTPException) as ei:
        await agent_mod.allow_tool_permission(req)
    assert ei.value.status_code == 400
    assert "格式" in str(ei.value.detail) or "空白" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_allow_endpoint_still_accepts_valid_name(gate, monkeypatch):
    """反向锁：合法登记照旧成功，别让校验把正门也堵上"""
    import routes.agent as agent_mod
    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    out = await agent_mod.allow_tool_permission(agent_mod.AllowPermissionRequest(tool="shell_exec"))
    assert out["granted"] is True and out["grant_key"] == "shell_exec"


# ── 半写损坏与隔离（CONTEXT.md「半写损坏」「隔离」两条口径）──────────────────


def test_persist_leaves_no_tmp_and_is_atomic(gate):
    """落盘必须 tmp+os.replace 原子写：授权后目录里不得残留 .tmp 半成品。"""
    gate.grant("shell_exec", scope="always", command="systemctl restart nginx")
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    leftovers = [p for p in allowlist.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # 正常路径走通：文件内容读得回来（原子 replace 不破坏可读性）
    assert "shell_exec:" in allowlist.read_text(encoding="utf-8")


def test_atomic_replace_cleans_tmp_on_failure(gate):
    """写失败路径（复核 nit）：os.replace 抛错时 finally 不得残留 .pid.tmp ——
    与 workflow._write_artifact 的收尾同口径，否则残留随 pid 积累。"""
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    blocker = allowlist.parent / "blocked-dir"
    blocker.mkdir()
    try:
        # 用目录占住目标路径：tmp 写成功、os.replace(file → dir) 抛 OSError
        with pytest.raises(OSError):
            gate_mod.PermissionGate._atomic_replace_in_dir(str(blocker), "payload")
        assert not [p for p in allowlist.parent.iterdir() if p.name.endswith(".tmp")], \
            "replace 失败后 tmp 必须被 finally 清掉"
    finally:
        blocker.rmdir()


def test_torn_write_quarantines_whole_file_not_subset(gate, caplog):
    """半写文件（合法行 + 截断的最后一行）必须整体隔离，不得按子集继续用。

    旧行为：跳过非法行、用可读子集起跑 —— 下一次 grant 会把子集写回定稿，
    真实基准里的其他预授权就静默蒸发了。新行为：整文件改名
    .corrupt-<ts>.bak 留证，内存集按空集起算并如实上报。
    """
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    real_key = "shell_exec:a1b2c3d4e5f67890"
    allowlist.write_text(real_key + "\nops_file:deadbeefdeadbe\n", encoding="utf-8")
    reloaded = PermissionGate()
    # 内存集不携带半写文件的可读子集
    assert reloaded._confirmed_permanent == set()
    # 原文件被隔离留证，证据完整保留
    assert not allowlist.exists()
    baks = [p for p in allowlist.parent.iterdir() if ".corrupt-" in p.name]
    assert len(baks) == 1
    assert real_key in baks[0].read_text(encoding="utf-8")
    # 隔离被如实上报（error 级日志，不是静默）
    assert any("隔离留证" in r.message for r in caplog.records)


def test_quarantined_baseline_not_resurrected_by_next_grant(gate):
    """隔离后下一次 grant 写出的是全新基准，不会把半写文件的内容混回去。"""
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    allowlist.write_text(
        "shell_exec:a1b2c3d4e5f67890\nops_file:deadbeef\n", encoding="utf-8")
    fresh = PermissionGate()
    fresh.grant("my_tool", scope="always")
    content = allowlist.read_text(encoding="utf-8")
    assert content == "my_tool\n", f"只应落新授权，实得 {content!r}"
    assert "shell_exec:a1b2" not in content
    assert "deadbeef" not in content


def test_clean_allowlist_still_loads_without_quarantine(gate):
    """反向锁：合法文件（含注释与空行）照旧整读，隔离不得误伤正常路径。"""
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    allowlist.write_text(
        "# 手工登记\n\nshell_exec:a1b2c3d4e5f67890\n@db-01|my_tool\n",
        encoding="utf-8")
    reloaded = PermissionGate()
    assert reloaded._confirmed_permanent == {
        "shell_exec:a1b2c3d4e5f67890", "@db-01|my_tool"}
    assert allowlist.exists(), "合法文件不得被隔离改名"
    assert not [p for p in allowlist.parent.iterdir() if ".corrupt-" in p.name]


def test_workflow_artifact_write_is_atomic(tmp_workspace):
    """workflow 工件落盘同口径：写入成功 + 不残留 .tmp（撕裂的 spec.md 会被
    get_phase 当有效阶段读回）。"""
    from agent import workflow
    target = Path(tmp_workspace) / "conv1" / "spec.md"
    out = workflow._write_artifact(target, "# spec\n内容")
    assert out == "# spec\n内容"
    assert target.read_text(encoding="utf-8") == "# spec\n内容"
    assert [p for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []


# ── hardline 预检（Q-B4①：allowlist 放不走的命令不再登记死预授权）────────────

HARDLINE_CMD = "rm -rf /"


@pytest.mark.asyncio
async def test_allow_endpoint_rejects_hardline_command(gate, monkeypatch):
    """hardline 命令在 FilesystemGuard 无条件阻断，allowlist 写了必落空 ——
    端点必须 400 带不可放行原因，而不是 200 登记「已授权」的死预授权。"""
    import fastapi
    import routes.agent as agent_mod
    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    req = agent_mod.AllowPermissionRequest(tool="shell_exec", command=HARDLINE_CMD)
    with pytest.raises(fastapi.HTTPException) as ei:
        await agent_mod.allow_tool_permission(req)
    assert ei.value.status_code == 400
    assert "不可放行" in str(ei.value.detail)
    # 拒绝之外什么都没落盘
    allowlist = Path(gate_mod.ALLOWLIST_PATH)
    assert not allowlist.exists() or allowlist.read_text(encoding="utf-8").strip() == ""


@pytest.mark.asyncio
async def test_grant_endpoint_rejects_hardline_command(gate, monkeypatch):
    """grant 与 allow 同判据：hardline 直接 400，不落审计 approved 死键"""
    import fastapi
    import routes.agent as agent_mod
    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    req = agent_mod.PermissionGrantRequest(
        tool="shell_exec", scope="once", command=HARDLINE_CMD, run_id="run-h")
    with pytest.raises(fastapi.HTTPException) as ei:
        await agent_mod.grant_tool_permission(req)
    assert ei.value.status_code == 400
    assert "不可放行" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_allow_endpoint_still_accepts_dangerous_command(gate, monkeypatch):
    """反向锁：dangerous（需审批）是审批机制要处理的正常输入，不得被预检误伤"""
    import routes.agent as agent_mod
    monkeypatch.setattr("agent.harness.reins._get_global_permission_gate_sync", lambda: gate)
    monkeypatch.setattr(agent_mod, "append_record_async", _noop_async, raising=True)
    out = await agent_mod.allow_tool_permission(agent_mod.AllowPermissionRequest(
        tool="shell_exec", command="systemctl restart nginx"))
    assert out["granted"] is True


def test_grant_scope_is_literal_enum():
    """scope 由裸 str 改 Literal 枚举（Q-B4①）：非法值在请求模型层就被拒"""
    import pydantic
    from routes.agent import PermissionGrantRequest
    with pytest.raises(pydantic.ValidationError):
        PermissionGrantRequest(tool="shell_exec", scope="forever")
    assert PermissionGrantRequest(tool="shell_exec").scope == "once"
