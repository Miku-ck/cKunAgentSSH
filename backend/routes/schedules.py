"""
定时任务路由 — 定时任务 CRUD + 运行 + 历史

GET    /api/schedules          — 任务列表
POST   /api/schedules          — 新建任务
PATCH  /api/schedules/{id}     — 更新任务
DELETE /api/schedules/{id}     — 删除任务
POST   /api/schedules/{id}/run — 立即执行
GET    /api/schedules/{id}/runs— 执行历史
DELETE /api/schedules/{id}/runs— 清空执行历史（保留任务；执行中 409）
"""
import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
from routes._patch_common import drop_nulls, require_bridge_or_503

router = APIRouter(tags=["schedules"])

_SCHEDULE_TYPES = {"interval", "daily", "weekly", "monthly"}

# S7-11：入库上限。名字不写死轮询秒数（这里曾写着「每 3s 一轮」而引擎实际 30s）——
# 数字归 schedule_engine 单管，注释抄一份就是一处会静默失效的副本。
_NAME_MAX_LEN = 100
# 与 AgentRunRequest.task（routes/agent.py:367，同为喂给无头 run 的提示词）同一口径
_TASK_PROMPT_MAX_LEN = 50_000


class ScheduleIn(BaseModel):
    # 上限的理由：名称与提示词会整份进列表响应与每一次无头 run 的上下文，
    # 无上限等于允许贴一整篇文档进来
    name: str = Field(..., min_length=1, max_length=_NAME_MAX_LEN)
    # spec 2026-10-27-schedule-task-type：任务分类，仅影响模板与列表分区，不改执行
    task_type: Literal["ops", "daily"] = "ops"
    # spec §6.2：任务绑定的专家 slug，新建默认通用（general）；存量迁移回填 general_ops
    # 上限与 experts.slug 同口径（routes/experts.py:26 / db/experts.py:29 均为 64）
    expert_slug: str = Field(default="general", max_length=64)
    schedule_type: str = "interval"
    # 审查修复：加范围校验 — 0/负值入库后 compute_next_run 算出 ≤now 的时间，
    # 每 30s tick 都触发无头 SSH Agent 运行，持续消耗模型调用与 SSH 连接
    interval_minutes: int = Field(default=60, ge=1, le=525600)
    time_hour: int = 9
    time_minute: int = 0
    weekday: int = 0
    day_of_month: int = 1
    target_host: str = ""
    task_prompt: str = Field(default="", max_length=_TASK_PROMPT_MAX_LEN)
    enabled: bool = True


class SchedulePatch(BaseModel):
    """PATCH 局部更新：只更新显式传入的字段（exclude_unset）。"""
    # S7-11：这里的约束必须与 ScheduleIn 逐字对齐。原先 name 是裸 Optional[str]，
    # 而 drop_nulls 只丢 None ⇒ {"name": ""} 和超长名都能从 PATCH 进库：POST 挡住的
    # 空名/长名，改个名字就绕过了（models_config 的 PATCH 早就带了同一组 min/max）。
    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX_LEN)
    # spec 2026-10-27-schedule-task-type：类型创建后不可变，此字段仅用于拒绝变更（见 update_schedule）
    task_type: Optional[Literal["ops", "daily"]] = None
    # spec §6.2：专家 slug 可改（与 task_type 不同，绑定关系允许后续调整）
    # 约束与 ScheduleIn 逐字对齐（同 name 的教训：POST 挡住的超长值不得从 PATCH 绕过）
    expert_slug: Optional[str] = Field(default=None, max_length=64)
    schedule_type: Optional[str] = None
    # 审查修复：与 ScheduleIn 一致的范围校验
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=525600)
    time_hour: Optional[int] = None
    time_minute: Optional[int] = None
    weekday: Optional[int] = None
    day_of_month: Optional[int] = None
    target_host: Optional[str] = None
    task_prompt: Optional[str] = Field(default=None, max_length=_TASK_PROMPT_MAX_LEN)
    enabled: Optional[bool] = None


_SCHEDULE_KEYS = {
    "schedule_type", "interval_minutes", "time_hour", "time_minute",
    "weekday", "day_of_month",
}


@router.get("/schedules")
async def list_schedules():
    """全部定时任务（读盘丢线程池，不占事件循环）。"""
    return await asyncio.to_thread(db.list_schedules)


