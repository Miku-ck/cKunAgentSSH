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
