# -*- coding: utf-8 -*-
"""审批卡片可读性改造回归：中文后果说明 + 上下文信息端到端透传。

覆盖三层：
  FilesystemGuard 的 permission_needed payload 带 explain/environment/task/
  ssh_session_id/command（卡片可读性）
  SSEAdapter.to_sse_dict 透传这些字段 + permission_explain 事件映射
  message / 审计 result 仍保持原英文 reason（历史审计数据一致性）

运行: python -m pytest backend/tests/test_approval_explain_card.py -v
"""
import asyncio
import time

import pytest

import agent.harness.command_explain as explain_mod
import agent.harness.reins.guards as _guards_mod
from agent.harness.command_explain import sanitize_llm_explanation
from agent.harness.contracts import AgentConfig, RunContext
from agent.harness.reins import FilesystemGuard, PermissionGate
from agent.harness.telemetry import SSEAdapter, TraceEvent

UNKNOWN_CMD = "mycohortool --deploy"  # 未收录命令 → needs_llm，走解释兜底
CMD = "rm -rf ~/build/out"  # 主目录下递归删除 → dangerous（走审批路径）
# 注：/var、/home 等系统敏感路径下的递归删除是 hardline（无条件阻断、不弹卡片），
# 这里必须用非敏感路径才能命中 dangerous 审批分支


@pytest.fixture(autouse=True)
def _no_file_io(monkeypatch):
    async def _noop_audit(**kw):
        return None
    monkeypatch.setattr(_guards_mod, "append_record_async", _noop_audit)


@pytest.fixture(autouse=True)
def _reset_explain_cache():
    """LLM 解释缓存是进程级模块状态，用例间必须隔离"""
    explain_mod._explain_cache.clear()
    explain_mod._explain_inflight.clear()
    yield
    explain_mod._explain_cache.clear()
    explain_mod._explain_inflight.clear()


def _ctx(task: str = "把项目旧的构建产物清掉，重新出一版包") -> RunContext:
    ctx = RunContext.new(
        task=task, mode="ssh", config=AgentConfig(),
        environment="test", ssh_session_id="sess-abc-123",
    )
    ctx.tool_calls_log.append({
        "tool": "shell_exec",
        "args": {"command": CMD},
        "iteration": 0,
        "success": None,
    })
    return ctx


def _guard() -> FilesystemGuard:
    gate = PermissionGate()
    gate._confirmed_permanent = set()
    return FilesystemGuard(permission_gate=gate)


def _card_payload(ctx: RunContext) -> dict:
    decision = asyncio.run(_guard().check(ctx))
    assert decision.intercepted, "dangerous 命令必须仍被拦截待审批"
    assert len(decision.events) == 1
    ev = decision.events[0]
    assert ev.kind == "permission_needed"
    return ev.payload


# ── 后端 payload ─────────────────────────────────────────────────────────────
def test_card_carries_chinese_explanation():
    payload = _card_payload(_ctx())
    assert payload["explain"], "explain 不能为空，否则小白看到的仍是英文 reason"
    assert "递归删除" in payload["explain"]
    assert "~/build/out" in payload["explain"], "后果说明必须点出被删的对象"
    assert payload["explain_source"] == "static"


def test_card_carries_user_context():
    payload = _card_payload(_ctx())
    assert payload["environment"] == "test"
    assert payload["ssh_session_id"] == "sess-abc-123"
    assert payload["command"] == CMD
    assert payload["task"] == "把项目旧的构建产物清掉，重新出一版包"


def test_task_truncated_for_card():
    payload = _card_payload(_ctx(task="x" * 500))
    assert len(payload["task"]) == 200


def test_english_reason_still_in_message_for_audit():
    """message 保留英文 reason：审计链与既有排查习惯依赖它，不能被中文文案替换"""
    payload = _card_payload(_ctx())
    assert "recursive delete under home directory" in payload["message"]


def test_unknown_command_explain_marks_llm_path():
    """未收录命令：卡片有中文兜底，同时告诉调用方值得补一次 LLM 解释"""
    ctx = _ctx()
    ctx.tool_calls_log[-1]["args"] = {"command": "mycohortool --deploy"}
    payload = _card_payload(ctx)
    assert payload["explain_source"] == "static"
    assert "mycohortool" in payload["explain"]


