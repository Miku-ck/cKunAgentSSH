# 生产环境审批策略调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生产环境危险操作在交互会话中恢复「用户现场确认」，无人值守仍要求预登记 allowlist；澄清向导只在「有人答 + 答完能执行 + 真的需要授权」时出现。

**Architecture:** 交互性统一用 `ctx.metadata[AWAIT_APPROVAL_KEY]`（与 Driver 审批等待闸同源）。`FilesystemGuard`（priority 10，shell 命令）与 `PermissionGate`（priority 8，工具级风险）的 prod 分支都加「仅无人值守才要求预登记」条件，交互 prod 落回既有发卡/等待/授权消费路径。`ClarificationInterceptor`（priority 7）新增四道跳过闸（非交互 / prod / hardline / 有效风险 safe），并移除不再需要的 allowlist 查询。

**Tech Stack:** Python 3.11+ / FastAPI backend，pytest（backend），Vue 3 + Vitest（前端文案）。

**Spec:** `docs/superpowers/specs/2026-10-27-prod-approval-policy-design.md`

## Global Constraints

- **不执行任何 git commit / git add**：工作区已含同一批文件的前序未提交改动（round-1 修复），提交时机由用户决定。每个任务以「测试通过」收尾，不以提交收尾。
- 交互判定只有一处来源：`ctx.metadata.get(AWAIT_APPROVAL_KEY)`（`agent/harness/reins/core.py:33`）。不新增第二个交互标志。
- prod 拒绝文案必须保留 `POLICY_DENY_MARKER` 子串「未预授权」、`allowlist` 与「不要再构造替代写法」三处（headless 判定与 ConfidenceMonitor 豁免依赖前两者，现有测试钉住第三者）。
- prod 仍然不交给模型自动裁决（`permission_mode="auto"` 在 prod 不调用 `try_auto_approve`）。
- 测试命令一律在仓库根目录执行：`python -m pytest backend/tests/... -v`。

---

### Task 1: FilesystemGuard — shell 命令的 prod 交互审批

**Files:**
- Modify: `backend/agent/harness/reins/guards.py`（imports 25-37、`_prod_deny_message` 42-56、prod 分支 168-208、auto-approve 223-230）
- Test: `backend/tests/test_permission_improvements.py`

**Interfaces:**
- Consumes: `AWAIT_APPROVAL_KEY`（`agent/harness/reins/core.py:33`）、`PermissionGate.check_permission/grant/is_permanently_allowed`。
- Produces: prod 交互路径返回带 `approval_key` 的 `permission_needed` 决策（Driver 据此登记等待）；无人值守路径的 `intercept_reason` 仍含 `POLICY_DENY_MARKER`。

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_permission_improvements.py` 顶部 import 区加入：

```python
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
```

在 `# ── 问题 4` 相关测试之后（约 205 行 `_guard` 定义之后）追加：

