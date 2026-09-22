# -*- coding: utf-8 -*-
"""计划模式（Plan Mode）回归测试。

覆盖四件事：
1. 只读强制的判定表 —— 正例与**反例**都要有，反例是这一层的存在理由；
2. 拦截器行为 —— 拒绝时的文案自带纠偏指令、事件类型正确；
3. **未启用计划模式时逐字段零差异**（最重要的回归：不能影响既有 run）；
4. 白名单的包含关系 —— `PLAN_READ_ONLY_VERBS` 必须是 `safety._SAFE_VERBS` 的严格子集。

第 4 条是为了钉住设计里那句结论：「plan 模式的承诺（什么都没改）比审批门
（用户同意了）更强，所以它的白名单必须更严」。两张表一旦各自漂移，这条会先红。
"""
import asyncio

import pytest

from agent.harness.contracts import ReinDecision, ReinPoint, RunContext
from agent.harness.reins import plan_mode as pm
from agent.harness.reins.core import Reins
from agent.harness.safety import _SAFE_VERBS


def _ctx(mode: str | None = "plan", tool: str = "", args: dict | None = None) -> RunContext:
    """造一个最小 RunContext；mode=None 表示完全不设置 permission_mode 键。"""
    ctx = RunContext.new(task="测试任务", mode="ssh")
    if mode is not None:
        ctx.metadata[pm.MODE_KEY] = mode
    if tool:
        ctx.tool_calls_log.append(
            {"tool": tool, "args": args or {}, "iteration": 0, "success": None}
        )
    return ctx


def _decide(tool: str, args: dict | None = None, mode: str | None = "plan") -> ReinDecision:
    return asyncio.run(pm.PlanModeGuard().check(_ctx(mode, tool, args)))


# ── 1. 工具级判定 ───────────────────────────────────────────────────────────

ALLOWED_TOOLS = [
    ("file_read", {"path": "/etc/nginx/nginx.conf"}),
    ("file_glob", {"pattern": "*.log"}),
    ("file_grep", {"pattern": "error"}),
    ("log_read", {"path": "/var/log/syslog"}),
    ("sys_overview", {}),
    ("process_inspect", {}),
    ("network_probe", {}),
    ("job_list", {}),
    ("job_output", {"job_id": "j1"}),
    ("web_search", {"query": "nginx 超时排查"}),
    ("load_skill", {"name": "x"}),
    ("memory_search", {"query": "上次怎么重启的"}),
    # todo_write 是唯一放行的「写」工具：写的是计划自身的表示，不是目标机状态
    ("todo_write", {"todos": []}),
]


@pytest.mark.parametrize("tool,args", ALLOWED_TOOLS, ids=[t for t, _ in ALLOWED_TOOLS])
def test_只读工具在计划模式下放行(tool, args):
    decision = _decide(tool, args)
    assert decision.intercepted is False, f"{tool} 不该被拦：{decision.intercept_reason}"


DENIED_TOOLS = [
    # 会改目标机状态
    ("file_write", {"path": "/etc/nginx/nginx.conf", "content": "x"}),
    ("file_edit", {"path": "/etc/hosts", "old": "a", "new": "b"}),
    ("shell_exec", {"command": "systemctl restart nginx"}),
    ("job_run", {"cmd": "rm -rf /tmp/x"}),
    ("job_kill", {"job_id": "j1"}),
    ("schedule_op", {"action": "create"}),
    # 按 action 分流，整只拒掉（status 只读、stop/restart 是写）
    ("service_manage", {"action": "status", "name": "nginx"}),
    # 写长期记忆（durable state），与 todo_write 的「计划表示」不同
    ("memory_add", {"content": "x"}),
    ("memory_invalidate", {"memory_id": "m1"}),
    ("block_update", {"content": "x"}),
    # risk_level=safe 但能间接执行任意命令 —— 只读约束的逃逸口
    ("spawn_subagent", {"task": "rm -rf /"}),
    # 未注册/未知工具也必须被拒（fail-closed）
    ("no_such_tool", {}),
]


@pytest.mark.parametrize("tool,args", DENIED_TOOLS, ids=[t for t, _ in DENIED_TOOLS])
def test_写工具与逃逸口在计划模式下被拒(tool, args):
    assert _decide(tool, args).intercepted is True


