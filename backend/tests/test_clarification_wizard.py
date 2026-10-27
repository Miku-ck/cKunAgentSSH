"""澄清向导（Clarification Wizard）回归测试。

覆盖：
  - _shell_detect_vague：明确命令返回 None，模糊命令返回原因
  - P1-2: find 只读命令不触发，仅 find | xargs 触发
  - _step_to_dict / _build_clarification_needed：事件结构正确
  - _format_answers：格式化用户答案文本
  - ClarificationInterceptor.check 三链路：触发→拦截→放行
  - P0-3: 路由层 answers 校验 + custom_text 截断
  - P1-5: _enforce_clarification_cap FIFO 淘汰

运行: python -m pytest backend/tests/test_clarification_wizard.py -v
"""
import asyncio

import pytest

import agent.harness.reins as reins_mod
from agent.harness.contracts import AgentConfig, RunContext, TraceEvent
from agent.harness.reins.core import AWAIT_APPROVAL_KEY
from agent.harness.reins import (
    ClarificationInterceptor,
    ClarificationOption,
    ClarificationStep,
    _CLARIFIABLE_TOOLS,
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    _build_clarification_needed,
    _enforce_clarification_cap,
    _file_write_build_steps,
    _file_write_detect_vague,
    _format_answers,
    _service_build_steps,
    _service_detect_vague,
    _shell_build_steps,
    _shell_detect_vague,
    _step_to_dict,
    record_clarification_answers,
)


# ── helpers ──

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


# ── _shell_detect_vague ──

class TestShellDetectVague:
    """_shell_detect_vague 不应误报明确命令，应识别真正的破坏性命令"""

    def test_clean_file_returns_none(self):
        # 明确单文件删除 → 不触发
        assert _shell_detect_vague({"command": "rm file.txt"}) is None

    def test_wildcard_rm_triggers(self):
        reason = _shell_detect_vague({"command": "rm -rf node_modules/*"})
        assert reason is not None
        assert "rm" in reason

    def test_recursive_rm_triggers(self):
        reason = _shell_detect_vague({"command": "rm -rf /tmp/build"})
        assert reason is not None

    def test_find_xargs_triggers(self):
        reason = _shell_detect_vague({"command": "find . -name '*.log' | xargs rm"})
        assert reason is not None

    def test_ls_returns_none(self):
        # 非破坏性命令不触发
        assert _shell_detect_vague({"command": "ls -la"}) is None

    def test_none_args_returns_none(self):
        assert _shell_detect_vague(None) is None
        assert _shell_detect_vague({}) is None

    def test_chmod_wildcard_triggers(self):
        reason = _shell_detect_vague({"command": "chmod -R 777 *"})
        assert reason is not None


# ── _shell_build_steps ──