```python
# ── 2026-10-27：prod 交互审批恢复 ──

def _interactive(ctx: RunContext) -> RunContext:
    """交互式 run：routes/agent.py 对用户发起的 run 置位的同一开关。"""
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


def test_prod_dangerous_command_cards_when_interactive():
    """交互 prod：人在卡片前，危险命令弹卡（仅本次/始终允许），不再直接拒绝。"""
    g = _guard()
    ctx = _interactive(_ctx("prod"))
    _log(ctx)
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    assert POLICY_DENY_MARKER not in d.intercept_reason
    assert any(e.kind == "permission_needed" for e in d.events)
    assert d.approval_key, "发卡必须带等待键，否则 Driver 不会停住等人"
    assert "无人值守" not in "\n".join(m["content"] for m in d.inject_messages)


def test_prod_interactive_once_grant_recheck_allows():
    """「允许本次」写入 once 池后，重判同一调用即放行。"""
    g = _guard()
    ctx = _interactive(_ctx("prod"))
    _log(ctx)
    asyncio.run(g.check(ctx))
    g.permission_gate.grant(
        "shell_exec", scope="once", command=CMD, run_id=ctx.run_id)
    assert asyncio.run(g.check(ctx)).intercepted is False


def test_prod_interactive_always_grant_persists_to_new_run():
    """「始终允许」= 预登记：同一主机的新 run 直接免审。"""
    g = _guard()
    ctx = _interactive(_ctx("prod"))
    _log(ctx)
    asyncio.run(g.check(ctx))
    g.permission_gate.grant(
        "shell_exec", scope="always", command=CMD,
        conversation_id=ctx.conversation_id, run_id=ctx.run_id)
    assert asyncio.run(g.check(ctx)).intercepted is False

    fresh = _interactive(_ctx("prod"))
    _log(fresh)
    assert asyncio.run(g.check(fresh)).intercepted is False


def test_prod_interactive_skips_model_auto_approve(monkeypatch):
    """prod 不交给模型裁决：auto 档也直接弹卡给用户。"""
    calls = []

    async def _spy(*_a, **_kw):
        calls.append(1)
        return None

    monkeypatch.setattr(_guards_mod, "try_auto_approve", _spy)
    g = _guard()
    ctx = _interactive(_ctx("prod"))
    ctx.metadata["permission_mode"] = "auto"
    _log(ctx)
    d = asyncio.run(g.check(ctx))
    assert calls == []
    assert any(e.kind == "permission_needed" for e in d.events)


def test_prod_unattended_deny_says_no_one_can_confirm():
    """无人值守：仍拒绝，但文案说清「没人能点卡」，而不是「拒绝临时授权」。"""
    g = _guard()
    ctx = _ctx("prod")
    _log(ctx)
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    assert POLICY_DENY_MARKER in d.intercept_reason
    inject = "\n".join(m["content"] for m in d.inject_messages)
    assert "无人值守" in inject and "allowlist" in inject
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest backend/tests/test_permission_improvements.py -q -k "prod_dangerous_command_cards_when_interactive or interactive_once or always_grant or skips_model or unattended_deny_says"`
Expected: FAIL —— 前三个测试拿到的仍是 `intercept_reason` 含「未预授权」的拒绝；`skips_model` 的 `calls` 非空（当前 prod 分支直接 return 前不会调用，但新测试的判据是发卡，仍会失败）；`unattended_deny_says` 文案不含「无人值守」。

- [x] **Step 3: 实现**

在 `backend/agent/harness/reins/guards.py` 的 `.core` import 列表最前面加入：

```python
from .core import (
    AWAIT_APPROVAL_KEY,
    _audit_environment,
    ...
)
```

把 `_prod_deny_message` 的返回句改为（保留开头与 marker）：

```python
    return (
        # POLICY_DENY_MARKER 是机器可读的拦截标记（headless 事件流据此判 blocked），
        # 与展示文案同串但语义独立——改文案别丢它
        f"[生产环境]（{POLICY_DENY_MARKER}）{effect}"
        "本次运行无人值守（定时任务 / 工作流），无法弹卡等待确认：危险命令需预先"
        "登记在权限白名单中，或在交互会话中重新发起以现场确认。"
        "本次执行已被拒绝、未写入终端。"
    )
```

把 prod 分支条件改为：

```python
                # 生产环境强化：只有无人值守时才坚持预授权 allowlist ——
                # 交互会话里用户就在卡片前，确认本身即授权来源（仅本次/始终允许）；
                # 定时任务/headless 没人可点卡，只能要求预先登记。
                env = _audit_environment(ctx)
                interactive = bool(ctx.metadata.get(AWAIT_APPROVAL_KEY))
                if env == "prod" and not interactive:
                    if not self.permission_gate.is_permanently_allowed(
                            tool, args, bound_command=bound, host=_target_host(ctx)):
```

把该分支内的模型注入文案改为：

```python
                            inject_messages=[{
                                "role": "user",
                                "content": (
                                    f"[系统拦截] 当前目标为生产环境，且本次运行无人值守，"
                                    f"无法弹卡等待用户确认。危险命令（{reason}）需预先在"
                                    "权限 allowlist 中登记后方可执行；也可提示用户在交互会话中"
                                    "重新发起以现场确认。"
                                    "注意：换用其他变体命令（find -delete、脚本封装等）同样会被拒绝，"
                                    "不要再构造替代写法。请停止尝试，直接告知用户上面两条出路。"
                                ),
                            }],
```

把 auto-approve 调用条件改为（注意分支缩进）：

