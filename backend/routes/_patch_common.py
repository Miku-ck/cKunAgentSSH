"""schedules / runbooks 两条「立即执行」端点共用的 PATCH 与触发前置守卫。

此前 routes/schedules.py 与 routes/runbooks.py 各抄了一份 `_drop_nulls` 和一段
桥探活 503：状态码、中文 detail 逐字相同，拆成两份副本等于给「改一边忘另一边」
留门。收口到同一处后，两路由的响应形态由构造保证一致。
"""
from __future__ import annotations

from fastapi import HTTPException


def drop_nulls(patch: dict) -> dict:
    """PATCH 中显式 null 与「未提供」等价，一律丢弃。

    这些字段在库里 NOT NULL 且参与 compute_next_run/_validate_fields 的 int()，
    原样透传会让 {"interval_minutes": null} 变成 int(None) 的 500，
    或把 NULL 写进调度列导致任务永不触发。
    """
    return {k: v for k, v in patch.items() if v is not None}


async def require_bridge_or_503() -> None:
    """配了 target_host 的触发前先探桥；断桥就地 503，不启动注定失败的无头 run。

    ElectronBridge 刻意留在函数体内惰性导入：提到模块级会把它带进 `import main`
    的导入图，破坏 tests/test_startup_critical_path.py 守住的启动关键路径。
    """
    from agent.electron_bridge import ElectronBridge

    bridge_err = await ElectronBridge().probe()
    if bridge_err is not None:
        raise HTTPException(
            status_code=503,
            detail=f"Electron 桥当前不可达（{bridge_err.value}），本次未触发；"
                   "请确认主进程在运行后重试",
        )