# ── SSE 透传 ─────────────────────────────────────────────────────────────────
def test_sse_permission_expired_passes_command():
    """审批超时事件必须带 command：前端 permCardKey(tool, command) 靠它定位卡片。

    回归点：to_sse_dict 曾丢弃 command（两个生产方都按注释带着它），超时事件
    永远匹配不到卡片 → 后端已按拒绝处理，卡片却仍可点击。
    """
    sse = SSEAdapter.to_sse_dict(TraceEvent(
        kind="permission_expired",
        source="reins.permission_gate",
        run_id="r1",
        payload={
            "tool": "shell_exec",
            "args": {"command": CMD},
            "command": CMD,
            "message": "授权审批超时，已按拒绝处理: shell_exec",
        },
    ))
    assert sse.get("command") == CMD, "缺 command → 前端无法定位超时卡片"
    assert sse.get("tool") == "shell_exec"


def test_sse_permission_needed_passes_display_fields():
    sse = SSEAdapter.to_sse_dict(TraceEvent(
        kind="permission_needed",
        source="reins.filesystem_guard",
        run_id="r1",
        payload={
            "tool": "shell_exec",
            "args": {"command": CMD},
            "risk": "dangerous",
            "reversible": False,
            "message": "需要用户授权: shell_exec — recursive delete of absolute path",
            "normalized_command": CMD,
            "explain": "递归删除该路径下的全部文件和子目录（/var/log/app）。",
            "explain_source": "static",
            "environment": "prod",
            "task": "清理日志",
            "ssh_session_id": "sess-abc-123",
            "command": CMD,
        },
    ))
    assert sse["type"] == "permission_needed"
    for key in ("explain", "explain_source", "environment", "task", "ssh_session_id", "command"):
        assert key in sse and sse[key], f"{key} 未透传到前端"


def test_sse_permission_explain_event():
    sse = SSEAdapter.to_sse_dict(TraceEvent(
        kind="permission_explain",
        source="reins.filesystem_guard",
        run_id="r1",
        payload={
            "tool": "shell_exec",
            "command": "mycohortool --deploy",
            "normalized_command": "mycohortool --deploy",
            "explanation": "运行某个自定义部署脚本。",
        },
    ))
    assert sse["type"] == "permission_explain"
    assert sse["explanation"] == "运行某个自定义部署脚本。"
    assert sse["command"] == "mycohortool --deploy"


# ── LLM 兜底：不阻塞审批、成功回填、失败静默 ──────────────────────────────────
def _unknown_cmd_ctx(**kw) -> RunContext:
    ctx = _ctx(**kw)
    ctx.tool_calls_log[-1]["args"] = {"command": UNKNOWN_CMD}
    # 有模型配置才可能走 LLM 兜底
    ctx.llm_base_url, ctx.llm_api_key, ctx.llm_model = "http://llm.test", "k", "m"
    return ctx


def _run_unknown_cmd(monkeypatch, llm_impl, settle: float = 0.2):
    """跑一次 dangerous 审批 + 后台解释，返回 (decision, 审批耗时, 已投递事件)"""
    emitted: list = []

    async def emit(ev) -> None:
        emitted.append(ev)

    async def main():
        ctx = _unknown_cmd_ctx()
        ctx.metadata["emit_event"] = emit
        monkeypatch.setattr(explain_mod, "_one_shot_llm", llm_impl)
        start = time.monotonic()
        decision = await _guard().check(ctx)
        elapsed = time.monotonic() - start
        await asyncio.sleep(settle)
        return decision, elapsed

    decision, elapsed = asyncio.run(main())
    return decision, elapsed, emitted


async def _slow_llm(messages, ctx):
    await asyncio.sleep(10)
    return "永远不会到达的解释"


async def _ok_llm(messages, ctx):
    return "运行自定义的部署脚本，会把当前项目发布到目标机器上。\n"


async def _boom_llm(messages, ctx):
    raise RuntimeError("模型不可用")


def test_llm_explain_does_not_block_approval(monkeypatch):
    """慢模型不能吃掉用户的 120s 审批窗口：卡片必须先弹出"""
    decision, elapsed, _ = _run_unknown_cmd(monkeypatch, _slow_llm, settle=0.05)
    assert decision.intercepted
    assert elapsed < 1.0, f"审批路径被 LLM 阻塞：{elapsed:.2f}s"