```python
                        # prod 不交给模型裁决：生产环境的「谁有权确认」不随审批
                        # 模式放宽（交互 prod 在这里直接发卡给用户）。
                        if env != "prod" and not self.permission_gate.denied_for_call(
                                tool, args, ctx.run_id,
                                str(cmd) if cmd else None):
                            decision = await try_auto_approve(
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest backend/tests/test_permission_improvements.py -q`
Expected: PASS（含原 `test_prod_deny_reason_carries_policy_marker_and_stop_guidance`）。

- [x] **Step 5: 回归同文件相关的 prod 内容闸测试**

Run: `python -m pytest backend/tests/test_permission_gate_content_bound_tools.py backend/tests/test_log_cleanup_tool.py -q`
Expected: PASS（这些 ctx 均未置交互标志，走无人值守拒绝路径）。

- [x] **Step 6: 校准两处过时注释（不改断言）**

`backend/tests/test_log_cleanup_tool.py::test_prod_delete_still_requires_allowlist` 的 docstring 改为：

```python
    """预览豁免不能顺带放走删除：无人值守 prod 的删除仍须预登记 allowlist。"""
```

`backend/tests/test_permission_gate_content_bound_tools.py:195` 的注释改为：

```python
    # 无人值守 prod 下临时审批通道根本不给（没有 permission_needed 卡片）
```

Run: `python -m pytest backend/tests/test_permission_gate_content_bound_tools.py backend/tests/test_log_cleanup_tool.py -q`
Expected: PASS。

---

### Task 2: PermissionGate — 工具级危险操作的同一矩阵

**Files:**
- Modify: `backend/agent/harness/reins/gate.py`（imports 28-41、prod 分支 1006-1052、auto-approve 1070-1083）
- Test: `backend/tests/test_sse_permission_and_run_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 确立的同一判据（`AWAIT_APPROVAL_KEY`）。
- Produces: 交互 prod 的 `service_manage stop/restart` 等工具级 dangerous 返回 `permission_needed` + `approval_key`；无人值守 prod 仍返回 `prod_deny_no_allowlist`。

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_sse_permission_and_run_lifecycle.py` 的 gate 测试 import 区（约 166-169 行）加入：

```python
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
```

在 `test_prod_dangerous_tool_requires_preallocation` 之前追加：

```python
def _interactive(ctx: RunContext) -> RunContext:
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx


def test_prod_dangerous_tool_cards_when_interactive(monkeypatch):
    """交互 prod：工具级 dangerous 未预登记 → 弹卡（仅本次/始终允许），不再直接拒绝。"""
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    assert "生产环境" not in d.intercept_reason
    assert any(e.kind == "permission_needed" for e in d.events)
    assert d.approval_key


def test_prod_dangerous_tool_once_grant_recheck_allows(monkeypatch):
    """「允许本次」后重判：once 池命中，工具放行。"""
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    asyncio.run(g.check(ctx))
    g.grant("service_manage", scope="once", command=None, run_id=ctx.run_id)
    assert asyncio.run(g.check(ctx)).intercepted is False


def test_prod_interactive_skips_auto_judge(monkeypatch):
    """prod 不走模型裁决（auto 档也一样）。"""
    calls = []

    async def _spy(*_a, **_kw):
        calls.append(1)
        return None

    monkeypatch.setattr(_gate_mod, "try_auto_approve", _spy)
    g = _prod_gate(monkeypatch)
    ctx = _interactive(_prod_ctx(
        "service_manage", {"action": "stop", "name": "nginx"}, "prod"))
    ctx.metadata["permission_mode"] = "auto"
    asyncio.run(g.check(ctx))
    assert calls == []


def test_prod_unattended_tool_deny_says_no_one_can_confirm(monkeypatch):
    """无人值守：仍拒绝，文案说清「没人能点卡」。"""
    g = _prod_gate(monkeypatch)
    ctx = _prod_ctx("service_manage", {"action": "stop", "name": "nginx"}, "prod")
    d = asyncio.run(g.check(ctx))
    assert d.intercepted is True
    inject = "\n".join(m["content"] for m in d.inject_messages)
    assert "无人值守" in inject and "allowlist" in inject
```

把 `test_prod_dangerous_tool_requires_preallocation` 的 docstring 改为：