class TestShellBuildSteps:
    def test_builds_three_steps(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        assert len(steps) == 3
        for s in steps:
            assert isinstance(s, ClarificationStep)
            assert s.options  # 非空
            assert s.allow_custom is True

    def test_first_step_asks_scope(self):
        steps = _shell_build_steps({"command": "rm -rf /tmp/*"})
        assert "范围" in steps[0].question or "作用" in steps[0].question

    def test_steps_have_three_options_each(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        for s in steps:
            assert len(s.options) == 3
            ids = [o.id for o in s.options]
            assert ids == ["A", "B", "C"]


# ── _step_to_dict ──

class TestStepToDict:
    def test_converts_correctly(self):
        step = ClarificationStep(
            question="test question?",
            options=[ClarificationOption(id="A", label="opt A")],
            allow_custom=True,
        )
        d = _step_to_dict(step)
        assert d["question"] == "test question?"
        assert d["options"] == [{"id": "A", "label": "opt A"}]
        assert d["allow_custom"] is True


# ── _build_clarification_needed ──

class TestBuildClarificationNeeded:
    def test_builds_trace_event(self):
        steps = _shell_build_steps({"command": "rm -rf *"})
        ev = _build_clarification_needed(
            run_id="r1", tool="shell_exec", args={"command": "rm -rf *"},
            steps=steps, step=0, total=3, tool_call_id="tc1",
        )
        assert isinstance(ev, TraceEvent)
        assert ev.kind == "clarification_needed"
        assert ev.payload["tool"] == "shell_exec"
        assert ev.payload["total"] == 3
        assert len(ev.payload["steps"]) == 3


# ── _format_answers ──

class TestFormatAnswers:
    def test_empty_answers(self):
        result = _format_answers({})
        assert "跳过" in result

    def test_formatted_steps(self):
        answers = {
            "0": {"choice": "A"},
            "1": {"choice": "custom", "custom_text": "blah"},
        }
        result = _format_answers(answers)
        assert "Q1" in result
        assert "Q2" in result
        assert "blah" in result


# ── ClarificationInterceptor 三链路 ──

class TestClarificationInterceptor:
    @pytest.fixture(autouse=True)
    def _clear_store(self):
        """每个测试前清理 pending 存储，避免跨测试干扰"""
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()
        yield
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()

    @pytest.mark.asyncio
    async def test_vague_command_triggers_clarification(self):
        """链路 1：模糊命令 → 拦截 + clarification_needed 事件"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf /tmp/*"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is True
        assert decision.intercept_reason.startswith("clarification_needed")
        assert len(decision.events) == 1
        ev = decision.events[0]
        assert ev.kind == "clarification_needed"

        # 验证 payload 含 reason + args（前端展示命令上下文）
        assert ev.payload.get("reason"), "reason 不能为空"
        assert "args" in ev.payload
        assert ev.payload["args"].get("command") == "rm -rf /tmp/*"

        # 验证 pending context 已存储（含 reason）
        assert ctx.run_id in _CLARIFICATION_CONTEXT
        assert _CLARIFICATION_CONTEXT[ctx.run_id].get("reason")

    @pytest.mark.asyncio
    async def test_clear_command_not_triggered(self):
        """明确命令 → 不拦截"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "ls -la"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is False
        assert not decision.events

    @pytest.mark.asyncio
    async def test_unregistered_tool_not_triggered(self):
        """不在注册表中的工具不触发"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(tool="file_read", args={"path": "/etc/passwd"})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is False

    @pytest.mark.asyncio
    async def test_pending_re_emits_same_step_and_intercepts(self):
        """链路 2：pending 未完成时重发当前步 + 拦截"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})

        # 第一次触发
        d1 = await interceptor.check(ctx)
        assert d1.intercepted

        # 第二次检查（模拟 LLM 重试同一工具）
        # 给 ctx 再加一条相同工具调用（模拟 LLM 重试）
        ctx.tool_calls_log.append({
            "tool": "shell_exec",
            "args": {"command": "rm -rf *"},
            "iteration": 1,
            "success": None,
        })
        d2 = await interceptor.check(ctx)

        assert d2.intercepted is True
        assert d2.events[0].kind == "clarification_needed"
        assert any("等待用户回答" in (m.get("content", "") or "") for m in d2.inject_messages)

    @pytest.mark.asyncio
    async def test_pending_only_freezes_mutating_tools(self):
        """澄清挂起期间只放行只读观测工具，其余（含非注册表写工具）一律冻结。

        回归点两条：
        1. 曾拦下一切工具调用 —— 用户离席几十秒、模型只想 file_read /
           sys_overview 也逐次吃 clarification_pending，直到 600s TTL 才结束，
           整个 run 被一张没人答的向导拖死。
        2. 修掉 1 之后又放得太宽 —— 判据曾是「不在 _CLARIFIABLE_TOOLS 即放行」，
           于是 file_edit / schedule_op / docker 类写工具在待答期间照常执行：
           用户还在确认「要不要删」，AI 已经把别的东西改了。现按
           contracts.READONLY_TOOLS（无副作用观测白名单）放行，其余冻结。
        """
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})
        assert (await interceptor.check(ctx)).intercepted

        # 只读工具（READONLY_TOOLS）→ 放行，且不得新建第二张向导
        ctx.tool_calls_log[-1] = {
            "tool": "file_read", "args": {"path": "/etc/hosts"},
            "iteration": 1, "success": None,
        }
        d_read = await interceptor.check(ctx)
        assert d_read.intercepted is False
        assert not d_read.events
        assert _CLARIFICATION_CONTEXT[ctx.run_id]["tool"] == "shell_exec"

        # 注册表内的另一个改写工具 → 仍拦截，且重发的是原向导（上下文不被覆盖）
        ctx.tool_calls_log[-1] = {
            "tool": "file_write", "args": {"path": "/etc/passwd", "content": "x"},
            "iteration": 2, "success": None,
        }
        d_write = await interceptor.check(ctx)
        assert d_write.intercepted is True
        assert d_write.events[0].payload["tool"] == "shell_exec"

        # 非注册表但会写盘的工具（file_edit）→ 同样冻结
        ctx.tool_calls_log[-1] = {
            "tool": "file_edit", "args": {"path": "/etc/hosts", "old": "a", "new": "b"},
            "iteration": 3, "success": None,
        }
        d_edit = await interceptor.check(ctx)
        assert d_edit.intercepted is True
        assert d_edit.events[0].payload["tool"] == "shell_exec"
        assert "写操作已冻结" in d_edit.intercept_reason
        _CLARIFICATION_CONTEXT.pop(ctx.run_id, None)

    @pytest.mark.asyncio
    async def test_late_answer_without_pending_is_dropped(self):
        """题目被回收后迟到的答案没有归属工具：不得注入给下一条无关调用。

        record_clarification_answers 在无 pending 时会补一个 tool="" 的桩
        （保证 TTL 清扫能回收孤儿答案）。原实现把「归属工具为空」当成
        「随便谁都可以消费」，于是超时后用户补交的答案会被紧接着的下一次
        工具调用吃掉，并把一段与它无关的澄清结论注入上下文。
        """
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(tool="file_read", args={"path": "/etc/hosts"})
        record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})

        decision = await interceptor.check(ctx)

        assert decision.intercepted is False
        assert not decision.events
        assert not decision.inject_messages
        assert ctx.run_id not in _CLARIFICATION_DONE
        assert ctx.run_id not in _CLARIFICATION_CONTEXT

    @pytest.mark.asyncio
    async def test_timed_out_signature_does_not_reopen_wizard(self):
        """等满 TTL 的同一条命令被重试：不再弹新向导，直接拦截并说明超时。

        超时后 Driver 保留签名（clarification_waited），不会再登记等待。
        若拦截器照常建题，前端会重新弹一张没人等答案的向导 —— run 已经
        往下跑了，用户迟到的作答无处可去。
        """
        from agent.harness.reins.core import tool_call_signature

        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})
        ctx.metadata["clarification_waited"] = {tool_call_signature(ctx.active_tool_call)}

        decision = await interceptor.check(ctx)

        assert decision.intercepted is True
        assert decision.intercept_reason.startswith("clarification_timeout")
        assert not decision.events, "超时签名重试不得重开向导"
        assert ctx.run_id not in _CLARIFICATION_CONTEXT

    @pytest.mark.asyncio
    async def test_done_answers_inject_context_and_release(self):
        """链路 3：答案就绪后 → 注入上下文 + clarification_done + 放行"""
        interceptor = ClarificationInterceptor()
        ctx = _make_ctx(args={"command": "rm -rf *"})

        # 第 1 步：拦截触发
        d1 = await interceptor.check(ctx)
        assert d1.intercepted

        # 模拟前端提交答案 → routes 写入 _CLARIFICATION_DONE
        _CLARIFICATION_DONE[ctx.run_id] = {"0": {"choice": "A"}, "1": {"choice": "B"}}
        ctx_info = _CLARIFICATION_CONTEXT.get(ctx.run_id)
        if ctx_info:
            ctx_info["_done"] = True

        # 第 2 步：框架再次调用 check
        decision = await interceptor.check(ctx)

        assert decision.intercepted is False
        assert len(decision.events) == 1
        assert decision.events[0].kind == "clarification_done"

        # inject_messages 包含用户答案摘要
        assert any("[用户澄清结果]" in (m.get("content", "") or "") for m in decision.inject_messages)


