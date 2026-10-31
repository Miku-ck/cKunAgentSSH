"""运维剧本路由 — 剧本 CRUD + 一键执行 + 执行历史

GET    /api/runbooks           — 剧本列表（含 last_run 摘要）
POST   /api/runbooks           — 新建剧本
PATCH  /api/runbooks/{id}      — 更新剧本
DELETE /api/runbooks/{id}      — 删除剧本（执行中 409）
POST   /api/runbooks/{id}/run  — 一键执行（探活 503 / 执行中 409）
GET    /api/runbooks/{id}/runs — 执行历史（limit 夹取）
DELETE /api/runbooks/{id}/runs — 清空执行历史（保留剧本；执行中 409）

结构完全镜像 routes/schedules.py：Pydantic 上限 + 手工 _validate 式校验（400 而非
422，错误文案可读）、PATCH 的 exclude_unset + _drop_nulls 防御、桥探活前置
（schedules.py:164-173 先例）、执行中 409（schedules.py:174-178 先例）。
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import runbook_store as rstore

router = APIRouter(tags=["runbooks"])

# 入库上限（与前端 maxlength 同口径；名称对齐 schedules 的 100 字上限）
_NAME_MAX_LEN = 100
_DESCRIPTION_MAX_LEN = 500
_TARGET_HOST_MAX_LEN = 200
_STEP_MAX_COUNT = 20
_STEP_COMMAND_MAX_LEN = 2000
_STEP_NOTE_MAX_LEN = 500
_HISTORY_LIMIT_MAX = 200


class RunbookStepIn(BaseModel):
    # 形状/长度校验在 _validate_steps 手工做（统一回 400 与可读文案，Pydantic
    # 的 422 detail 是结构化数组，前端 toast 不可读）
    command: str = ""
    assertCommand: str = ""
    note: str = ""


class RunbookIn(BaseModel):
    # 上限理由：名称/说明整份进列表响应，步骤命令整份进每次无头 run 的上下文，
    # 无上限等于允许贴一整篇文档进来（同 schedules 的 S7-11 口径）
    name: str = Field(..., min_length=1, max_length=_NAME_MAX_LEN)
    description: str = Field(default="", max_length=_DESCRIPTION_MAX_LEN)
    target_host: str = Field(default="", max_length=_TARGET_HOST_MAX_LEN)
    steps: list[RunbookStepIn] = Field(default_factory=list)
    enabled: bool = True


class RunbookPatch(BaseModel):
    """PATCH 局部更新：只更新显式传入的字段（exclude_unset）。约束与 RunbookIn 对齐 ——
    Optional 裸字段会让 {"name": ""} / 超长名从 PATCH 绕过 POST 挡住的校验
    （routes/schedules.py:52 的审查教训同款）。"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX_LEN)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX_LEN)
    target_host: Optional[str] = Field(default=None, max_length=_TARGET_HOST_MAX_LEN)
    steps: Optional[list[RunbookStepIn]] = None
    enabled: Optional[bool] = None


def _drop_nulls(patch: dict) -> dict:
    """PATCH 中显式 null 与「未提供」等价，一律丢弃（routes/schedules.py:71-78 同款）。"""
    return {k: v for k, v in patch.items() if v is not None}


def _validate_steps(steps: list[RunbookStepIn]) -> list[dict]:
    """步骤数组形状/上限校验（POST/PATCH 共用）：全部回 400，文案可读。"""
    if not steps:
        raise HTTPException(status_code=400, detail="剧本至少需要 1 个步骤")
    if len(steps) > _STEP_MAX_COUNT:
        raise HTTPException(
            status_code=400, detail=f"步骤数需 ≤{_STEP_MAX_COUNT}，当前 {len(steps)} 步")
    out: list[dict] = []
    for i, s in enumerate(steps):
        cmd = (s.command or "").strip()
        if not cmd:
            raise HTTPException(status_code=400, detail=f"第 {i + 1} 步的命令不能为空")
        if len(cmd) > _STEP_COMMAND_MAX_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"第 {i + 1} 步的命令需 ≤{_STEP_COMMAND_MAX_LEN} 字",
            )
        assert_cmd = (s.assertCommand or "").strip()
        if len(assert_cmd) > _STEP_COMMAND_MAX_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"第 {i + 1} 步的验证命令需 ≤{_STEP_COMMAND_MAX_LEN} 字",
            )
        note = (s.note or "").strip()
        if len(note) > _STEP_NOTE_MAX_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"第 {i + 1} 步的说明需 ≤{_STEP_NOTE_MAX_LEN} 字",
            )
        out.append({"command": cmd, "assertCommand": assert_cmd, "note": note})
    return out


def _row_out(row: dict) -> dict:
    """步骤数组冗余安全化：存储侧已解析，这里再兜一层非 dict 元素。"""
    row["steps"] = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
    return row


@router.get("/runbooks")
async def list_runbooks():
    # 首次触达顺带收尸（schedule 引擎的收尸挂在循环启动，本引擎无轮询循环；
    # main.py 属 sharedWiring 禁改，挂不到 lifespan）—— 打开剧本页即把上个进程
    # 遗留的 running 行落 stale_reaped，前端 isRunning 不再吃到僵尸状态
    """全部运维剧本。首次触达顺带收尸上个进程遗留的 running 行 —— 本引擎没有轮询循环可挂，而 main.py 属不可改的装配段。"""
    from runbook_engine import get_runbook_engine
    await get_runbook_engine().ensure_reaped()
    rows = await asyncio.to_thread(rstore.list_runbooks)
    return [_row_out(r) for r in rows]


