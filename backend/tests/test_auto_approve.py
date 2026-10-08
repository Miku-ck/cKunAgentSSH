# -*- coding: utf-8 -*-
"""自动审批（permission_mode="auto"）—— LLM 审批裁决器回归测试。

钉住四件事：
1. **fail-closed**：裁决 DENY / 超时 / 异常 / 未配置模型 / 总闸关闭，全部回退
   弹卡 —— "自动审批"绝不能因模型不可用退化成"无人审批"；
2. **未开 auto 时逐字段零差异**（与 test_plan_mode 同款回归口径）：normal/plan
   路径下裁决器一次都不许被调用；
3. **裁决器不越过的边界**：hardline 绝对阻断、prod 预授权强化、用户 run 内
   显式拒绝 —— 这三条路径既不弹卡也不问模型；
4. 放行审计记 conclusion="auto_allowed"（与用户点出来的 approved 区分），
   提示事件走 emit_event 通道（PRE_TOOL_CALL 非拦截分支不消费 decision.events）。

运行: python -m pytest backend/tests/test_auto_approve.py -v
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.harness.reins.auto_approve as _aa
import agent.harness.reins.gate as _gate_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.tools import create_registry

NGINX = "/etc/nginx/nginx.conf"        # dangerous：敏感路径写
PLANT = "/root/.ssh/authorized_keys"   # hardline：绝对阻断


# ── 夹具与构造器 ─────────────────────────────────────────────────────────────

@pytest.fixture
def audits(monkeypatch):
    """捕获三个模块的审计落库调用（gate / guards / auto_approve）"""
    records = []

    async def _rec(**kw):
        records.append(kw)
        return None

    monkeypatch.setattr(PermissionGate, "_load_permanent_allowlist", lambda self: set())
    monkeypatch.setattr(PermissionGate, "_save_permanent_allowlist", lambda self: None)
    for _m in (_guards_mod, _gate_mod, _aa):
        monkeypatch.setattr(_m, "append_record_async", _rec)
    return records


def _saddle():
    return SimpleNamespace(registry=create_registry())


def _ctx(mode: str | None = "auto", environment: str = "test") -> RunContext:
    ctx = RunContext.new(task="重启一下 nginx", config=AgentConfig(),
                         environment=environment)
    if mode is not None:
        ctx.metadata[_aa.MODE_KEY] = mode
    ctx.llm_base_url = "http://llm.test/v1"
    ctx.llm_api_key = "k"
    ctx.llm_model = "m"
    return ctx


def _logged(ctx: RunContext, tool: str, args: dict) -> RunContext:
    ctx.tool_calls_log.append(
        {"tool": tool, "args": args, "iteration": 0, "success": None}
    )
    return ctx


def _stub_judge(monkeypatch, allow: bool):
    """替换真实 LLM 调用，记录被问到的次数"""
    calls: list[str] = []

    async def _fake(ctx, subject, reason):
        calls.append(subject)
        return allow

    monkeypatch.setattr(_aa, "_judge_once", _fake)
    return calls


def _forbidden_judge(monkeypatch):
    """真实 LLM 裁决调用被替换为必炸桩 —— 断言「该路径根本不该问模型」。

    只替换 _judge_once 而不替换 try_auto_approve：后者是 import 时按名字绑定
    进 gate/guards 模块的（from .auto_approve import try_auto_approve），
    monkeypatch _aa 上的名字对它们无效，会造成"测了个寂寞"的假断言。
    try_auto_approve 在调用点按模块全局名查 _judge_once，桩必然生效；
    非 auto 档由其入口短路保证连 _judge_once 都到不了。
    """
    def _boom(*a, **kw):
        raise AssertionError("非 auto 档/边界路径不得调用 LLM 裁决器")

    monkeypatch.setattr(_aa, "_judge_once", _boom)


def _emits(ctx: RunContext) -> list:
    got: list = []

    async def _emit(ev):
        got.append(ev)

    ctx.metadata["emit_event"] = _emit
    return got


# ── 1. 档位判定与输出解析 ────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,expected", [
    ("auto", True), ("AUTO", True), (" auto", False),
    ("normal", False), ("plan", False), (None, False),
])
def test_is_auto_mode_只在_auto_档为真(mode, expected):
    ctx = _ctx(mode=mode)
    if mode == " auto":  # 带前导空格的脏值：harness 层已归一化，这里测防御口径
        ctx.metadata[_aa.MODE_KEY] = mode
    assert _aa.is_auto_mode(ctx) is expected


@pytest.mark.parametrize("raw,expected", [
    ("ALLOW", True),
    ("allow", True),
    ("ALLOW: 该命令可逆", True),
    (" DENY", False),
    ("DENY\nALLOW", False),        # 只认第一行
    ("I think it's allowed", False),  # 不含裸 ALLOW 打头
    ("", False), (None, False),
])
def test_裁决输出解析只认第一行_ALLOW(raw, expected):
    assert _aa._parse_verdict(raw) is expected


# ── 2. fail-closed：真实 _judge_once 的所有失败面 ────────────────────────────

def test_未配置端点_directly_判负(audits):
    ctx = _ctx()
    ctx.llm_base_url = ""
    assert asyncio.run(_aa._judge_once(ctx, "shell_exec: x", "r")) is False


def test_空_api_key_不算未配置模型(monkeypatch, audits):
    """S10-07：空 api_key 是本仓承认的合法态 —— 面板 provider 初值是「自定义/vLLM」
    （isLocal，连密钥输入框都不渲染），库里存的就是空串，`llm_adapter._get_client`
    也按 `api_key or "EMPTY"` 处理。原守卫用 `not ctx.llm_api_key` 把它判成
    「没配模型」⇒ 本地 vLLM 用户把自动审批开关拨上去也永不生效。
    这条同时钉住「问到的仍然是那个空串」，别让谁顺手改成 "EMPTY" 又写回模型配置。
    """
    import post_processor

    asked: list = []

    async def _fake(**kw):
        asked.append(kw["model_cfg"]["api_key"])
        return "ALLOW"

    monkeypatch.setattr(post_processor, "_call_llm_with_timeout", _fake)
    ctx = _ctx()
    ctx.llm_api_key = ""
    assert asyncio.run(_aa._judge_once(ctx, "shell_exec: x", "r")) is True
    assert asked == [""]


def test_总闸关闭_不问模型(monkeypatch, audits):
    import post_processor

    def _spy(*a, **kw):
        raise AssertionError("CKUN_AGENT_DISABLE_AUTO_APPROVE=1 时不得发起 LLM 调用")

    monkeypatch.setattr(post_processor, "_call_llm_with_timeout", _spy)
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", False)
    assert asyncio.run(_aa._judge_once(_ctx(), "shell_exec: x", "r")) is False


def test_环境变量kill_switch压过设置面板开关(monkeypatch):
    """env 总闸优先于 DB/UI 开关（G4/G5 代理同款姿态：运维一键回退不需要发版，
    也不允许设置页把它翻回来）。"""
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", True)
    monkeypatch.setenv("CKUN_AGENT_DISABLE_AUTO_APPROVE", "1")
    assert _aa.auto_approve_effective() is False
    monkeypatch.delenv("CKUN_AGENT_DISABLE_AUTO_APPROVE")
    assert _aa.auto_approve_effective() is True
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", False)
    assert _aa.auto_approve_effective() is False


def test_env_kill_switch_在裁决调用处生效(monkeypatch):
    """_judge_once 必须在发起 LLM 调用前查运行时口径，只看导入期常量 = 摆设。

    桩用"记录 + 返回 ALLOW"而不是抛异常：_judge_once 把一切异常都 fail-closed
    吞成 False，抛异常的桩会"因错误的理由通过"（见 try/except 分支）。
    """
    import post_processor

    hits: list = []

    async def _spy(**kw):
        hits.append(kw)
        return "ALLOW"

    monkeypatch.setattr(post_processor, "_call_llm_with_timeout", _spy)
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", True)
    monkeypatch.setenv("CKUN_AGENT_DISABLE_AUTO_APPROVE", "1")
    assert asyncio.run(_aa._judge_once(_ctx(), "shell_exec: x", "r")) is False
    assert hits == [], "kill switch 生效时不得发起 LLM 调用"


@pytest.mark.parametrize("outcome", [
    None,                              # 超时/异常时 _call_llm 返回 None
    "",                                # 空返回
    "DENY",
    "MAYBE",                           # 不合约定输出
    "I think it's ALLOWed",            # ALLOW 不在第一行打头
])
def test_LLM_输出与失败一律回退弹卡(monkeypatch, outcome):
    """fail-closed 底线：拿不准 = 不放行，回退人工审批卡。

    输出解析的细粒度口径（大小写/多行/前缀）由 test_裁决输出解析 单独钉，
    这里只测 _judge_once 的失败面全部收敛到 False。
    """
    import post_processor

    async def _fake(**kw):
        return outcome if isinstance(outcome, str) else None

    monkeypatch.setattr(post_processor, "_call_llm_with_timeout", _fake)
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", True)
    got = asyncio.run(_aa._judge_once(_ctx(), "shell_exec: rm -rf /var", "dangerous"))
    assert got is False


def test_LLM_抛异常_判负不抛出(monkeypatch):
    import post_processor

    async def _boom(**kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(post_processor, "_call_llm_with_timeout", _boom)
    monkeypatch.setattr(_aa, "AUTO_APPROVE_ENABLED", True)
    assert asyncio.run(_aa._judge_once(_ctx(), "shell_exec: x", "r")) is False


# ── 3. try_auto_approve 单元语义 ─────────────────────────────────────────────

def test_非_auto_档_裁决器一次都不被调用(monkeypatch, audits):
    """入口短路：normal/plan/未设置三档下 try_auto_approve 必须原样返回 None。

    _judge_once 已换成必炸桩 —— 桩若在循环里被碰到，异常会从被调函数
    一路抛出来直接红。不用 monkeypatch.undo() 重铺桩：那会连 audits
    夹具的打桩一起撤掉。
    """
    _forbidden_judge(monkeypatch)
    for mode in ("normal", "plan", None):
        ctx = _logged(_ctx(mode=mode), "file_write", {"path": NGINX, "content": "x"})
        assert asyncio.run(_aa.try_auto_approve(ctx, "file_write",
                                                {"path": NGINX}, "dangerous")) is None
    assert audits == []


def test_ALLOW_放行记审计并走_emit_event(monkeypatch, audits):
    _stub_judge(monkeypatch, True)
    ctx = _ctx()
    emits = _emits(ctx)
    d = asyncio.run(_aa.try_auto_approve(
        ctx, "file_write", {"path": NGINX, "content": "x"}, "dangerous: sensitive path",
        bound_command=f"echo x > {NGINX}"))
    assert d is not None and d.intercepted is False
    assert d.events == [], "PRE_TOOL_CALL 非拦截分支不消费 events，放行决策必须空事件"
    allow = [r for r in audits if r.get("conclusion") == "auto_allowed"]
    assert len(allow) == 1 and allow[0]["action"] == "file_write"
    assert allow[0]["result"].startswith("llm_judge_allow")
    assert len(emits) == 1 and emits[0].kind == "warning"
    assert "自动审批" in emits[0].payload["message"]


def test_DENY_不放行不记审计(monkeypatch, audits):
    _stub_judge(monkeypatch, False)
    d = asyncio.run(_aa.try_auto_approve(_ctx(), "shell_exec", {"command": "rm -rf /"}, "r"))
    assert d is None
    assert audits == []


def test_同一命令_run_内只裁一次(monkeypatch, audits):
    calls = _stub_judge(monkeypatch, True)
    ctx = _ctx()
    _emits(ctx)
    for _ in range(2):
        d = asyncio.run(_aa.try_auto_approve(
            ctx, "shell_exec", {"command": "systemctl restart nginx"}, "r"))
        assert d is not None
    assert len(calls) == 1, "per-run 缓存缺失会重复刷 LLM"
    assert len([r for r in audits if r.get("conclusion") == "auto_allowed"]) == 2


# ── 4. 真实链路：FilesystemGuard dangerous 分支 ──────────────────────────────

def _guard():
    gate = PermissionGate(saddle=_saddle())
    return gate, FilesystemGuard(permission_gate=gate, saddle=_saddle())


def _write_ctx(mode="auto", environment="test"):
    ctx = _ctx(mode=mode, environment=environment)
    _logged(ctx, "file_write", {"path": NGINX, "content": "x\n"})
    return ctx


def test_guard_auto_放行时不留审批等待签名(monkeypatch, audits):
    _stub_judge(monkeypatch, True)
    gate, guard = _guard()
    ctx = _write_ctx()
    _emits(ctx)
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is False
    from agent.harness.reins.core import _perm_pending
    assert _perm_pending(ctx) == {}, "放行路径不得在 pending 里留半张卡"
    assert any(r.get("conclusion") == "auto_allowed" for r in audits)


def test_guard_auto_裁负照旧弹卡(monkeypatch, audits):
    _stub_judge(monkeypatch, False)
    _gate_obj, guard = _guard()
    ctx = _write_ctx()
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is True
    assert any(e.kind == "permission_needed" for e in d.events)


def test_guard_normal_档行为与引入前一致(monkeypatch, audits):
    _forbidden_judge(monkeypatch)
    _gate_obj, guard = _guard()
    ctx = _write_ctx(mode="normal")
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is True
    assert any(e.kind == "permission_needed" for e in d.events)


def test_guard_hardline_不问模型(monkeypatch, audits):
    _forbidden_judge(monkeypatch)
    _gate_obj, guard = _guard()
    ctx = _write_ctx()
    ctx.tool_calls_log[-1]["args"] = {"path": PLANT, "content": "key\n"}
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is True
    assert not any(e.kind == "permission_needed" for e in d.events)


def test_guard_prod_强化优先_合规授权不问模型(monkeypatch, audits):
    _forbidden_judge(monkeypatch)
    _gate_obj, guard = _guard()
    ctx = _write_ctx(environment="prod")
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is True
    assert "生产环境" in d.intercept_reason
    assert any(r.get("conclusion") == "denied" for r in audits)


def test_guard_用户拒绝过的命令不被模型翻案(monkeypatch, audits):
    calls = _stub_judge(monkeypatch, True)  # 即便模型说 ALLOW
    gate, guard = _guard()
    ctx = _write_ctx()
    from agent.harness.reins.core import extract_bound_command
    bound, _ = extract_bound_command(create_registry(),
                                     "file_write", ctx.tool_calls_log[-1]["args"])
    gate.deny_command("file_write", str(bound), ctx.run_id)
    d = asyncio.run(guard.check(ctx))
    assert d.intercepted is True
    assert "曾被用户拒绝" in d.intercept_reason
    assert calls == [], "用户显式拒绝后不得再刷 LLM"


# ── 5. 真实链路：PermissionGate 工具级授权 ───────────────────────────────────
# service_manage = moderate + 不可逆（test 环境；prod 下 restart 升级为 dangerous）
# —— 目前注册表里没有声明 risk_level="dangerous" 的工具，工具级 dangerous
# 分支只能经 prod 升级到达，而那正是裁决器不许碰的合规路径（见下条反向钉）。

def test_gate_工具级不可逆_auto_放行(monkeypatch, audits):
    _stub_judge(monkeypatch, True)
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(), "service_manage", {"action": "restart", "name": "nginx"})
    _emits(ctx)
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is False, f"应被自动放行，实际：{d.intercept_reason}"
    assert any(r.get("conclusion") == "auto_allowed" for r in audits)


def test_gate_工具级不可逆_auto_裁负仍弹卡(monkeypatch, audits):
    _stub_judge(monkeypatch, False)
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(), "service_manage", {"action": "restart", "name": "nginx"})
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    assert any(e.kind == "permission_needed" for e in d.events)


def test_gate_工具级用户拒绝过的命令不被模型翻案(monkeypatch, audits):
    """S2-01 回归：自动审批不得推翻用户在 run 内的显式拒绝（工具级分支）。

    FilesystemGuard 的 shell 分支有 is_denied 前置门，PermissionGate 的工具级
    分支曾漏了 —— check_permission 返回 False 分不清「还没批过」与「拒绝过」，
    于是用户点过「拒绝」的 service_manage(stop/restart) / schedule_op / job_kill
    会被裁决器静默放行。用户否决永远高于模型翻案。
    """
    calls = _stub_judge(monkeypatch, True)  # 即便模型说 ALLOW
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(), "service_manage", {"action": "restart", "name": "nginx"})
    gate.deny_command("service_manage", None, ctx.run_id)
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    assert calls == [], "用户显式拒绝后不得再刷 LLM"
    assert not any(r.get("conclusion") == "auto_allowed" for r in audits)


def test_gate_normal_档零差异(monkeypatch, audits):
    _forbidden_judge(monkeypatch)
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(mode="normal"), "service_manage",
                  {"action": "restart", "name": "nginx"})
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    assert any(e.kind == "permission_needed" for e in d.events)


def test_gate_prod_升级后的_dangerous_不问模型(monkeypatch, audits):
    """prod + destructive_actions → risk=dangerous → 仅接受预授权 allowlist。

    这条是合规边界（已定：prod 授权必须出自用户预登记），auto 档不得让
    模型替用户批准 —— 走的是 gate 里 try_auto_approve 之前的 prod return。
    """
    _forbidden_judge(monkeypatch)
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(environment="prod"), "service_manage",
                  {"action": "restart", "name": "nginx"})
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    assert "生产环境" in d.intercept_reason
    assert any(r.get("conclusion") == "denied" for r in audits)


def test_gate_工具级裁决subject绑定bound命令(monkeypatch, audits):
    """裁决 subject 与授权键必须是同一对象。

    gate 弹卡前调 try_auto_approve 原本不传 bound_command（同上文
    check_permission 却传了）→ _subject_of 退到 args.content，模型被问的是
    「要写入的文件正文」，而放行后授权键绑的是「base64 写模板的路径哈希」——
    两处不同物正是本仓库「跨层 key 漂移」缺陷家族的形态。
    """
    calls = _stub_judge(monkeypatch, False)
    gate, _guard_obj = _guard()
    args = {"path": "/tmp/app.txt", "content": "TOP-SECRET-BODY"}
    ctx = _logged(_ctx(), "file_write", args)
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    from agent.harness.reins.core import extract_bound_command
    bound, _free = extract_bound_command(create_registry(),
                                         "file_write", args)
    assert bound and "/tmp/app.txt" in bound
    assert len(calls) == 1
    assert calls[0] == f"file_write: {bound}", "裁决 subject 偏离授权键绑定的那条命令"
    assert "TOP-SECRET-BODY" not in calls[0], "文件正文不得充当裁决标的"


def test_gate_兜底时长过期判定走getter_0为永久等待(monkeypatch, audits):
    """设置面板改的兜底时长必须即时被 gate/guards 读到。

    原实现是 `from .core import _PERMISSION_APPROVAL_FALLBACK_S` 的导入期快照
    —— PUT 后重绑 core 模块名，两个消费模块毫无感知（跨层 key 漂移家族）。
    0 = 永久等待的语义也只有在调用点现查才立得住。
    """
    import time as _t

    from agent.harness.reins.core import _perm_pending, tool_call_signature

    _stub_judge(monkeypatch, False)
    gate, _guard_obj = _guard()
    ctx = _logged(_ctx(mode="normal"), "service_manage",
                  {"action": "restart", "name": "nginx"})
    pending = _perm_pending(ctx)
    pending[tool_call_signature(ctx.tool_calls_log[-1])] = _t.monotonic() - 10_000

    monkeypatch.setattr(_gate_mod, "approval_fallback_seconds", lambda: 0.0)
    d = asyncio.run(gate.check(ctx))
    assert d.intercepted is True
    assert not any(e.kind == "permission_expired" for e in d.events), \
        "0 = 永久等待，不得判过期"

    monkeypatch.setattr(_gate_mod, "approval_fallback_seconds", lambda: 1.0)
    d2 = asyncio.run(gate.check(ctx))
    assert any(e.kind == "permission_expired" for e in d2.events), \
        "改了时长必须当场生效，而不是等重启"


# ── 6. 契约：HTTP 值域与 harness 归一化 ──────────────────────────────────────

def test_请求模型接受_auto_且仍拦非法值():
    from pydantic import ValidationError

    from routes.agent import AgentRunRequest

    assert AgentRunRequest(
        task="t", conversation_id="c", permission_mode="auto").permission_mode == "auto"
    for bad in ("aut0", "AUTO", "manual", ""):
        with pytest.raises(ValidationError):
            AgentRunRequest(task="t", conversation_id="c", permission_mode=bad)


def test_harness_归一化值域包含_auto():
    """harness.run 的白名单元组必须认识 auto —— 漏改会把合法档位打成
    未知值静默降回 normal（警告一次，然后审批卡照旧弹，用户以为开了自动）。"""
    src = (Path(__file__).resolve().parents[1]
           / "agent" / "harness" / "harness.py").read_text(encoding="utf-8")
    assert '"normal", "auto", "plan"' in src


def test_plan_模式不受_auto_档影响():
    """PlanModeGuard 只在值恰为 plan 时激活 —— auto 是审批轴，不是只读轴。"""
    from agent.harness.reins import plan_mode as pm

    d = asyncio.run(pm.PlanModeGuard().check(
        _logged(_ctx(mode="auto"), "file_write", {"path": "/tmp/a", "content": "x"})))
    assert d.intercepted is False