@router.post("/schedules")
async def create_schedule(req: ScheduleIn):
    """新建定时任务：周期类型不支持 400，字段先过 _validate_fields；落库时算好首个 next_run_at，并顺带把调度引擎起来。"""
    if req.schedule_type not in _SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的周期类型: {req.schedule_type}")
    # model_dump 对未变的 req 是确定性的，序列化一次复用即可
    data = req.model_dump()
    _validate_fields(data)
    # spec §6.2：expert_slug 必须指向已存在的专家。否则无头 run 解析时 fail-closed 抛错，
    # 任务每个 tick 都失败 —— 把校验左移到创建入口，与 headless 的 fail-closed 同口径。
    if await asyncio.to_thread(db.get_expert_by_slug, req.expert_slug) is None:
        raise HTTPException(status_code=400, detail=f"专家不存在: {req.expert_slug}")
    from agent.schedule_engine import get_schedule_engine, compute_next_run
    next_at = compute_next_run(data)
    created = await asyncio.to_thread(
        db.create_schedule, {**data, "next_run_at": next_at}
    )
    if created is None:
        # 读回为 None = INSERT 成功后该行被并发删除。不拦的话前端拿到 null
        # 会当成功渲染一个空任务。
        raise HTTPException(status_code=404, detail="任务创建后即被删除（并发竞态），请刷新列表")
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
    """局部更新任务；任务不存在 404；传入与原值不同的 task_type 一律 422 —— 类型创建后不可变。"""
    existing = await asyncio.to_thread(db.get_schedule, schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="任务不存在")
    patch = drop_nulls(req.model_dump(exclude_unset=True))
    # spec 2026-10-27-schedule-task-type：类型创建后不可变。显式传入与原值不同 → 422；
    # 相同值也一律从 patch 剔除（db.update_schedule 的 allowed 本就不含 task_type，此处双保险）。
    explicit_type = req.model_dump(exclude_unset=True).get("task_type")
    if explicit_type is not None and explicit_type != existing.get("task_type"):
        raise HTTPException(status_code=422, detail="任务类型创建后不可修改")
    patch.pop("task_type", None)
    # spec §6.2：改绑专家时同样校验存在性（与 create 同口径）。drop_nulls 已丢显式 null，
    # 故只在真的传了 expert_slug 时查库，未传则不动原绑定。
    if "expert_slug" in patch:
        if await asyncio.to_thread(db.get_expert_by_slug, patch["expert_slug"]) is None:
            raise HTTPException(status_code=400, detail=f"专家不存在: {patch['expert_slug']}")
    merged = {**existing, **patch}
    if merged.get("schedule_type") not in _SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的周期类型: {merged.get('schedule_type')}")
    _validate_fields(merged)
    updated = await asyncio.to_thread(db.update_schedule, schedule_id, patch)
    if not updated:
        # 校验与写入之间任务被另一个窗口删掉
        raise HTTPException(status_code=404, detail="任务不存在")
    # 调度参数变化，或从停用恢复为启用时重算下次执行时间。
    # 恢复启用必须重算：停用期间 next_run_at 早已过期，带着旧值启用会在
    # 下一个 tick 立即补跑一次；停用方向（true→false）不重算，保持原语义。
    reenabled = patch.get("enabled") is True and not existing.get("enabled")
    if (_SCHEDULE_KEYS & set(patch.keys())) or reenabled:
        from agent.schedule_engine import compute_next_run
        next_at = compute_next_run(updated)
        await asyncio.to_thread(db.set_schedule_next_run, schedule_id, next_at)
        # 重读后返回：updated 取于写 next_run_at 之前，直接回给调用方会带上
        # 旧的下次执行时间（改周期后接口显示与新周期不符）
        return await asyncio.to_thread(db.get_schedule, schedule_id)
    return updated


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    # 走 engine：先取消在途的无头 Agent run 并落定执行历史，再删行
    """删除任务：走 engine，先取消在途的无头 Agent run 并落定执行历史，再删行；不存在 404。"""
    from agent.schedule_engine import get_schedule_engine
    ok = await get_schedule_engine().remove_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str):
    """立即触发一次执行；任务不存在 404。需要远程会话的任务先探一次桥活（预算 1s），桥断就当场给原因码，不回 ok 谎报「已触发」。"""
    sched = await asyncio.to_thread(db.get_schedule, schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="任务不存在")
    from agent.schedule_engine import get_schedule_engine
    engine = get_schedule_engine()
    engine.ensure_started()
    # S7-07：桥断开时回 ok:true 是谎报 —— trigger 只 create_task，真正的失败要等
    # 那次无头 run 走到建会话那一步，用户看到的是「已触发立即执行」的 toast，
    # 真实结局要么 30s 轮询后落到卡片状态，要么主动去翻历史。需要远程会话的任务
    # 在触发前先探一次活（探活预算 1s），失败就地给原因码，不启动注定失败的 run。
    if (sched.get("target_host") or "").strip():
        await require_bridge_or_503()
    started = await engine.trigger(sched, manual=True)
    if not started:
        # 上一次还没跑完：trigger 会静默丢弃本次触发，此时回 ok:true 等于
        # 让前端弹「已触发立即执行」而实际什么都没发生
        raise HTTPException(status_code=409, detail="该任务正在执行中，请等本次结束后再触发")
    return {"ok": True, "schedule_id": schedule_id}


@router.get("/schedules/{schedule_id}/runs")
async def list_schedule_runs(schedule_id: str, limit: int = 50):
    """该任务的执行历史，limit 夹在 1..200；任务不存在 404。"""
    if not await asyncio.to_thread(db.get_schedule, schedule_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(db.list_schedule_runs, schedule_id, limit)


@router.delete("/schedules/{schedule_id}/runs")
async def clear_schedule_runs(schedule_id: str):
    """清空该任务的全部执行历史（保留任务本身）；有进行中的执行则 409 拒。

    进行中闸已下沉到 db.clear_schedule_runs 内部（BEGIN IMMEDIATE 同锁内
    检查 + 删除）：原先路由层"先查 running 再删"的两次独立 to_thread 之间
    是可被调度触发穿透的 TOCTOU 窗口，这里不再预检查。返回 None = 闸拒绝。
    """
    if not await asyncio.to_thread(db.get_schedule, schedule_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    deleted = await asyncio.to_thread(db.clear_schedule_runs, schedule_id)
    if deleted is None:
        raise HTTPException(status_code=409, detail="该任务正在执行中，请等本次结束后再清空历史")
    return {"ok": True, "deleted": deleted}