```python
    """无人值守 prod：service_manage stop（升级为 dangerous）未预授权 → 拒绝。

    交互会话（见 test_prod_dangerous_tool_cards_when_interactive）由用户现场确认；
    这里钉住无人值守仍只认预登记 allowlist。
    """
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest backend/tests/test_sse_permission_and_run_lifecycle.py -q -k "prod_dangerous_tool or skips_auto_judge or unattended_tool_deny"`
Expected: FAIL —— 交互 prod 仍返回拒绝；无人值守注入文案不含「无人值守」。

- [x] **Step 3: 实现**

在 `backend/agent/harness/reins/gate.py` 的 `.core` import 列表加入 `AWAIT_APPROVAL_KEY`（置于 `_COMMAND_EXEC_TOOLS` 之前）。

把危险工具分支改为：

```python
        # dangerous → 需每次授权（不可 session 级缓存，防止首次授权后 Agent 执行任意高危命令）
        if risk == "dangerous":
            # prod 强化：只有无人值守时才要求预登记 —— 交互会话里用户可现场点卡
            # （仅本次 / 始终允许），「始终允许」写入的正是同一条 allowlist。
            # 与 FilesystemGuard 的 shell 命令同一矩阵，两处不得漂移。
            interactive = bool(ctx.metadata.get(AWAIT_APPROVAL_KEY))
            if environment == "prod" and not interactive and not self.is_permanently_allowed(
                    tool, args, bound_command=bound, host=_target_host(ctx)):
```

把该拒绝分支的 warning message 改为：

```python
                            "message": (
                                # POLICY_DENY_MARKER 供 headless 事件流判 blocked（机器可读）
                                f"[生产强化] 生产环境危险工具{POLICY_DENY_MARKER}（无人值守）: {tool} "
                                "需预先登记 allowlist，或在交互会话中重新发起以现场确认。"
                            ),
```

把该拒绝分支的注入文案改为：

```python
                        "content": (
                            f"[系统拦截] 当前目标为生产环境，且本次运行无人值守，"
                            f"无法弹卡等待用户确认。危险工具 {tool} 需预先在"
                            "权限 allowlist 中登记后方可执行；也可提示用户在交互会话中"
                            "重新发起以现场确认。"
                            "换用其他工具或参数变体同样会被拒绝，不要再尝试替代写法。"
                            "请停止尝试，直接告知用户上面两条出路。"
                        ),
```

把 auto-approve 段改为：

```python
        # 自动审批（permission_mode="auto"）：工具级 dangerous / moderate 不可逆
        # 未授权时，弹卡前先让模型裁决一次（fail-closed，见 auto_approve 模块）。
        # prod 不走裁决：交互 prod 由用户点卡，无人值守 prod 已在上面拒绝。
        # bound_command 与上方 check_permission 同源：裁决标的必须等于授权键绑定的
        # 那条命令，否则模型批的是文件正文、键却绑在写模板路径上（两处不同物）。
        # S2-01 修复：check_permission 返回 False 无法区分「还没批过」与「用户拒绝
        # 过」，原实现把两者一视同仁送进裁决器 ⇒ 自动审批可推翻用户的显式拒绝
        # （FilesystemGuard 有这道门、这里漏了 —— 同一族防护写一处漏另一处）。
        if environment != "prod" and not self.denied_for_call(
                tool, args, ctx.run_id, bound):
            decision = await try_auto_approve(
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest backend/tests/test_sse_permission_and_run_lifecycle.py backend/tests/test_service_manage_command_bound.py backend/tests/test_permission_gate_content_bound_tools.py backend/tests/test_permission_policy_ttl.py backend/tests/test_allowlist_host_scope.py -q`
Expected: PASS。

---

### Task 3: 澄清向导 — 四道跳过闸

**Files:**
- Modify: `backend/agent/harness/reins/clarification.py`（imports 26-33、dataclass 511-550、调用点 671-674）
- Modify: `backend/agent/harness/reins/defaults.py:84-88`
- Test: `backend/tests/test_clarification_wizard.py`、`backend/tests/test_clarification_blocking.py`、`backend/tests/test_clarification_answer_binding_and_labels.py`、`backend/tests/test_harness_feedback_loop.py`

