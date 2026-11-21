"""db/schedules 与 database 双份实现的 parity 守卫 — P0 阶段 3 迁移过渡期。

迁移计划（docs/structure-refactor-plan.md P0）在阶段 3 完成前允许 db/ 包持有
函数体副本，但两份实现必须逐字一致：任何一侧单独改动而另一侧未同步，本文件
当场失败，防止行为漂移。database.py 收敛为再导出壳（迁移终态）后可删本文件。

同时钉住迁移期两条运行时契约：
- database.init_db 必须把库路径同步给 db 包连接池（否则 db.schedules 等新模块
  在生产进程内指向 sqlite3.connect("") 的临时空库，查什么都 no such table）；
- 未初始化时 db 包必须 fail-fast 抛 RuntimeError，而不是静默连临时库。
"""

import ast
import inspect

import pytest

import database as dbmod
import db._conn as db_conn_mod
import db.schedules as db_sched


# ── 1) 源码级 parity：同名函数 AST 必须一致（docstring 属于 AST 的一部分） ──

def _module_top_functions(module) -> dict:
    tree = ast.parse(inspect.getsource(module))
    return {
        node.name: ast.dump(node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_schedules_functions_are_shared():
    """P2-3 收敛后 database.py 不再持有副本：同名符号必须是 db/schedules 的同一对象。

    过渡期的 AST parity（两份实现逐字一致）在收敛为再导出后失去意义 —— 现在要
    防的回归是「有人在 database.py 里再抄一份」。
    """
    for name in db_sched.__all__:
        assert getattr(dbmod, name) is getattr(db_sched, name), (
            f"{name} 不是 db/schedules.py 的再导出（疑似出现了第二份副本）"
        )


def test_all_covers_all_public_functions():
    """新加进 db/schedules 的公共函数必须同步进 __all__，否则 star import
    漏导出（db/__init__.py 靠 __all__ 收口命名空间）。"""
    public = {
        name
        for name, value in vars(db_sched).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", "") == db_sched.__name__
    }
    assert public == set(db_sched.__all__)


# ── 2) 运行时契约：共享连接池（database.init_db 同步路径） ──

def test_db_pkg_crud_on_shared_pool(base_db):
    """base_db 走生产入口 database.init_db —— db 包连接池路径同步后，
    db.schedules 的读写与 database 同库可见（P0-1 的回归钉）。"""
    created = db_sched.create_schedule({"name": "parity", "task_type": "ops"})
    assert created is not None
    sid = created["id"]
    assert dbmod.get_schedule(sid)["name"] == "parity"           # 跨池可见

    midnight = db_sched.create_schedule({"name": "mid", "time_hour": 0})["id"]
    assert dbmod.get_schedule(midnight)["time_hour"] == 0        # 0 点不被 or 吞掉

    db_sched.create_schedule_run(sid, run_id="run-1")
    assert db_sched.update_schedule_run("run-1", status="completed", summary="ok") >= 1
    assert db_sched.update_schedule_run("no-such-key", status="failed") == 0  # 0 行可观测
    assert db_sched.clear_schedule_runs(sid) == 1                # 无 running → 原子清空

    db_sched.create_schedule_run(sid, run_id="run-2")            # running 行
    assert db_sched.clear_schedule_runs(sid) is None             # 原子闸拒绝
    assert len(dbmod.list_schedule_runs(sid, limit=50)) == 1
    assert db_sched.has_running_schedule_run(sid) is True

    assert db_sched.prune_schedule_runs(max_count=0) == 0        # prune 不吃 running 行
    assert db_sched.fail_running_schedule_runs(sid, "parity") == 1
    assert db_sched.prune_schedule_runs(max_count=0) == 1        # 终态行可清
    assert dbmod.get_schedule(sid) is not None                   # 任务本身保留
    assert db_sched.delete_schedule(sid) is True


# ── 3) 运行时契约：未初始化必须 fail-fast ──

def test_uninitialized_pool_fails_fast(monkeypatch):
    monkeypatch.setattr(db_conn_mod, "_db_path", "")
    with pytest.raises(RuntimeError, match="未初始化"):
        db_sched.list_schedules()