def test_空工具名在判定层就被拒():
    """空工具名要直接测 `classify_plan_action`，不能走拦截器。

    拦截器在 `ctx.tool_calls_log` 为空时提前返回（"这一轮没有工具要判"），
    所以用「空工具名 + 空日志」去断言拦截，测到的是提前返回分支 —— 断言为假
    是测试写错，不是实现漏判。（第一版就是这么写错的。）
    """
    allowed, reason = pm.classify_plan_action("", {})
    assert allowed is False
    assert reason


def test_spawn_subagent_被拒的理由说明它是逃逸口():
    """这条专门钉住设计里的反例：risk_level=safe ≠ 只读。"""
    d = _decide("spawn_subagent", {"task": "x"})
    assert "计划模式下不可用" in d.intercept_reason
    assert "可能改变目标机状态" in d.intercept_reason


# ── 2. 命令级判定 ───────────────────────────────────────────────────────────

ALLOWED_COMMANDS = [
    "df -h",
    "df -h /var",
    "ls -la /var/log",
    "cat /etc/nginx/nginx.conf",
    "head -n 50 /var/log/nginx/error.log",
    "tail -f /var/log/syslog",
    "grep -rn 'timeout' /etc/nginx",
    "cat /etc/nginx/nginx.conf | grep -n worker",
    "systemctl status nginx",
    "systemctl is-active nginx",
    "journalctl -u nginx -n 50",
    "ip addr",
    "ss -lntp",
    "ps aux",
    "du -sh /var/log",
    "find /var/log -name '*.log'",
    "find /etc -type f -name '*.conf' | head -20",
    "git status",
    "docker ps",
    "kubectl get pods -A",
    "echo hello",
    "echo 'a>b'",            # 引号里的 > 不是重定向
    "echo x > /dev/null",    # /dev/null 不算落盘
    "ps aux 2>&1",           # fd 复制不算落盘
]


@pytest.mark.parametrize("cmd", ALLOWED_COMMANDS)
def test_只读命令放行(cmd):
    allowed, reason = pm.classify_plan_command(cmd)
    assert allowed is True, f"`{cmd}` 应放行，却被拒：{reason}"


DENIED_COMMANDS = [
    # 写文件 / 重定向
    "tee /tmp/x",
    "cat /etc/hosts > /tmp/h",
    "echo hi >> /tmp/x",
    "cp /etc/hosts /tmp/h",
    "mv /etc/hosts /tmp/h",
    "mkdir /tmp/x",
    "sed -i 's/a/b/' /etc/hosts",
    # 改服务 / 进程
    "systemctl restart nginx",
    "systemctl stop nginx",
    "service nginx restart",
    "kill -9 1234",
    # 删 / 权限
    "rm -rf /var/log",
    "chmod 777 /etc/shadow",
    "chown root /etc",
    "find / -delete",
    "find /tmp -exec rm {} ;",
    "sort -o /tmp/x /etc/hosts",
    # 装包
    "apt update",
    "apt-get install nginx",
    "dpkg -i x.deb",
    # 网络下载落盘 / 外发
    "curl -o /tmp/x http://a/b",
    "wget -O /tmp/x http://a/b",
    # 提权 / 洗白 / 间接执行
    "sudo cat /etc/shadow",
    "su - root",
    "xargs rm",
    "env FOO=1 rm -rf /tmp/x",
    "sh -c 'rm -rf /'",
    "bash -c 'df -h'",
    "python3 -c 'print(1)'",
    "ssh root@10.0.0.1",
    # 命令替换
    "echo $(whoami)",
    "echo `id`",
    # 带写副作用的 flag / 带参即改
    "journalctl --vacuum-files=0",
    "journalctl --rotate",
    "dmesg -C",
    "date -s '2020-01-01'",
    "hostname web-99",
    "ifconfig eth0 down",
    # safety 既有规则（敏感读取）—— 本层不重写规则，但必须仍然拦住
    "cat /etc/shadow",
    # 不在白名单的只读之外动词
    "awk '{print $1}' /etc/hosts",
    "top -b -n1",
    "",
]