**Interfaces:**
- Consumes: `AWAIT_APPROVAL_KEY`、`resolve_registry`、`extract_bound_command`、`classify_command/classify_constructed_command`、`registry.get(tool).meta.risk_level`。
- Produces: `ClarificationInterceptor(saddle=...)`（不再接受 `permission_gate`）；`_skip_clarification(ctx, tool, args) -> bool`。

- [x] **Step 1: 迁移测试上下文（先改测试基础设施）**

`backend/tests/test_clarification_wizard.py`：
- import 区加入 `from agent.harness.reins.core import AWAIT_APPROVAL_KEY`
- `_make_ctx` 改为：

```python
def _make_ctx(tool: str = "shell_exec", args: dict | None = None,
              interactive: bool = True) -> RunContext:
    """构造一个最小化的 RunContext 用于拦截器测试。

    默认交互式：向导是交互会话的能力，无人值守运行不会建题（见 _skip_clarification）。
    """
    ctx = RunContext.new(task="test task", config=AgentConfig())
    if interactive:
        ctx.metadata[AWAIT_APPROVAL_KEY] = True
    ctx.tool_calls_log = [{
        "tool": tool,
        "args": args or {"command": "rm -rf *"},
        "iteration": 0,
        "success": None,
    }]
    return ctx
```

`backend/tests/test_clarification_answer_binding_and_labels.py`：
- import 区加入 `from agent.harness.reins.core import AWAIT_APPROVAL_KEY`
- `_ctx` 在返回前加一行：

```python
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
```

`backend/tests/test_harness_feedback_loop.py`：
- import 区加入 `from agent.harness.reins.core import AWAIT_APPROVAL_KEY`
- `_ctx` 改为：

```python
def _ctx() -> RunContext:
    ctx = RunContext.new(task="测试", config=AgentConfig())
    ctx.metadata[AWAIT_APPROVAL_KEY] = True
    return ctx
```

- [x] **Step 2: 写失败测试并改写旧断言**

`backend/tests/test_clarification_wizard.py` 中把整个 `TestBlockedCallsSkipWizard` 类（约 616-698 行）替换为：

```python
class TestBlockedCallsSkipWizard:
    """四类调用不问向导：无人可答、prod、hardline、有效风险 safe。

    事故现场（2026-10-27）：`rm -rf /home/admin-pl/.trae-cn-server/manager-logs`
    是 safe 调用（托管日志目录）却连弹 3 步向导；prod 危险命令答完仍被拒。
    """

    @pytest.fixture(autouse=True)
    def _clear_store(self):
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()
        yield
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()

    @pytest.mark.asyncio
    async def test_hardline_command_skips_wizard(self):
        it = ClarificationInterceptor()
        ctx = _make_ctx(
            args={"command": "rm -r /home/admin-pl/.cache/pip && echo \"OK\""})
        d = await it.check(ctx)
        assert d.intercepted is False, "hardline 命令答什么都执行不了，不该开向导"
        assert ctx.run_id not in _CLARIFICATION_CONTEXT

    @pytest.mark.asyncio
    async def test_unattended_run_skips_wizard(self):
        """非交互 run 没人能答题：建题只会把模型拖进重试。"""
        it = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf /tmp/*"}, interactive=False)
        assert (await it.check(ctx)).intercepted is False
        assert ctx.run_id not in _CLARIFICATION_CONTEXT

    @pytest.mark.asyncio
    async def test_prod_never_asks(self):
        """prod 由审批卡承载确认，向导不出现（无论是否预登记）。"""
        it = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf /tmp/*"})
        ctx.environment = "prod"
        assert (await it.check(ctx)).intercepted is False
        assert ctx.run_id not in _CLARIFICATION_CONTEXT

    @pytest.mark.asyncio
    async def test_safe_recursive_rm_skips_wizard(self):
        """事故命令：托管日志目录 → safe，答完也只会直接执行，不该先问 3 步。"""
        it = ClarificationInterceptor()
        ctx = _make_ctx(args={
            "command": "rm -rf /home/admin-pl/.trae-cn-server/manager-logs"})
        assert (await it.check(ctx)).intercepted is False

    @pytest.mark.asyncio
    async def test_dangerous_command_still_asks(self):
        """test 环境 + 交互 + dangerous：向导保留（随后才轮到审批卡）。"""
        it = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf /tmp/*"})
        assert (await it.check(ctx)).intercepted is True
```

