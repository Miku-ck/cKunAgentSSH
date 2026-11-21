"""init_db 启动失败必须留下可诊断的项目级 error（含堆栈）并原样上抛。

回归闸门：init_db 是 lifespan 里唯一不在 gather(return_exceptions=True) 内的
步骤。曾在它外面裸调用，失败时项目自身零日志，只能靠 Electron 的 stderr 尾部
超时兜底，现场只有「进程退出」而没有根因。本测试钉住两条不变量：

1. 失败时 logger.error 带 exc_info=True（堆栈在 stderr / caplog 可查）；
2. 异常必须 re-raise —— 吞掉会让后端带坏库继续跑（属行为变更）。

注意：本测试只验日志与异常传播，绝不触碰 database.py 的建表/迁移/数据修复逻辑；
结构指纹另有 test_startup_critical_path.py 守卫。
"""
import asyncio
import logging

import pytest

import main


def test_init_db_failure_logs_error_with_traceback_and_reraises(monkeypatch, caplog):
    """init_db 抛异常 → 一条 ERROR + traceback 落日志，且异常原样上抛。"""
    boom = RuntimeError("disk I/O error on init_db")

    def _raise(_db_path):
        raise boom

    monkeypatch.setattr(main.db, "init_db", _raise)

    async def _run():
        async with main.lifespan(None):
            pass

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(_run())

    # 1. 异常原样上抛（没被吞）
    assert exc_info.value is boom

    # 2. 一条明确的 ERROR，且带 traceback（exc_info=True 的效果）
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("数据库初始化失败" in r.getMessage() for r in records), (
        "缺少 init_db 失败的 error 日志，现场将无根因可查"
    )
    hit = next(r for r in records if "数据库初始化失败" in r.getMessage())
    assert hit.exc_info is not None, "error 日志未带 exc_info，堆栈丢失"
    assert hit.exc_info[0] is RuntimeError, "堆栈未指向真实异常类型"