# ── P1-2: find 只读误判修复 ──

class TestFindNotDestructive:
    """P1-2: 只读 find 命令不再触发澄清（仅 find | xargs 仍触发）"""

    def test_find_alone_no_trigger(self):
        """find . -name '*.pdf' 是只读的，不应触发"""
        assert _shell_detect_vague({"command": "find . -name '*.pdf'"}) is None

    def test_find_with_type_no_trigger(self):
        """find /tmp -type f 是只读的"""
        assert _shell_detect_vague({"command": "find /tmp -type f -mtime +7"}) is None

    def test_find_pipe_xargs_triggers(self):
        """find ... | xargs rm 仍视为破坏性"""
        reason = _shell_detect_vague({"command": "find . -name '*.log' | xargs rm"})
        assert reason is not None

    def test_rm_wildcard_still_triggers(self):
        """rm -rf * 仍然触发（回归确认）"""
        assert _shell_detect_vague({"command": "rm -rf *"}) is not None


# ── P1-5: _enforce_clarification_cap FIFO 淘汰 ──

class TestClarificationCap:
    """P1-5: 全局 dict 超容量时应 FIFO 淘汰"""

    def test_cap_evicts_oldest(self):
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()
        # 填充超过上限（设定一个小值便于测试）
        # reins.py 已拆包：常量定义在 clarification 子模块，patch 包属性不传导
        import agent.harness.reins.clarification as _rmod
        orig_cap = _rmod._CLARIFICATION_MAX_ENTRIES
        try:
            _rmod._CLARIFICATION_MAX_ENTRIES = 5
            for i in range(10):
                _CLARIFICATION_DONE[f"old_run_{i}"] = {}
                _CLARIFICATION_CONTEXT[f"old_run_{i}"] = {}
            _enforce_clarification_cap()
            assert len(_CLARIFICATION_DONE) <= 5
            assert len(_CLARIFICATION_CONTEXT) <= 5
            # 老的条目应被淘汰（FIFO 保留最新的）
            assert "old_run_0" not in _CLARIFICATION_DONE
            assert "old_run_9" in _CLARIFICATION_DONE
        finally:
            _rmod._CLARIFICATION_MAX_ENTRIES = orig_cap
            _CLARIFICATION_DONE.clear()
            _CLARIFICATION_CONTEXT.clear()