@router.post("/runbooks")
async def create_runbook(req: RunbookIn):
    """新建剧本：步骤先过 _validate_steps，名称/描述/目标主机去空白后落库。"""
    steps = _validate_steps(req.steps)
    created = await asyncio.to_thread(
        rstore.create_runbook,
        {
            "name": req.name.strip(),
            "description": req.description.strip(),
            "target_host": req.target_host.strip(),
            "steps": steps,
            "enabled": req.enabled,
        },
    )
    return _row_out(created)


@router.patch("/runbooks/{runbook_id}")
async def update_runbook(runbook_id: str, req: RunbookPatch):
    """局部更新剧本；不存在 404，名称被清空 400；patch 里出现 steps 键即整体替换并重新校验。"""
    existing = await asyncio.to_thread(rstore.get_runbook, runbook_id)
    if not existing:
        raise HTTPException(status_code=404, detail="剧本不存在")
    patch = _drop_nulls(req.model_dump(exclude_unset=True))
    if "name" in patch:
        patch["name"] = (patch.get("name") or "").strip()
        if not patch["name"]:
            raise HTTPException(status_code=400, detail="剧本名称不能为空")
    if "steps" in patch:
        # 存储列是 steps_json（字符串）——patch 以「steps 键存在」表达整体替换
        patch["steps"] = _validate_steps(req.steps)
    if "description" in patch:
        patch["description"] = (patch.get("description") or "").strip()
    if "target_host" in patch:
        patch["target_host"] = (patch.get("target_host") or "").strip()
    updated = await asyncio.to_thread(rstore.update_runbook, runbook_id, patch)
    if not updated:
        # 校验与写入之间剧本被另一个窗口删掉
        raise HTTPException(status_code=404, detail="剧本不存在")
    return _row_out(updated)


@router.delete("/runbooks/{runbook_id}")
async def delete_runbook(runbook_id: str):
    """删除剧本；不存在 404，正在执行 409。删除前先同步占位，堵住「检查后、删除前并发 trigger」的 TOCTOU 窗口。"""
    if not await asyncio.to_thread(rstore.get_runbook, runbook_id):
        raise HTTPException(status_code=404, detail="剧本不存在")
    from runbook_engine import get_runbook_engine
    engine = get_runbook_engine()
    # 同步检查+占位（无 await，事件循环内原子）：占位后到删除落库之间的所有
    # await 期间，trigger 的同步检查段会被拒——堵住「检查后、删除前并发 trigger
    # 建行、行被 FK CASCADE 静默吞掉但执行体继续跑」的 TOCTOU 窗口（评审意见 3）
    if not engine.begin_delete(runbook_id):
        raise HTTPException(
            status_code=409, detail="该剧本正在执行中，请等本次结束后再删除")
    try:
        ok = await asyncio.to_thread(rstore.delete_runbook, runbook_id)
    finally:
        engine.end_delete(runbook_id)
    if not ok:
        raise HTTPException(status_code=404, detail="剧本不存在")
    return {"ok": True}


@router.post("/runbooks/{runbook_id}/run")
async def run_runbook(runbook_id: str):
    """立即执行一次剧本；不存在 404；配了 target_host 时先探桥，桥断返回 503 并带原因码。"""
    runbook = await asyncio.to_thread(rstore.get_runbook, runbook_id)
    if not runbook:
        raise HTTPException(status_code=404, detail="剧本不存在")
    # 桥探活前置（routes/schedules.py:164-173 同款）：桥断开时触发注定失败的
    # 无头 run 等于谎报「已触发」，探活预算换用户当场看到原因码
    if (runbook.get("target_host") or "").strip():
        from agent.electron_bridge import ElectronBridge

        bridge_err = await ElectronBridge().probe()
        if bridge_err is not None:
            raise HTTPException(
                status_code=503,
                detail=f"Electron 桥当前不可达（{bridge_err.value}），本次未触发；"
                       "请确认主进程在运行后重试",
            )
    from runbook_engine import get_runbook_engine
    rrb_id = await get_runbook_engine().trigger(runbook)
    if not rrb_id:
        # 上一次还没跑完：trigger 静默丢弃本次触发，回 ok 等于谎报
        raise HTTPException(status_code=409, detail="该剧本正在执行中，请等本次结束后再触发")
    return {"ok": True, "runbook_run_id": rrb_id}


@router.get("/runbooks/{runbook_id}/runs")
async def list_runbook_runs(runbook_id: str, limit: int = 50):
    """该剧本的执行历史，limit 夹到 1.._HISTORY_LIMIT_MAX；不存在 404；steps_state 只保留 dict 条目。"""
    if not await asyncio.to_thread(rstore.get_runbook, runbook_id):
        raise HTTPException(status_code=404, detail="剧本不存在")
    limit = max(1, min(limit, _HISTORY_LIMIT_MAX))
    rows = await asyncio.to_thread(rstore.list_runbook_runs, runbook_id, limit)
    for r in rows:
        # steps_state 冗余安全化：脏 JSON 已在 store 兜底为数组，这里只滤非 dict
        r["steps_state"] = [s for s in (r.get("steps_state") or []) if isinstance(s, dict)]
    return rows


@router.delete("/runbooks/{runbook_id}/runs")
async def clear_runbook_runs(runbook_id: str):
    """清空该剧本的全部执行历史（保留剧本本身）；有进行中的执行则 409 拒。"""
    if not await asyncio.to_thread(rstore.get_runbook, runbook_id):
        raise HTTPException(status_code=404, detail="剧本不存在")
    # 进行中闸：有 running 行时拒清，避免删掉仍在写回的执行记录
    if await asyncio.to_thread(rstore.has_running_runbook_run, runbook_id):
        raise HTTPException(status_code=409, detail="该剧本正在执行中，请等本次结束后再清空历史")
    deleted = await asyncio.to_thread(rstore.clear_runbook_runs, runbook_id)
    return {"ok": True, "deleted": deleted}