@pytest.mark.parametrize("cmd", DENIED_COMMANDS)
def test_非只读命令被拒(cmd):
    allowed, reason = pm.classify_plan_command(cmd)
    assert allowed is False, f"`{cmd}` 应被拒，却放行了"
    assert reason, "拒绝必须带原因（原因会作为纠偏文案回灌给模型）"


def test_重定向识别_引号与fd复制与devnull都不算写():
    assert pm._find_write_redirect("echo 'a>b'") == ""
    assert pm._find_write_redirect("ps aux 2>&1") == ""
    assert pm._find_write_redirect("echo x > /dev/null") == ""
    assert pm._find_write_redirect("echo x > /tmp/f") != ""
    assert pm._find_write_redirect("echo x >>/tmp/f") != ""


def test_拒绝原因能区分是工具不行还是命令不行():
    _, tool_reason = pm.classify_plan_action("service_manage", {"action": "status"})
    assert "改用只读工具" in tool_reason or "shell_exec 跑只读命令" in tool_reason

    _, cmd_reason = pm.classify_plan_command("systemctl restart nginx")
    assert "systemctl" in cmd_reason or "只读排查" in cmd_reason


# ── 3. 拦截器行为 ───────────────────────────────────────────────────────────

def test_拒绝文案自带纠偏指令():
    """纠偏必须写在 intercept_reason 里。

    driver.py 在 PRE_TOOL_CALL 的拦截分支只做
    `result_text = f"工具调用被拦截: {intercept_reason}"` 回灌给模型，
    **不消费 inject_messages** —— 所以「该怎么做」如果只放在 inject_messages 里，
    模型永远收不到。
    """
    d = _decide("file_write", {"path": "/etc/x", "content": "y"})
    assert d.intercepted is True
    assert "计划模式" in d.intercept_reason
    assert "plan_write" in d.intercept_reason, "要告诉模型调研完后用 plan_write 产出计划"
    assert "不要重试" in d.intercept_reason


def test_拒绝时发出_plan_mode_block_事件():
    d = _decide("file_write", {"path": "/etc/x", "content": "y"})
    assert len(d.events) == 1
    ev = d.events[0]
    assert ev.kind == "plan_mode_block"
    assert ev.payload.get("tool") == "file_write"
    assert ev.run_id  # 事件必须带 run_id，否则前端无法归并到本次 run


def test_提前短路_后续守卫不再被调用():
    """PlanModeGuard 必须早于 FilesystemGuard/PermissionGate。

    否则流程会变成「先弹审批卡、用户点了才被拒」，而 Claude Code 的口径是
    plan 模式下 file edits **never auto-approved, even when an allow rule matches**。
    这里用「后续守卫有没有被调用」来断言顺序，不依赖别的类里的 priority 常量。
    """

    class _RecordingGuard:
        point = ReinPoint.PRE_TOOL_CALL
        priority = 8  # 与 PermissionGate（会弹审批卡的那个）同档，取 8 而非 10 更严苛

        def __init__(self) -> None:
            self.called = False

        async def check(self, ctx):
            self.called = True
            return ReinDecision()

    # 计划模式：被拒，且后续守卫一次都没跑
    reins = Reins()
    reins.register(pm.PlanModeGuard())
    stub = _RecordingGuard()
    reins.register(stub)
    d = asyncio.run(reins.check(
        ReinPoint.PRE_TOOL_CALL, _ctx("plan", "file_write", {"path": "/etc/x", "content": "y"})
    ))
    assert d.intercepted is True
    assert stub.called is False

    # 非计划模式：不被拦，后续守卫正常跑（证明短路只发生在该发生时）
    reins2 = Reins()
    reins2.register(pm.PlanModeGuard())
    stub2 = _RecordingGuard()
    reins2.register(stub2)
    d2 = asyncio.run(reins2.check(
        ReinPoint.PRE_TOOL_CALL, _ctx("normal", "file_write", {"path": "/etc/x", "content": "y"})
    ))
    assert d2.intercepted is False
    assert stub2.called is True