`backend/tests/test_clarification_blocking.py` 中把 `test_unattended_run_does_not_wait`（约 208-217 行）替换为：

```python
@pytest.mark.asyncio
async def test_unattended_run_skips_wizard_and_executes():
    """定时任务无人可答：不开向导（开了也没人等），直接走原执行/拦截路径。"""
    ctx = _ctx(interactive=False, max_iterations=2)
    harness = _Harness()

    events = await asyncio.wait_for(_collect(harness, ctx), 2)

    assert not any(ev.kind == "clarification_needed" for ev in events)
    assert harness.saddle.executed == ["shell_exec"]
    assert harness.stirrups.rounds == 2
```

- [x] **Step 3: 运行确认失败**

Run: `python -m pytest backend/tests/test_clarification_wizard.py backend/tests/test_clarification_blocking.py -q`
Expected: FAIL —— `test_unattended_run_skips_wizard`、`test_prod_never_asks`、`test_safe_recursive_rm_skips_wizard` 与改写后的 blocking 用例失败（当前仍会建题）。

- [x] **Step 4: 实现**

`backend/agent/harness/reins/clarification.py`：
- `.core` import 加入 `AWAIT_APPROVAL_KEY`，删除 `_target_host`。
- dataclass 里删除 `permission_gate` 字段，把 `_blocked_before_approval` 整体替换为：

```python
    # 算绑定命令用（saddle.registry）；向导不再判断 allowlist，故不需要权限门。
    saddle: Optional[Any] = None

    def _skip_clarification(self, ctx: RunContext, tool: str,
                            args: dict) -> bool:
        """本次调用是否不该开向导。

        四类跳过：
        · 非交互 run —— 没人能作答，Driver 本来就不会等，建题只会把模型拖进重试；
        · prod —— 危险调用由审批卡承载（卡片自带命令与后果说明），safe 调用直接执行；
        · hardline —— 无条件阻断，答什么都执行不了；
        · 有效风险 safe —— 不需要授权就会执行，不该先拦下来问 3 个问题。
        """
        if not ctx.metadata.get(AWAIT_APPROVAL_KEY):
            return True
        if (getattr(ctx, "environment", "") or "") == "prod":
            return True

        registry = resolve_registry(ctx, self.saddle)
        bound, is_free_form = extract_bound_command(registry, tool, args)
        if bound:
            level, _reason = (classify_command if is_free_form
                              else classify_constructed_command)(bound)
            return level in ("hardline", "safe")

        # 无绑定命令：看工具级风险；未知风险不放宽（宁可多问一次）
        get = getattr(registry, "get", None)
        impl = get(tool) if callable(get) else None
        meta = getattr(impl, "meta", None)
        return getattr(meta, "risk_level", "") == "safe"
```

- 把调用点（现 `_blocked_before_approval(ctx, tool, args)`）改为：

```python
        # 向导只在「有人答 + 答完能执行 + 真的需要授权」时才有意义。
        if self._skip_clarification(ctx, tool, args):
            return ReinDecision()
```

`backend/agent/harness/reins/defaults.py`：

```python
    # ── 澄清向导（PRE_TOOL_CALL）──
    # saddle：算绑定命令用。是否建题由拦截器按「交互 / prod / hardline / safe」
    # 四道闸自行判定（见 _skip_clarification）。
    interceptors.append(ClarificationInterceptor(saddle=saddle))
```

- [x] **Step 5: 运行确认通过**

Run: `python -m pytest backend/tests/test_clarification_wizard.py backend/tests/test_clarification_blocking.py backend/tests/test_clarification_answer_binding_and_labels.py backend/tests/test_clarification_sse_contract.py backend/tests/test_harness_feedback_loop.py backend/tests/test_plan_mode.py -q`
Expected: PASS。

- [x] **Step 6: 检查没有残留的 `permission_gate=` 构造**

Run: `grep -rn "ClarificationInterceptor(.*permission_gate" backend src`
Expected: 无输出。

---

### Task 4: 前端与文档文案对齐