# ── P0-3: 路由层 answers 校验 ──

class TestRouteValidation:
    """P0-3: 路由层对 answers 结构做防御性校验 + custom_text 截断"""

    def test_valid_answers_pass(self):
        """合法 answers 应通过校验"""
        from routes.agent import ClarificationAnswerRequest
        req = ClarificationAnswerRequest(run_id="r1", answers={
            "0": {"choice": "A"},
            "1": {"choice": "custom", "custom_text": "hello"},
        })
        assert req.answers["0"] == {"choice": "A"}
        assert req.answers["1"]["choice"] == "custom"
        assert req.answers["1"]["custom_text"] == "hello"

    def test_invalid_step_idx_skipped(self):
        """非法 step_idx（非数字）应被过滤"""
        from routes.agent import ClarificationAnswerRequest
        req = ClarificationAnswerRequest(run_id="r1", answers={
            "abc": {"choice": "A"},
            "0": {"choice": "B"},
        })
        assert "abc" not in req.answers
        assert "0" in req.answers

    def test_invalid_choice_skipped(self):
        """非法 choice 应被过滤"""
        from routes.agent import ClarificationAnswerRequest
        req = ClarificationAnswerRequest(run_id="r1", answers={
            "0": {"choice": "Z"},
            "1": {"choice": "A"},
        })
        assert "0" not in req.answers
        assert req.answers["1"]["choice"] == "A"

    def test_custom_text_truncated(self):
        """超长 custom_text 应被截断"""
        from routes.agent import ClarificationAnswerRequest, _CUSTOM_TEXT_MAX_LEN
        long_text = "A" * (_CUSTOM_TEXT_MAX_LEN + 200)
        req = ClarificationAnswerRequest(run_id="r1", answers={
            "0": {"choice": "custom", "custom_text": long_text},
        })
        assert len(req.answers["0"]["custom_text"]) == _CUSTOM_TEXT_MAX_LEN

    def test_non_dict_item_skipped(self):
        """非 dict item 应被过滤"""
        from routes.agent import ClarificationAnswerRequest
        # 注意：pydantic 会尝试 cast；我们用明确无效类型验证清理
        req = ClarificationAnswerRequest(run_id="r1", answers={"0": {"choice": "A"}})
        assert req.answers["0"] == {"choice": "A"}

    def test_too_many_steps_rejected(self):
        """超过 32 个 step 应触发 ValidationError"""
        from routes.agent import ClarificationAnswerRequest
        import pytest
        with pytest.raises(Exception):
            ClarificationAnswerRequest(run_id="r1", answers={
                str(i): {"choice": "A"} for i in range(40)
            })


# ── P1-6: TTL 过期 + run 终态回收 + 运维语境题目 ──

