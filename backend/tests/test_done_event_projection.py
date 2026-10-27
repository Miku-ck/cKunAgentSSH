"""done 事件的投影对账：生产者写了、落库方读了、中间投影层漏了。

背景（实测缺陷）：driver 的 done 载荷里带着 `context_summary`（spec §8 写明「随 done
出栈，由 routes 落到 agent_runs 行，否则摘要随 ctx 一起消亡」），routes 也确实在读
（`routes/agent.py:979` 落库、`:1077` 喂 episodic job）。而 `SSEAdapter.to_sse_dict`
的 done 分支是**白名单**投影（只挑 state/iterations/state_history/cost/run_id），
补字段时没跟着补 —— 于是这条链两头都在、中间断了：`agent_runs.context_summary`
恒为 NULL，跨 run 的滚动摘要永不复用，轨迹回放拿不到摘要，全程不报错也不告警。

本守卫不钉死某一条数据，而是钉「路由读的键 ⊆ 投影产出的键」这个形状，并要求
生产者确实把该键写进了 done 载荷。任何一侧新增/改名而中间没跟上都会红。

运行: python -m pytest backend/tests/test_done_event_projection.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from agent.harness.contracts import TraceEvent
from agent.harness.telemetry import SSEAdapter

BACKEND = Path(__file__).resolve().parents[1]
DRIVER = BACKEND / "agent" / "harness" / "driver.py"
ROUTES = BACKEND / "routes" / "agent.py"
TELEMETRY = BACKEND / "agent" / "harness" / "telemetry.py"


def _driver_done_keys() -> set[str]:
    """driver 里 done 载荷字典的键集合（按字面量键认，不按名字认）"""
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        # done 载荷的形状特征：同时带 state / state_history / cost
        if {"state", "state_history", "cost"} <= set(keys):
            return set(keys)
    return set()


def _routes_done_read_keys() -> set[str]:
    """routes 在 done 分支里读的 event.get("…") 键"""
    text = ROUTES.read_text(encoding="utf-8")
    m = re.search(r'^(\s*)elif event\.get\("type"\) == "done":', text, re.M)
    assert m, "没找到 routes 的 done 分支（改动前先确认这段还在）"
    indent = len(m.group(1))
    tail = text[m.end():]
    stop = re.search(r'^' + " " * indent + r'(elif |if |return )', tail, re.M)
    block = tail[: stop.start()] if stop else tail
    return {k for k in re.findall(r'event\.get\("([a-z_][a-z0-9_]*)"', block) if k != "type"}


def _adapter_done_keys() -> set[str]:
    """投影层在 done 分支实际写出的键"""
    tree = ast.parse(TELEMETRY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "kind"
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "done"):
            continue
        keys = set()
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            for t in stmt.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)):
                    keys.add(t.slice.value)
        return keys
    return set()


def test_guard_inputs_are_actually_extracted():
    """三个集合都必须非空 —— 抽不到东西的守卫比没有守卫更糟"""
    assert _driver_done_keys(), "driver 的 done 载荷没解析出来"
    assert _routes_done_read_keys(), "routes 的 done 分支没读到任何 event.get"
    assert _adapter_done_keys(), "投影层的 done 分支没解析出来"


def test_routes_never_read_a_key_the_projection_drops():
    """核心不变量：路由在 done 分支读的每个键，投影层都必须真的产出"""
    produced = _adapter_done_keys()
    read = _routes_done_read_keys()
    missing = read - produced
    assert not missing, (
        f"routes 的 done 分支读 {sorted(missing)}，但 SSEAdapter 的 done 投影不透传 —— "
        "这些字段会静默变成 None/\"\"，落库列恒空、下游消费者无从察觉"
    )


def test_producer_writes_what_the_route_reads():
    """另一侧：路由读的键必须真有生产者写它，否则读的是空气"""
    produced_by_driver = _driver_done_keys()
    read = _routes_done_read_keys()
    unproduced = read - produced_by_driver
    assert not unproduced, f"routes 读 {sorted(unproduced)}，driver 的 done 载荷里没有这些键"


def test_context_summary_survives_the_projection():
    """回归钉：这条链曾经断在中间，摘要永远落不了库"""
    ev = TraceEvent(kind="done", source="driver", run_id="run-1",
                    payload={"state": "completed", "iterations": 3, "state_history": [],
                             "cost": None, "run_id": "run-1",
                             "context_summary": "第一轮排查了 nginx，第二轮改了 systemd 单元"})
    out = SSEAdapter.to_sse_dict(ev)
    assert out["context_summary"] == "第一轮排查了 nginx，第二轮改了 systemd 单元"


def test_undo_recorded_survives_the_projection():
    """回归钉（功能 B）：driver 的 done 载荷带 undo_recorded（undo 栈快照是否成功
    落库，driver.py done 分支写入），白名单投影必须透传 —— 断在中间时 SSE 线上
    恒缺失，前端无从知晓回滚是否已记录。"""
    assert "undo_recorded" in _driver_done_keys(), "driver 的 done 载荷没有 undo_recorded"
    ev = TraceEvent(kind="done", source="driver", run_id="run-1",
                    payload={"state": "completed", "iterations": 3, "state_history": [],
                             "cost": None, "run_id": "run-1", "undo_recorded": True})
    out = SSEAdapter.to_sse_dict(ev)
    assert out["undo_recorded"] is True


def _routes_update_agent_run_kwargs() -> set[str]:
    """routes 里 update_agent_run 收到的关键字形参集合（全文件并集）。

    覆盖两种调用形态：直接调用 db.update_agent_run(...)，以及本仓惯用的
    asyncio.to_thread(db.update_agent_run, run_id=..., ...) —— 后者的关键字
    在 AST 上属于 to_thread 调用，由 to_thread 转发给目标函数。
    """
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    names: set[str] = set()

    def _is_target(fn: ast.expr) -> bool:
        return isinstance(fn, ast.Attribute) and fn.attr == "update_agent_run"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_target(node.func):
            names |= {kw.arg for kw in node.keywords if kw.arg}
        elif (isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
              and node.args and _is_target(node.args[0])):
            names |= {kw.arg for kw in node.keywords if kw.arg}
    return names


def test_undo_recorded_reaches_the_runs_table():
    """消费侧闭环（功能 B 收尾）：done 投影透出的 undo_recorded 必须被 routes
    落进 agent_runs（update_agent_run 形参 + 列表投影列），否则通道修了一半
    —— SSE 线上有、库里恒 NULL，运行列表的「回滚」标记无从谈起。"""
    kwargs = _routes_update_agent_run_kwargs()
    assert "undo_recorded" in kwargs, "routes 没把 undo_recorded 传给 update_agent_run，落库链仍断"
    db_text = (BACKEND / "database.py").read_text(encoding="utf-8")
    assert re.search(r"_RUN_LIST_COLUMNS = \([^)]*undo_recorded", db_text, re.S), (
        "运行列表投影 _RUN_LIST_COLUMNS 缺 undo_recorded 列，前端标记恒 undefined"
    )


def test_projection_actually_catches_a_dropped_key():
    """变异自检：把键从投影里摘掉，上面的对账必须产出差集（否则守卫是假的）"""
    produced = _adapter_done_keys()
    read = _routes_done_read_keys()
    assert "context_summary" in produced & read
    doctored = produced - {"context_summary"}
    assert read - doctored == {"context_summary"}


@pytest.mark.parametrize("key", sorted(_routes_done_read_keys()))
def test_each_read_key_is_projected(key: str):
    assert key in _adapter_done_keys(), f"done 分支读了 {key} 却不透传"