def test_判定异常按拒绝处理_不变成放行(monkeypatch):
    """判定函数抛异常时必须 fail-closed。

    放行方向出错等于「只读约束静默失效」，比误拒危险得多。
    """
    def _boom(*_a, **_k):
        raise RuntimeError("dalvik")

    monkeypatch.setattr(pm, "classify_plan_action", _boom)
    d = _decide("file_read", {"path": "/etc/hosts"})
    assert d.intercepted is True
    assert "按只读约束拒绝" in d.intercept_reason


# ── 4. 纪律注入 ─────────────────────────────────────────────────────────────

def test_纪律每轮注入一次():
    d = asyncio.run(pm.PlanDisciplineRein().check(_ctx("plan")))
    assert d.intercepted is False
    assert len(d.inject_messages) == 1
    content = d.inject_messages[0]["content"]
    assert content.startswith("[计划模式]")
    assert "只读" in content and "plan_write" in content


# ── 5. 未启用计划模式：逐字段零差异（最重要的回归）──────────────────────────

@pytest.mark.parametrize("mode", ["normal", None, "execute", ""])
@pytest.mark.parametrize("tool,args", [
    ("file_read", {"path": "/etc/hosts"}),                                  # 通常放行
    ("file_write", {"path": "/etc/hosts", "content": "x"}),                  # 通常需审批
    ("shell_exec", {"command": "systemctl restart nginx"}),                  # 通常需审批
    ("spawn_subagent", {"task": "x"}),                                      # 通常放行
])
def test_非计划模式下逐字段零差异(mode, tool, args):
    """没开计划模式时，两个拦截器都必须返回全默认 ReinDecision。

    这条守住「引入本模块不改变既有行为」—— 任何字段被填上都会红。
    """
    ctx = _ctx(mode, tool, args)
    guard = asyncio.run(pm.PlanModeGuard().check(ctx))
    assert guard == ReinDecision(), f"PlanModeGuard 在 mode={mode!r} 时污染了决策：{guard}"
    assert guard.intercepted is False
    assert guard.events == []
    assert guard.inject_messages == []
    assert guard.undo_actions == []
    assert guard.terminate is False

    discipline = asyncio.run(pm.PlanDisciplineRein().check(ctx))
    assert discipline == ReinDecision(), f"PlanDisciplineRein 在 mode={mode!r} 时污染了决策"


def test_无工具调用时不产生决策():
    assert asyncio.run(pm.PlanModeGuard().check(_ctx("plan"))) == ReinDecision()


def test_plan_mode_active_只认_plan():
    assert pm.plan_mode_active(_ctx("plan")) is True
    assert pm.plan_mode_active(_ctx("PLAN")) is True  # 大小写不敏感
    for other in ("normal", "execute", "", None):
        assert pm.plan_mode_active(_ctx(other)) is False


# ── 6. 白名单包含关系：plan 必须比审批门更严 ─────────────────────────────────

def test_只读动词必须是_safety_白名单的严格真子集():
    """「plan 的承诺比审批门更强」→ 它的动词表必须更小，且不能为空。

    两张表一旦各自漂移（比如有人在 safety 里加了新动词并默认 plan 也能用），
    这条会先红 —— 而不是等到某条命令悄悄改坏了生产机。
    """
    assert pm.PLAN_READ_ONLY_VERBS, "白名单不能为空"
    extra = pm.PLAN_READ_ONLY_VERBS - _SAFE_VERBS
    assert not extra, f"plan 白名单里有 safety 都不认的动词：{sorted(extra)}"
    assert pm.PLAN_READ_ONLY_VERBS < _SAFE_VERBS, "必须是严格真子集（相等说明没更严）"


def test_有写侧路的动词被显式排除在只读白名单外():
    """逐项列出反例，改动前先想清楚这一列 —— 它们都在 safety 的白名单里，但不能进 plan。"""
    for verb in ("tee", "curl", "wget", "awk", "sed", "find", "journalctl",
                 "dmesg", "date", "ifconfig", "hostname", "env", "sudo",
                 "systemctl", "service", "docker", "kubectl", "apt"):
        assert verb not in pm.PLAN_READ_ONLY_VERBS, f"{verb} 不该进只读白名单"