**Files:**
- Modify: `src/components/ssh/ConnectionModal.vue:127-129`
- Modify: `src/components/agent/RollbackPanel.vue:157-161`
- Test: `src/components/agent/RollbackPanel.test.ts:101-107`
- Modify: `docs/p0-ops-agent-enhancements.md:390`、`:434`
- Modify: `backend/agent/harness/reins/auto_approve.py:13-15`（docstring）
- Modify: `backend/agent/harness/driver.py:1387`（注释）

**Interfaces:**
- Consumes: Task 1/2 的最终语义。
- Produces: 用户可见文案不再声称「prod 不弹临时审批卡」。

- [x] **Step 1: 改前端文案**

`src/components/ssh/ConnectionModal.vue`：

```vue
            <div class="env-hint" v-if="form.environment === 'prod'">
              [注意] 生产环境：交互会话中危险命令会弹审批卡，由你现场确认；定时任务、工作流等无人值守运行无法弹卡，危险命令必须预先登记 allowlist。
            </div>
```

`src/components/agent/RollbackPanel.vue`：

```ts
const prodAllowlistHint = computed(() =>
  environment.value === 'prod'
    ? '生产环境：危险操作会在审批卡上由你现场确认（允许本次 / 始终允许）。选「始终允许」即把该命令写入本主机 allowlist；定时任务、工作流等无人值守运行无法弹卡，必须预先登记，否则会被拒绝。'
    : '',
)
```

- [x] **Step 2: 更新前端文案断言**

`src/components/agent/RollbackPanel.test.ts` 中替换该 it：

```ts
  it('prod 说清两条通道：交互现场确认 + 无人值守预登记', () => {
    expect(rollbackPanel).toContain("environment.value === 'prod'")
    expect(rollbackPanel).toContain('现场确认')
    expect(rollbackPanel).toContain('无人值守运行无法弹卡')
    expect(rollbackPanel).toContain('预先登记')
  })
```

- [x] **Step 3: 运行前端测试**

Run: `npx vitest run src/components/agent/RollbackPanel.test.ts`
Expected: PASS。

- [x] **Step 4: 改文档与注释**

`docs/p0-ops-agent-enhancements.md:390` 的第 5 条整段替换为：

```markdown
5. **B 的 undo_restore 在 prod 的行为（2026-10-27 修订）**：交互会话（`await_approval=True`）里 dangerous 工具弹审批卡，由用户选「允许本次 / 始终允许」——「始终允许」写入主机限域的 allowlist；无人值守运行（定时任务 / headless / runbook）没人可点卡，dangerous 工具仍只接受预登记 allowlist（`prod_deny_no_allowlist`）。因此 prod 回滚在交互界面中可现场确认；若要把回滚放进定时任务/剧本，则需两步预登记。`existed=False` 的反向 `rm -f` 走同一矩阵。
```

`:434` 的 M1 行只改一处措辞：`而 prod 未预登记（`gate.py`/`guards.py` 的 `prod_deny_no_allowlist`）只发 warning` → `而 prod 无人值守未预登记（`gate.py`/`guards.py` 的 `prod_deny_no_allowlist`）只发 warning`。

`backend/agent/harness/reins/auto_approve.py` docstring 中该条改为：

```
  · prod —— 交互会话由用户现场点卡确认；无人值守只接受预授权 allowlist。
    让 LLM"替用户批准"会击穿「生产环境谁有权确认」这条边界；
```

`backend/agent/harness/driver.py:1387` 注释改为：

```python
            # hardline / 曾被拒绝 / 无人值守的 prod 未预登记……那些是「直接否掉」，没有人要等
```

- [x] **Step 5: 确认没有残留的旧口径**

Run: `grep -rn "不弹临时审批卡\|拒绝临时审批\|不再临时弹窗审批" src backend/agent docs`
Expected: 无输出（`src/*.test.ts` 里模拟旧 warning 的 fixture 不在此列，保留不动）。

---

### Task 5: 全量回归

**Files:** 无新增改动（只跑测试）。

- [x] **Step 1: 全量后端测试**

Run: `python -m pytest backend/tests -q`
Expected: 全部通过（基线 3375 passed；数量会因新增用例上升）。若有失败，先判断是否属于「旧口径断言」，按 Task 1-3 建立的语义修正测试，不要改实现去迎合过时文案。

- [x] **Step 2: 前端相关测试**

Run: `npx vitest run src/components/agent/RollbackPanel.test.ts src/stores/agent.test.ts`
Expected: PASS。