def test_record_clarification_answers_creates_stub_context():
    """孤儿答案（无既有 context）也会被配对桩捕获，供 TTL 回收"""
    reins_mod._CLARIFICATION_DONE.pop("run-orphan", None)
    reins_mod._CLARIFICATION_CONTEXT.pop("run-orphan", None)
    reins_mod.record_clarification_answers("run-orphan", {"0": {"choice": "A"}})
    assert "run-orphan" in reins_mod._CLARIFICATION_DONE
    ci = reins_mod._CLARIFICATION_CONTEXT["run-orphan"]
    assert ci["_done"] is True and "ts" in ci
    reins_mod.release_clarification_run("run-orphan")
    assert "run-orphan" not in reins_mod._CLARIFICATION_DONE
    assert "run-orphan" not in reins_mod._CLARIFICATION_CONTEXT


def test_purge_expired_clarifications_removes_old_entries(monkeypatch):
    done, ctx = reins_mod._CLARIFICATION_DONE, reins_mod._CLARIFICATION_CONTEXT
    done.pop("run-ttl", None)
    ctx.pop("run-ttl", None)
    ctx["run-ttl"] = {"tool": "shell_exec", "steps": [], "_done": False,
                      "ts": 0.0}  # 远早于 TTL
    done["run-ttl"] = {"0": {"choice": "B"}}
    removed = reins_mod._purge_expired_clarifications()
    assert "run-ttl" not in ctx and "run-ttl" not in done
    assert removed >= 1  # expired 计数按 context 条目（done 配对回收不单计）


def test_purge_keeps_fresh_entries():
    import time as _t
    done, ctx = reins_mod._CLARIFICATION_DONE, reins_mod._CLARIFICATION_CONTEXT
    done.pop("run-fresh", None)
    ctx.pop("run-fresh", None)
    ctx["run-fresh"] = {"tool": "shell_exec", "steps": [], "_done": False,
                        "ts": _t.time()}
    reins_mod._purge_expired_clarifications()
    assert "run-fresh" in ctx
    reins_mod.release_clarification_run("run-fresh")


def test_interceptor_releases_after_ttl(monkeypatch):
    """过期未答题不再无限拦截：TTL 后同 run 的模糊命令应重建新题而非重发旧题"""
    import time as _t
    done, ctx = reins_mod._CLARIFICATION_DONE, reins_mod._CLARIFICATION_CONTEXT
    done.pop("run-exp", None)
    ctx.pop("run-exp", None)
    ctx["run-exp"] = {"tool": "shell_exec", "args": {"command": "rm -rf /var/log/*"},
                      "tool_call_id": "x", "steps": [], "current_step": 0,
                      "total": 0, "reason": "r", "_done": False, "ts": 0.0}
    d = ClarificationInterceptor()
    rctx = _make_ctx()
    rctx.run_id = "run-exp"
    decision = asyncio.run(d.check(rctx))
    assert decision.intercepted is True
    assert decision.intercept_reason.startswith("clarification_needed")  # 重建新题而非 pending 重发
    new_ci = ctx["run-exp"]
    assert new_ci["ts"] > _t.time() - 5  # 新时间戳
    reins_mod.release_clarification_run("run-exp")


def test_shell_steps_use_ops_semantics_not_git():
    """澄清题不得出现本地 git 工作区话术（作用范围=目标机/递归/复核）"""
    steps = _shell_build_steps({"command": "rm -rf /var/log/*.gz"})
    assert len(steps) == 3
    parts = [s.question for s in steps]
    for step in steps:
        for opt in step.options:
            parts.append(opt.label)
    all_text = "".join(parts)
    for forbidden in ("git", "commit", "工作区", "仓库"):
        assert forbidden not in all_text
    assert "目标机" in steps[0].question
    assert "undo" in steps[1].options[2].label


# ── 扩注册表（2026-09 演进）：job_run / file_write / service_manage ──