# ── 7. 生产装配（create_default_reins）──────────────────────────────────────
# 1~6 组用手搓 reins 证明「判定与拦截器自身正确」；这一组证明它们**真的被装进了
# 生产装配**、顺序对、且端到端拦得住。少了这一组，单元全绿而线上毫无变化 ——
# 「实现了但没接线」正是本仓库反复出现的失败模式（前端 plan chip 亮着而后端
# 只读强制从不激活，就是同一类缺陷，见 docs/plan-mode.md §6.1）。


def _default_reins():
    from agent.harness.reins.defaults import create_default_reins
    return create_default_reins()


def test_默认装配已注册两个计划模式拦截器且顺序正确():
    reins = _default_reins()
    pre_tool = [type(i).__name__ for i in reins._interceptors[ReinPoint.PRE_TOOL_CALL]]
    assert "PlanModeGuard" in pre_tool, f"PlanModeGuard 未进默认装配：{pre_tool}"
    pre_iter = [type(i).__name__ for i in reins._interceptors[ReinPoint.PRE_ITERATION]]
    assert pre_iter.count("PlanDisciplineRein") == 1, f"纪律注入未注册或重复：{pre_iter}"

    # `Reins.register` 按 priority 升序插入，且 `_interceptors` 的名单就是执行顺序。
    # PlanModeGuard 必须早于**每一个会弹审批卡的拦截器** —— 否则 plan 下的写操作会
    # 先弹审批卡，用户点「始终允许」就绕过了只读承诺（Claude Code 口径：
    # file edits never auto-approved in plan mode, even when an allow rule matches）。
    prios = [i.priority for i in reins._interceptors[ReinPoint.PRE_TOOL_CALL]]
    assert prios == sorted(prios), f"未按 priority 升序：{prios}"
    for later in ("ClarificationInterceptor", "PermissionGate", "FilesystemGuard"):
        assert later in pre_tool, f"预期 {later} 在 PRE_TOOL_CALL 上，实际 {pre_tool}"
        assert pre_tool.index("PlanModeGuard") < pre_tool.index(later), (
            f"PlanModeGuard 未早于 {later} —— 用户点『始终允许』会绕过只读承诺：{pre_tool}")

    # 也不许与 LoopDetector 撞 priority：同值时顺序由注册顺序隐式决定
    # （`list.sort` 稳定），那会让「先防死循环、再判只读」变成注册顺序的副产品
    prio_by_name = {type(i).__name__: i.priority
                    for i in reins._interceptors[ReinPoint.PRE_TOOL_CALL]}
    assert prio_by_name["PlanModeGuard"] != prio_by_name.get("LoopDetector"), (
        f"PlanModeGuard 与 LoopDetector 同为 {prio_by_name.get('LoopDetector')}，"
        "顺序会退化成依赖 append 顺序")


def test_默认装配下_plan_模式端到端拦住写工具():
    """saddle=None 时 PermissionGate 对一切工具 fail-closed，所以断言必须落在
    「是不是 plan_mode_block」，而不是「整条链有没有拦」—— 否则这条测试在
    plan_mode 完全没注册时也会因为 PermissionGate 拦下而假通过。
    """
    reins = _default_reins()

    d = asyncio.run(reins.check(
        ReinPoint.PRE_TOOL_CALL, _ctx("plan", "file_write", {"path": "/etc/passwd"})))
    assert d.intercepted is True
    assert [e.kind for e in d.events] == ["plan_mode_block"], (
        f"plan 下写工具不是被计划模式拦的：{[e.kind for e in d.events]}")

    # 同一工具在 normal 下**不能**出现 plan_mode_block（其余拦截器仍可拦，故查事件）
    d2 = asyncio.run(reins.check(
        ReinPoint.PRE_TOOL_CALL, _ctx("normal", "file_write", {"path": "/etc/passwd"})))
    assert "plan_mode_block" not in [e.kind for e in d2.events]

    # 未设该键的老调用方同样不受影响
    d3 = asyncio.run(reins.check(
        ReinPoint.PRE_TOOL_CALL, _ctx(None, "file_write", {"path": "/etc/passwd"})))
    assert "plan_mode_block" not in [e.kind for e in d3.events]


