"""运维专家建议谓词守卫：触发/不触发边界 + 只读运维覆盖。

关键词级规则（复用变更动词表 + 运维名词小词表），不是新分类器。
证伪：让失败路径抛异常 → 调用方静默 no-op 守卫红（见路由侧）。
"""
import pytest

from agent.expert_suggest import should_suggest_ops


@pytest.mark.parametrize("task", [
    "重启 nginx",          # 变更动词
    "看下磁盘空间",        # 只读运维（名词命中，变更动词不命中）
    "查一下端口占用",
    "docker 容器挂了",
    "清理日志",
])
def test_ops_signal_triggers(task):
    assert should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_selected_expert_suppresses():
    assert not should_suggest_ops("重启 nginx", expert_selected=True, is_conversation=False)


def test_conversation_suppresses():
    assert not should_suggest_ops("你好", expert_selected=False, is_conversation=True)


@pytest.mark.parametrize("task", ["写一首诗", "帮我写周报", "翻译这段英文", ""])
def test_non_ops_task_does_not_trigger(task):
    assert not should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_none_task_safe():
    assert not should_suggest_ops(None, expert_selected=False, is_conversation=False)


# ── 路由侧守卫（spec §8「建议信号」）─────────────────────────────────────────
# 上面的谓词单测只钉 should_suggest_ops 的真值表；真正的发射点在 routes/agent.py
# 的 event_stream 闭包里：未选专家 + 非对话 + 运维信号 → yield expert_suggestion，
# 判定失败由 try/except 静默 no-op（绝不拖 run）。该闭包深埋在 agent_run 数百行装配
# 之后，无法轻量直驱，故按项目既有 AST 源守卫惯例（test_plan_mode.py 钉 agent.py 的
# 成对写点、test_trace_projection_keys.py 扫发射 kind）把真实路由的「载荷 + no-op
# 包裹」钉死，再配两条行为仿真说明意图。
# 证伪：把路由的 except Exception 改窄 / 删掉 try / 在 except 里 re-raise / 漏载荷键
#       → AST 守卫红；谓词抛异常穿透包裹 → 行为守卫红。

_SUGGESTION_KEYS = {"type", "slug", "name", "reason", "task", "run_id"}


def _suggestion_try():
    """定位 routes/agent.py 里发 expert_suggestion 的 try 块（路由 no-op 守卫的锚点）。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "routes/agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Try)
        and any(isinstance(c, ast.Constant) and c.value == "expert_suggestion"
                for c in ast.walk(n))
    ]
    assert len(hits) == 1, f"expert_suggestion 发射块应唯一，实际 {len(hits)} 处"
    return hits[0]


def test_route_emits_suggestion_for_ops_task():
    """路由侧（行为）：运维任务 + 未选专家 → expert_suggestion，载荷取自内置运维包。"""
    from agent.expert_policy import builtin_general_ops

    task = "重启 nginx"
    assert should_suggest_ops(task, expert_selected=False, is_conversation=False)
    ops = builtin_general_ops()
    # 与 routes/agent.py 发射的载荷逐键同构（键集合由下面的 AST 守卫钉在真实路由上）
    event = {
        "type": "expert_suggestion",
        "slug": ops["slug"],
        "name": ops["name"],
        "reason": "检测到运维任务，建议切到运维专家",
        "task": task,
        "run_id": "test-run",
    }
    assert event["type"] == "expert_suggestion"
    assert event["slug"] == "general_ops"
    assert event["name"] == "运维专家"
    assert event["task"] == task
    assert event["run_id"] == "test-run"
    assert event["reason"]


def test_route_suggestion_failure_is_noop(monkeypatch):
    """路由侧（行为）：判定抛异常 → 静默 no-op，不发事件、不把异常拖回 run。"""
    import agent.expert_suggest as es

    def _boom(*a, **k):
        raise RuntimeError("simulated suggestion failure")

    monkeypatch.setattr(es, "should_suggest_ops", _boom)

    events: list[str] = []
    # 复刻路由的 try/except 包裹；真实路由的同一结构由下面的 AST 守卫钉住。
    try:
        if es.should_suggest_ops("重启 nginx", expert_selected=False, is_conversation=False):
            events.append("expert_suggestion")
    except Exception:
        pass  # 路由在此 logger.warning，run 继续以通用身份跑
    assert events == [], "判定失败必须 no-op：不得发事件，也不得让异常穿透"


def test_route_suggestion_block_is_noop_guarded():
    """路由侧（源守卫）：expert_suggestion 发射必须在 try/except Exception 内，
    except 走 logger.warning 且不 re-raise；谓词/运维包调用与载荷键齐全。

    这条把上面的行为仿真从「自证」升级为「真钉路由」：删掉 try/except、改窄捕获
    类型、在 except 里 re-raise、或漏发载荷键，都会红。
    """
    import ast

    node = _suggestion_try()

    # 1) 必须被 except Exception 捕获（不是 BaseException，也不是漏捕获/窄捕获）
    assert node.handlers, "expert_suggestion 发射必须包在 try 里"
    caught = {h.type.id for h in node.handlers if isinstance(h.type, ast.Name)}
    assert caught == {"Exception"}, f"判定失败须由 except Exception 静默吃掉，实际 {caught}"
    handler = node.handlers[0]

    # 2) except 体走 logger.warning，且绝不 re-raise（no-op 不拖 run）
    warns = [
        n for n in ast.walk(handler)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "warning"
    ]
    assert warns, "except 分支应 logger.warning 记一笔后跳过"
    assert not any(isinstance(n, ast.Raise) for n in ast.walk(handler)), \
        "no-op 守卫不得 re-raise，否则判定失败会拖垮整个 run"

    # 3) try 体真的调了谓词与内置运维包（不是无条件发死事件）
    body_nodes = [c for stmt in node.body for c in ast.walk(stmt)]
    called = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in body_nodes
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert "should_suggest_ops" in called, "发射前必须过 should_suggest_ops 判定"
    assert "builtin_general_ops" in called, "载荷必须取自内置运维包"

    # 4) 发射载荷键齐全（前端 chip 靠 slug/name 切换、task/run_id 归因）
    dicts = [
        d for d in body_nodes if isinstance(d, ast.Dict)
        and any(isinstance(v, ast.Constant) and v.value == "expert_suggestion"
                for v in d.values)
    ]
    assert dicts, "找不到 expert_suggestion 载荷字典"
    keys = {k.value for k in dicts[0].keys if isinstance(k, ast.Constant)}
    assert keys == _SUGGESTION_KEYS, f"载荷键漂移：{keys ^ _SUGGESTION_KEYS}"