def test_llm_explain_backfills_after_card(monkeypatch):
    """成功路径：卡片事件先出，解释事件后到，command 与卡片 args.command 一致"""
    decision, elapsed, emitted = _run_unknown_cmd(monkeypatch, _ok_llm)
    card = [e for e in decision.events if e.kind == "permission_needed"]
    assert card and card[0].payload["explain_source"] == "static"
    explains = [e for e in emitted if e.kind == "permission_explain"]
    assert len(explains) == 1
    payload = explains[0].payload
    assert payload["command"] == UNKNOWN_CMD, "回填键必须能对上已弹出的卡片"
    assert "部署脚本" in payload["explanation"]
    assert payload["explanation"] == payload["explanation"].strip()
    assert elapsed < 1.0


def test_llm_explain_failure_keeps_static_and_no_retry(monkeypatch):
    """失败：不投递事件、命令仍按原链路拦截，且同命令不再重试（防刷屏）"""
    calls = []

    async def counting_llm(messages, ctx):
        calls.append(1)
        return await _boom_llm(messages, ctx)

    decision, _, emitted = _run_unknown_cmd(monkeypatch, counting_llm)
    assert decision.intercepted and decision.events
    assert not [e for e in emitted if e.kind == "permission_explain"]
    assert len(calls) == 1

    # 第二次审批同一命令：命中"已失败"缓存，不再调用模型
    ctx2 = _unknown_cmd_ctx()
    ctx2.metadata["emit_event"] = lambda ev: asyncio.sleep(0)
    asyncio.run(_guard().check(ctx2))
    assert len(calls) == 1


def test_llm_explain_disabled_switch(monkeypatch):
    """kill switch 生效：退回纯静态，不调用模型"""
    called = []

    async def spy(messages, ctx):
        called.append(1)
        return "不该被使用"

    async def emit(ev) -> None:
        return None

    async def main():
        ctx = _unknown_cmd_ctx()
        ctx.metadata["emit_event"] = emit
        monkeypatch.setattr(explain_mod, "_one_shot_llm", spy)
        monkeypatch.setattr(explain_mod, "LLM_EXPLAIN_ENABLED", False)
        return await _guard().check(ctx)

    decision = asyncio.run(main())
    assert decision.intercepted, "关闭解释开关不能影响拦截"
    assert not called


def test_cached_explain_emits_without_llm_call(monkeypatch):
    """同命令再次审批：直接用缓存投递事件，不再打模型"""
    key = explain_mod.explain_cache_key("shell_exec", UNKNOWN_CMD)
    explain_mod._explain_cache[key] = "缓存里的解释。"

    async def never(messages, ctx):
        raise AssertionError("命中缓存不应调用模型")

    decision, _, emitted = _run_unknown_cmd(monkeypatch, never)
    explains = [e for e in emitted if e.kind == "permission_explain"]
    assert len(explains) == 1 and explains[0].payload["explanation"] == "缓存里的解释。"


def test_no_model_config_skips_llm(monkeypatch):
    """未配模型（base_url 为空）：跳过兜底，卡片仍是静态文案"""
    async def never(messages, ctx):
        raise AssertionError("无模型配置时不应调用模型")

    async def emit(ev) -> None:
        return None

    async def main():
        ctx = _unknown_cmd_ctx()
        ctx.llm_base_url = ""
        ctx.metadata["emit_event"] = emit
        monkeypatch.setattr(explain_mod, "_one_shot_llm", never)
        return await _guard().check(ctx)

    assert (asyncio.run(main())).intercepted


# ── 模型输出校验 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "I cannot determine what this command does.",       # 非中文
    "无法确定该命令的作用",                              # 模型承认不确定
    "这是一句超过八十个字的解释" * 12,                   # 过长
])
def test_rejects_unusable_explanations(raw):
    assert sanitize_llm_explanation(raw) is None


def test_accepts_single_line_chinese():
    assert sanitize_llm_explanation(" 运行部署脚本，\n会把项目发布到目标机。  ") == \
        "运行部署脚本， 会把项目发布到目标机。"