def test_默认装配下_plan_模式每轮注入纪律块():
    reins = _default_reins()
    d = asyncio.run(reins.check(ReinPoint.PRE_ITERATION, _ctx("plan")))
    texts = [m.get("content", "") for m in d.inject_messages]
    assert any("只读" in t for t in texts), f"plan 下未注入纪律块：{texts}"

    d2 = asyncio.run(reins.check(ReinPoint.PRE_ITERATION, _ctx("normal")))
    assert d2.inject_messages == [], "normal 下不该注入计划纪律"


def test_harness_run_签名接受_permission_mode_且默认_normal():
    """入口通路的守卫：`harness.run` 是 permission_mode 写进 ctx.metadata 的唯一
    地方。参数一旦被删/改名，routes 层传的值会静默丢弃（计划模式永远不生效），
    而所有单元测试仍然是绿的 —— 因为没人从这条路径进来测。
    """
    import inspect

    from agent.harness.harness import Harness

    sig = inspect.signature(Harness.run)
    assert "permission_mode" in sig.parameters, "harness.run 丢了 permission_mode 参数"
    assert sig.parameters["permission_mode"].default == "normal", (
        "默认值必须是 normal，否则既有调用方会被静默切进计划模式")


# ── 8. HTTP 入口到 run 的通路 ───────────────────────────────────────────────
# `AgentRunRequest.permission_mode`（字段）与 `harness.run(permission_mode=...)`
# （传参）是**两个独立写点**。只加字段不传参 = 界面选了计划模式但实际是普通模式，
# 而且两边单独看都「对」，测试全绿。用 AST 把这一对钉在一起。


def test_请求字段与_run_传参成对存在():
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "routes/agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 1) 请求模型上必须有该字段，且默认值必须是 "normal"
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AgentRunRequest":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.value is not None:
                        try:
                            defaults[stmt.target.id] = ast.literal_eval(stmt.value)
                        except ValueError:
                            defaults[stmt.target.id] = ast.unparse(stmt.value)
    assert "permission_mode" in defaults, "AgentRunRequest 丢了 permission_mode 字段"
    assert defaults["permission_mode"] == "normal", (
        f"字段默认值必须是 'normal'，实际 {defaults['permission_mode']!r}")

    # 2) harness.run(...) 必须把 req 上的值传下去（限定 harness.run，不用裸 .run）
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "harness"
    ]
    assert calls, "routes/agent.py 里找不到 harness.run(...) 调用"
    passed = [
        ast.unparse(kw.value) for c in calls for kw in c.keywords
        if kw.arg == "permission_mode"
    ]
    assert passed == ["req.permission_mode"], (
        f"harness.run 没有（正确）传 permission_mode，实际 {passed} —— 计划模式不会生效")


def test_请求模型的值域校验真的拦住非法值():
    """Literal 是这一层的安全属性：非法值必须 422，不能静默退化成普通模式。

    运行时真造一次模型（AST 只能证明写了注解，不能证明注解真的生效）。
    """
    from pydantic import ValidationError

    from routes.agent import AgentRunRequest

    assert AgentRunRequest(task="t", conversation_id="c").permission_mode == "normal"
    assert AgentRunRequest(
        task="t", conversation_id="c", permission_mode="plan").permission_mode == "plan"
    for bad in ("plna", "PLAN", "execute", ""):
        with pytest.raises(ValidationError):
            AgentRunRequest(task="t", conversation_id="c", permission_mode=bad)


def test_HTTP_层把_permission_mode_交给_run_运行时通路():
    """AST 证明「源码里有这一对」，这条证明「运行时真的接得上」：
    造一个真 AgentRunRequest，断言 harness.run 的**签名**能接受该字段名。
    两条一起才能覆盖「改名只改一半」这类断链。
    """
    import inspect

    from agent.harness.harness import Harness
    from routes.agent import AgentRunRequest

    req = AgentRunRequest(task="t", conversation_id="c", permission_mode="plan")
    sig = inspect.signature(Harness.run)
    assert req.permission_mode in ("normal", "plan")
    # 关键字名必须能对上（否则 TypeError: unexpected keyword argument）
    sig.bind_partial(task=req.task, permission_mode=req.permission_mode)