- [x] **Step 3: 事故链路自检（脚本）**

Run:

```bash
cd backend && python - <<'PY'
import asyncio
import agent.harness.reins.guards as guards_mod
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from agent.harness.reins.guards import FilesystemGuard
from agent.harness.reins.gate import PermissionGate
from agent.harness.reins.clarification import ClarificationInterceptor

async def _noop_audit(**_kw):
    return None
guards_mod.append_record_async = _noop_audit   # 不往真实审计库写

SAFE = "rm -rf /home/admin-pl/.trae-cn-server/manager-logs"
DANGER = "rm -rf /tmp/*"

def ctx(cmd, env, interactive):
    c = RunContext.new(task="清理临时日志", config=AgentConfig(), environment=env)
    if interactive:
        c.metadata[AWAIT_APPROVAL_KEY] = True
    c.tool_calls_log.append({"tool": "shell_exec", "args": {"command": cmd},
                             "iteration": 0, "success": None})
    return c

clar = ClarificationInterceptor()
print("向导(prod,交互,事故命令) intercepted =",
      asyncio.run(clar.check(ctx(SAFE, "prod", True))).intercepted)

for label, cmd, interactive in (
        ("守卫(prod,交互,事故命令)", SAFE, True),
        ("守卫(prod,交互,危险命令)", DANGER, True),
        ("守卫(prod,无人值守,危险命令)", DANGER, False)):
    g = PermissionGate()
    g._confirmed_permanent = set()
    d = asyncio.run(FilesystemGuard(permission_gate=g).check(ctx(cmd, "prod", interactive)))
    print(label, "intercepted =", d.intercepted,
          "events =", [e.kind for e in d.events],
          "reason =", d.intercept_reason)
PY
```

Expected:
- `向导(prod,交互,事故命令) intercepted = False`（prod 不建题）
- `守卫(prod,交互,事故命令) intercepted = False`（safe 直接执行）
- `守卫(prod,交互,危险命令) intercepted = True`，`events` 含 `permission_needed`（发卡等人）
- `守卫(prod,无人值守,危险命令) intercepted = True`，`reason` 含「未预授权」

---

## Self-Review

- **Spec coverage**：§2 授权矩阵 → Task 1（shell）+ Task 2（工具级）；§2.1 auto 排除 → Task 1/2；§2.2 文案与 marker → Task 1/2；§3 四道闸 → Task 3；§4 文件与前端/文档 → Task 1-4；§5 测试计划 → Task 1-3 + Task 5；§6 取舍无实现项。
- **Placeholder scan**：无 TBD / TODO；所有测试与实现均给出完整代码。
- **Type consistency**：`AWAIT_APPROVAL_KEY`（不是 `await_approval` 字面量）；`_skip_clarification(ctx, tool, args)`；`ClarificationInterceptor(saddle=...)`；`interactive = bool(ctx.metadata.get(AWAIT_APPROVAL_KEY))` 在 guard/gate 同名同义。
- **Commit policy**：Global Constraints 已声明不提交；Task 5 以全量测试为收尾。

---

## 验证记录（2026-10-27，ZCode 会话执行）

- 实现（Task 1-3 的 guards/gate/clarification/defaults 改动与 Task 4 文案）随 round-1 修复进入 `558fc3e`，本会话逐文件核对与计划一致，无需补码。
- Task 5 验证结果：
  - 计划点名的 14 个测试文件：**400 passed**（含 Task 1/2 新增的 prod 交互发卡、once/always 授权、auto 排除、无人值守文案用例）；
  - 前端 `RollbackPanel.test.ts` + `agent.test.ts`：**72 passed**；
  - 事故链路自检脚本：**4/4 预期命中**（向导 prod 不建题 / 交互 safe 直接执行 / 交互危险发卡 `permission_needed` / 无人值守拒绝含「未预授权」）；
  - 全量 `pytest backend/tests`：3404 passed + 3 failed，失败项为 `test_bot_store`（另一工作流当时实现未保存的中间态），复跑 **4/4 通过**，与本计划无关。
- T4 Step 5 旧口径残留：仅计划/规格两个元文档引用旧文案作说明，`src`/`backend/agent` 无残留；T3 Step 6 无 `permission_gate=` 构造残留。