class TestRegistryExtension:
    def _steps_shape(self, steps):
        assert len(steps) == 3
        for s in steps:
            assert isinstance(s, ClarificationStep)
            assert [o.id for o in s.options] == ["A", "B", "C"]
            assert s.allow_custom is True

    def test_registry_contains_command_and_ops_tools(self):
        assert set(_CLARIFIABLE_TOOLS) == {"shell_exec", "job_run", "file_write", "service_manage"}

    def test_every_registry_entry_callable_pair(self):
        for name, (detect, build) in _CLARIFIABLE_TOOLS.items():
            assert callable(detect) and callable(build), name

    # job_run 复用 shell 检测器（cmd 参数）
    def test_job_run_wildcard_cmd_triggers(self):
        assert _shell_detect_vague({"cmd": "rm -rf /var/log/*"}) is not None

    def test_job_run_clean_cmd_not_triggered(self):
        assert _shell_detect_vague({"cmd": "df -h"}) is None

    # file_write：仅系统配置路径触发
    def test_file_write_etc_path_triggers(self):
        assert _file_write_detect_vague({"path": "/etc/nginx/nginx.conf", "content": "x"}) is not None

    def test_file_write_bare_etc_triggers(self):
        assert _file_write_detect_vague({"path": "/etc", "content": "x"}) is not None

    def test_file_write_app_path_not_triggered(self):
        assert _file_write_detect_vague({"path": "/srv/app/config.ini", "content": "x"}) is None

    def test_file_write_steps_shape(self):
        self._steps_shape(_file_write_build_steps({"path": "/etc/foo.conf"}))

    def test_file_write_steps_ask_backup_and_reload(self):
        steps = _file_write_build_steps({"path": "/etc/ssh/sshd_config"})
        all_text = steps[0].question + "".join(o.label for o in steps[0].options) \
            + steps[1].question
        assert "备份" in all_text
        assert "reload" in steps[1].options[1].label or "reload" in steps[1].options[2].label

    # service_manage：仅 stop/restart × 会话/网络关键服务触发
    def test_service_stop_sshd_triggers(self):
        assert _service_detect_vague({"action": "stop", "name": "sshd"}) is not None

    def test_service_restart_with_suffix_triggers(self):
        assert _service_detect_vague({"action": "restart", "name": "sshd.service"}) is not None

    def test_service_status_not_triggered(self):
        assert _service_detect_vague({"action": "status", "name": "sshd"}) is None

    def test_service_noncritical_stop_not_triggered(self):
        assert _service_detect_vague({"action": "stop", "name": "nginx"}) is None

    def test_service_steps_shape(self):
        self._steps_shape(_service_build_steps({"action": "stop", "name": "sshd"}))


class TestInterceptorPriority:
    def test_clarification_runs_before_permission_and_guard(self):
        from agent.harness.reins import FilesystemGuard, PermissionGate
        clar = ClarificationInterceptor()
        assert clar.priority < PermissionGate().priority < FilesystemGuard().priority


# ── 答完也执行不了的调用不弹向导（2026-10-27 事故）──

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


class TestInterceptorExtendedTools:
    @pytest.fixture(autouse=True)
    def _clear_store(self):
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()
        yield
        _CLARIFICATION_DONE.clear()
        _CLARIFICATION_CONTEXT.clear()

    @pytest.mark.asyncio
    async def test_file_write_system_path_emits_clarification(self):
        it = ClarificationInterceptor()
        ctx = _make_ctx(tool="file_write", args={"path": "/etc/foo.conf", "content": "x"})
        d = await it.check(ctx)
        assert d.intercepted
        ev = d.events[0]
        assert ev.kind == "clarification_needed"
        assert ev.payload["tool"] == "file_write"
        assert len(ev.payload["steps"]) == 3

    @pytest.mark.asyncio
    async def test_service_manage_sshd_stop_emits_clarification(self):
        it = ClarificationInterceptor()
        ctx = _make_ctx(tool="service_manage", args={"action": "stop", "name": "sshd"})
        d = await it.check(ctx)
        assert d.intercepted
        assert d.events[0].kind == "clarification_needed"

    @pytest.mark.asyncio
    async def test_extended_tool_release_after_answer(self):
        it = ClarificationInterceptor()
        ctx = _make_ctx(tool="file_write", args={"path": "/etc/foo.conf", "content": "x"})
        d1 = await it.check(ctx)
        assert d1.intercepted
        record_clarification_answers(ctx.run_id, {"0": {"choice": "A"}})
        ctx2 = _make_ctx(tool="file_write", args={"path": "/etc/foo.conf", "content": "x"})
        ctx2.run_id = ctx.run_id
        d2 = await it.check(ctx2)
        assert not d2.intercepted
        assert any(e.kind == "clarification_done" for e in d2.events)
