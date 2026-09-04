"""
定时任务路由 — 定时任务 CRUD + 运行 + 历史

GET    /api/schedules          — 任务列表
POST   /api/schedules          — 新建任务
PATCH  /api/schedules/{id}     — 更新任务
DELETE /api/schedules/{id}     — 删除任务
POST   /api/schedules/{id}/run — 立即执行
GET    /api/schedules/{id}/runs— 执行历史
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from agent.schedule_engine import get_schedule_engine, compute_next_run

logger = logging.getLogger(__name__)
router = APIRouter(tags=["schedules"])

_SCHEDULE_TYPES = {"interval", "daily", "weekly", "monthly"}


class ScheduleIn(BaseModel):
    name: str = Field(..., min_length=1)
    schedule_type: str = "interval"
    # 审查修复：加范围校验 — 0/负值入库后 compute_next_run 算出 ≤now 的时间，
    # 每 30s tick 都触发无头 SSH Agent 运行，持续消耗模型调用与 SSH 连接
    interval_minutes: int = Field(default=60, ge=1, le=525600)
    time_hour: int = 9
    time_minute: int = 0
    weekday: int = 0
    day_of_month: int = 1
    target_host: str = ""
    task_prompt: str = ""
    enabled: bool = True


class SchedulePatch(BaseModel):
    """PATCH 局部更新：只更新显式传入的字段（exclude_unset）。"""
    name: Optional[str] = None
    schedule_type: Optional[str] = None
    # 审查修复：与 ScheduleIn 一致的范围校验
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=525600)
    time_hour: Optional[int] = None
    time_minute: Optional[int] = None
    weekday: Optional[int] = None
    day_of_month: Optional[int] = None
    target_host: Optional[str] = None
    task_prompt: Optional[str] = None
    enabled: Optional[bool] = None


_SCHEDULE_KEYS = {
    "schedule_type", "interval_minutes", "time_hour", "time_minute",
    "weekday", "day_of_month",
}


@router.get("/schedules")
async def list_schedules():
    return await asyncio.to_thread(db.list_schedules)


@router.post("/schedules")
async def create_schedule(req: ScheduleIn):
    if req.schedule_type not in _SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的周期类型: {req.schedule_type}")
    _validate_fields(req.model_dump())
    next_at = compute_next_run(req.model_dump())
    created = await asyncio.to_thread(
        db.create_schedule, {**req.model_dump(), "next_run_at": next_at}
    )
    get_schedule_engine().ensure_started()
    return created


def _validate_fields(d: dict) -> None:
    st = d.get("schedule_type") or "interval"
    if not (0 <= int(d.get("time_hour", 9)) <= 23):
        raise HTTPException(status_code=400, detail="时必须在 0-23")
    if not (0 <= int(d.get("time_minute", 0)) <= 59):
        raise HTTPException(status_code=400, detail="分必须在 0-59")
    if st == "weekly" and not (0 <= int(d.get("weekday", 0)) <= 6):
        raise HTTPException(status_code=400, detail="星期必须在 0(周一)-6(周日)")
    if st == "monthly" and not (1 <= int(d.get("day_of_month", 1)) <= 28):
        raise HTTPException(status_code=400, detail="每月的日期必须在 1-28")


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, req: SchedulePatch):
    existing = await asyncio.to_thread(db.get_schedule, schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="任务不存在")
    patch = req.model_dump(exclude_unset=True)
    merged = {**existing, **patch}
    if merged.get("schedule_type") not in _SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的周期类型: {merged.get('schedule_type')}")
    _validate_fields(merged)
    updated = await asyncio.to_thread(db.update_schedule, schedule_id, patch)
    # 仅当调度参数发生变化时重算下次执行时间；toggle 启停不重算
    if _SCHEDULE_KEYS & set(patch.keys()):
        next_at = compute_next_run(updated)
        await asyncio.to_thread(db.set_schedule_next_run, schedule_id, next_at)
    return updated


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    ok = await asyncio.to_thread(db.delete_schedule, schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str):
    sched = await asyncio.to_thread(db.get_schedule, schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="任务不存在")
    engine = get_schedule_engine()
    engine.ensure_started()
    await engine.trigger(sched)
    return {"ok": True, "schedule_id": schedule_id}


@router.get("/schedules/{schedule_id}/runs")
async def list_schedule_runs(schedule_id: str, limit: int = 50):
    if not await asyncio.to_thread(db.get_schedule, schedule_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(db.list_schedule_runs, schedule_id, limit)