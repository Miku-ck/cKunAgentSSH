# -*- coding: utf-8 -*-
"""S2-04 回归 — 每条隶属某个 run 的审批决策必须在审计记录里带上 run_id。

docs/security-boundary.md §6 声明审计链可追「谁在哪个 run 里批了什么」，而
routes/agent.py 的八处 append_record_async 原先**一处都没传** run_id：列有默认值
""，所以不是报错，只是永远为空 —— 事后能证明「批过 foo」，证不了批的是哪次运行。
撤销与手动登记永久授权那两条按设计不属于任何 run（它们改的是跨 run 的 allowlist），
run_id 留空才是正确取值。
"""
import asyncio
import re
from pathlib import Path

import pytest

ROUTE_SRC = (Path(__file__).resolve().parents[1] / "routes" / "agent.py").read_text(
    encoding="utf-8")

# 每条隶属某个 run 的决策动作 → 必须能在同一条 append_record_async 调用里看到 run_id
RUN_BOUND_ACTIONS = (
    "permission_grant",
    "permission_deny_command",
    "permission_release_run",
    "clarification_answer",
    "clarification_skip",
)
# 按设计无 run 的两条：不该出现 run_id，也不该被当成漏传
RUN_LESS_ACTIONS = ("permission_revoke", "permission_allow_manual")


def _call_blocks() -> list[str]:
    """按本文件既有排版切出每个 append_record_async(...) 调用体。"""
    return re.findall(r"append_record_async\((.*?)\n    \)", ROUTE_SRC, re.S)


def test_every_run_bound_audit_carries_run_id():
    blocks = _call_blocks()
    assert blocks, "解析不到任何 append_record_async 调用点，守卫本身失效了"
    for action in RUN_BOUND_ACTIONS:
        mine = [b for b in blocks if action in b]
        assert mine, f"找不到 action={action} 的审计调用点（被删了？改名了？）"
        for b in mine:
            assert "run_id=" in b, (
                f"{action} 隶属某个 run 却没把 run_id 交给审计列 —— "
                f"§6 的「可追到 run」会变成一句假话：\n{b}")


def test_run_less_decisions_are_explicitly_marked():
    """无 run 是设计而非遗漏 —— 留下注释，免得下一轮又被当成同一条 bug 翻出来。"""
    blocks = _call_blocks()
    for action in RUN_LESS_ACTIONS:
        mine = [b for b in blocks if action in b]
        assert mine, f"找不到 action={action} 的审计调用点"
        joined = "\n".join(mine)
        assert "run_id=" not in joined, f"{action} 不该带 run_id"
        assert "S2-04" in joined, f"{action} 需要一句「按设计无 run」的说明"


@pytest.fixture
def recorder(monkeypatch):
    """截下路由交给审计层的 kwargs —— 只验「有没有把 run_id 传下去」。"""
    import routes.agent as ra

    seen: list[dict] = []

    async def _fake(**kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(ra, "append_record_async", _fake)
    return seen


def test_skip_clarification_records_its_run(recorder):
    import routes.agent as ra
    req = ra.ClarificationAnswerRequest(run_id="run-skip-42", answers={})
    asyncio.run(ra.skip_clarification(req))
    assert recorder, "skip 没落审计"
    assert recorder[-1]["run_id"] == "run-skip-42"
    assert recorder[-1]["conclusion"] == "skipped"


def test_answer_clarification_records_its_run(recorder):
    import routes.agent as ra
    req = ra.ClarificationAnswerRequest(
        run_id="run-ans-7", answers={"0": {"choice": "A"}})
    asyncio.run(ra.submit_clarification_answer(req))
    assert recorder[-1]["run_id"] == "run-ans-7"
    assert recorder[-1]["conclusion"] == "answered"


def test_release_run_records_the_path_run_id():
    """release-run 的 run_id 来自路径参数，同样必须进列（源码守卫，理由同上）。"""
    rel = [b for b in _call_blocks() if "permission_release_run" in b]
    assert rel and all("run_id=run_id" in b for b in rel)


def test_deny_no_longer_buries_run_in_free_text():
    """run 曾是 result 里的一句 `run=...` 自由文本；它现在是真列。

    留在文本里会变成两个真相来源（审计页按 run_id 列筛选，看不见 result 里那句）。
    这里走源码守卫而不是直接调路由：deny_command 要构造进程级 PermissionGate，
    在测试宿主里会卡在 gate 的初始化上（实测整进程挂死），不值得为它付这个代价。
    """
    deny = [b for b in _call_blocks() if "permission_deny_command" in b]
    assert deny, "找不到 deny 的审计调用点"
    b = deny[0]
    assert "run_id=req.run_id" in b
    assert "run=" not in b, "run 不该同时存在于 result 自由文本里"
