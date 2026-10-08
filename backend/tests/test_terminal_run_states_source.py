# -*- coding: utf-8 -*-
"""run 终态集合只有一个来源，三处消费者不许各抄一份。

同一份「哪些 status 算终态」原本在三个地方各写字面量：
  - `database.update_agent_run` 的 `_TERMINAL_RUN_STATUSES` —— 决定要不要写 completed_at
  - `routes/agent.py` 释放陈旧 run 占位的 `stale = ... in (...)` —— 决定这条会话
    能不能再起一个 run（漏掉 human_help/max_reached 时，这两种结束的 run 的内存
    占位永不自愈，用户看到的是「已有运行中的 Agent 任务」直到重启）
  - `agent/reflection.py` 的 `_TERMINAL_STATES` —— 状态转移异常检测的判据

第三份确实漂了：它漏了 `human_help`，而 `State.HUMAN_HELP` 的枚举注释明写着
「终止态」。后果是一条以人工介入结束的 run，之后若再出现
`human_help → acting` 这种「终态后又跑起来」的异常转移，反思侧一条都报不出来。

本文件钉的是**形状**（集合等于共享源、消费者不再手抄），不是某一条判定结果，
所以以后加第五个终态时漏改任何一处都会红。

运行: python -m pytest backend/tests/test_terminal_run_states_source.py -v
"""
import ast
import re
from pathlib import Path

from agent.harness.contracts import State, TERMINAL_RUN_STATES

BACKEND = Path(__file__).resolve().parents[1]


def test_shared_set_covers_every_declared_terminal_state():
    """共享源本身必须完整：枚举里标了终止态的，集合里就得有"""
    assert TERMINAL_RUN_STATES == frozenset({
        "completed", "failed", "max_reached", "human_help",
    })
    # 集合成员是**裸字符串值**而不是枚举成员 —— str-Enum 的哈希语义不保证与
    # 裸字符串一致，而三处消费者比对的都是从 JSON/DB 读出的字符串
    assert all(isinstance(x, str) and not isinstance(x, State) for x in TERMINAL_RUN_STATES)


def _owner_with_attr(mod, attr: str):
    """在模块的类里找把某属性定义在自己 __dict__ 上的那一个"""
    for cls in vars(mod).values():
        if isinstance(cls, type) and attr in vars(cls):
            return cls
    return None


def test_reflection_uses_the_shared_set():
    """反思侧不得再有第二个清单（漂移已经发生过一次）"""
    import agent.reflection as reflection
    cls = _owner_with_attr(reflection, "_TERMINAL_STATES")
    assert cls is not None, "reflection 里找不到 _TERMINAL_STATES"
    value = vars(cls)["_TERMINAL_STATES"]
    assert value is TERMINAL_RUN_STATES, (
        f"{cls.__name__}._TERMINAL_STATES 不是共享源，而是 {sorted(value or [])} —— "
        "两份清单迟早分叉（上一次就分在了 human_help 上）"
    )
    assert "human_help" in value


def test_database_terminal_list_matches_shared_source():
    """storage 层保持纯 stdlib leaf，不反向 import harness —— 那就用对账钉住"""
    src = (BACKEND / "database.py").read_text(encoding="utf-8")
    m = re.search(r"_TERMINAL_RUN_STATUSES\s*=\s*\(([^)]*)\)", src)
    assert m, "database.py 里找不到 _TERMINAL_RUN_STATUSES（改动前先确认它还在）"
    literals = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert literals == set(TERMINAL_RUN_STATES), (
        f"database 的终态清单 {sorted(literals)} 与共享源 {sorted(TERMINAL_RUN_STATES)} 不一致"
    )


def test_routes_no_longer_hand_copies_the_list():
    """路由必须用共享符号，不能再出现内联四元组"""
    src = (BACKEND / "routes" / "agent.py").read_text(encoding="utf-8")
    assert "from agent.harness.contracts import TERMINAL_RUN_STATES" in src
    assert not re.search(r'"completed",\s*"failed",\s*"human_help"', src), (
        "routes/agent.py 又抄了一份终态字面量清单"
    )
