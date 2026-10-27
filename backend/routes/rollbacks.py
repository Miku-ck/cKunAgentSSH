"""run 级一键回滚路由 — undo 明细 / 回滚计划 / 回滚登记（功能 B 独占）。

GET  /api/rollback/runs/{run_id}/undo-actions — 某 run 的 undo 明细（不含快照）
POST /api/rollback/plan                       — 组装回滚任务文本（只组装不执行）
POST /api/rollback/register                   — 登记源 run → 回滚 run 关联

边界（设计文档 §2.3）：本 router **不碰 harness、不执行任何回滚动作** ——
回滚的执行由前端拿 plan 的 task_text 走既有 POST /api/agent/run 交互式全链
（await_approval → 审批卡 → grant 写前审计 → AuditLogger 落链），这是对
「apply_reverse 直连 ElectronBridge 治理旁路」的刻意反方向。鉴权由 main.py
的 X-API-Token 中间件统一覆盖 /api/*（sharedWiring：router 注册由集成者执行）。

响应契约：任何端点都**不返回 snapshot_b64**（大字段不出库、不进前端、不进
模型）；file 项在 task_text 里只写 (run_id, seq) 引用，快照留给 undo_restore
工具从 undo_store 直达远端。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db
import undo_store
from undo_store import ROLLBACKABLE_RUN_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rollbacks"])

# run id 形态：uuid4().hex（32 字符）；上限放宽到 64 以兼容历史形态
_RUN_ID_MAX_LEN = 64

# task_text 内【原命令】的预览上限。原命令仅作展示性上下文（"它原本做了什么"），
# 模型不需要原样复述它；而【反向命令必须全量进 task_text】—— shell 项唯一的
# 执行通道就是模型按任务书原样执行该命令（undo_restore 对 shell 条目直接拒绝，
# 也没有任何工具能从 undo 行读回它），截断它就是让模型去执行一条被剪短的命令
# （mv 到截断前缀的错误路径 / 静默失败谎报回滚）。反向命令的总长上限由
# _TASK_TEXT_MAX 整体兜底，超限直接 400 拒绝 plan。
_CMD_PREVIEW_MAX = 300

# task_text 硬上限：远低于 AgentRunRequest.task 的 50000（routes/agent.py:398），
# 超过即拒绝 —— 回滚计划宁缺勿滥，不让一次超大 run 的回滚任务悄悄膨胀
_TASK_TEXT_MAX = 40_000


class RollbackPlanIn(BaseModel):
    source_run_id: str = Field(..., min_length=1, max_length=_RUN_ID_MAX_LEN)


class RollbackRegisterIn(BaseModel):
    source_run_id: str = Field(..., min_length=1, max_length=_RUN_ID_MAX_LEN)
    rollback_run_id: str = Field(..., min_length=1, max_length=_RUN_ID_MAX_LEN)


def _item_warning(confidence: str, reverse_command: str | None) -> str:
    """逐项警示语（UI 反色提示的数据源）：会真删文件的形态必须点名。"""
    if confidence == "delete_created":
        return "回滚将删除该文件（原 run 执行时它不存在）"
    if confidence == "derived" and (reverse_command or "").startswith("rm "):
        return "回滚将执行删除命令"
    return ""


def _run_or_404(run_id: str) -> dict:
    run = db.get_agent_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    return run


@router.get("/rollback/runs/{run_id}/undo-actions")
async def get_undo_actions(run_id: str):
    """某 run 的 undo 明细 + 可信度 + 状态（不含 snapshot_b64）。"""
    run = await asyncio.to_thread(_run_or_404, run_id)
    rows = await asyncio.to_thread(undo_store.get_undo_actions, run_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"run 无 undo 记录: {run_id}")
    run_status = str(run.get("status") or "")
    terminal = run_status in (
        "completed", "failed", "max_reached", "human_help",
    )
    items = []
    for r in rows:
        confidence = str(r.get("confidence") or "manual")
        reverse = r.get("reverse_command")
        items.append({
            "seq": r.get("seq"),
            "kind": r.get("kind"),
            "tool": r.get("tool"),
            "command": r.get("command"),
            "reverse_command": reverse,
            "path": r.get("path"),
            "existed": r.get("existed"),
            "confidence": confidence,
            "warning": _item_warning(confidence, reverse if isinstance(reverse, str) else None),
            "status": r.get("status"),
            "host": r.get("host"),
            "environment": r.get("environment"),
        })
    return {
        "run_id": run_id,
        "run_status": run_status,
        "terminal": terminal,
        "items": items,
    }


def _truncate(text: str, limit: int = _CMD_PREVIEW_MAX) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断展示，共 {len(text)} 字符）"


def _build_task_text(source_run_id: str, host: str, environment: str, items: list[dict]) -> str:
    """组装回滚任务书：LIFO 序、逐项可信度+危险标记、file 项只给 (run_id, seq) 引用。"""
    lines: list[str] = [
        "你是回滚执行器。请把下面这次 run 产生的副作用按序撤销，"
        "严格遵守 LIFO：从最后一项开始逐项执行，不得跳序、不得合并。",
        "",
        f"源 run：{source_run_id}",
        f"目标主机：{host or '（沿用当前 SSH 会话的主机，与源 run 相同）'}",
        f"目标环境：{environment}",
        "撤销完成后逐项汇报实际结果；任何一步失败都如实报告失败原因与已执行到的位置，"
        "绝不凭空声称已回滚。危险命令会弹出审批卡，等待用户批准后继续。",
        "",
        f"待撤销项（共 {len(items)} 项，按回滚顺序）：",
    ]
    for idx, it in enumerate(items, start=1):
        confidence = it["confidence"]
        if it["kind"] == "shell":
            # 反向命令【全量】写进任务书（模型唯一输入，截断=让模型执行残缺命令）；
            # 原命令仅展示上下文，可截断
            lines.append(
                f"{idx}. [seq={it['seq']}] shell（可信度：{confidence}）"
                f"：用 shell_exec 原样执行反向命令 {it['reverse_command'] or ''}"
                f"（原命令：{_truncate(it['command'] or '')}）"
            )
        elif it["kind"] == "file":
            if confidence == "delete_created":
                lines.append(
                    f"{idx}. [seq={it['seq']}] file（可信度：{confidence}）"
                    f"：删除 run 期间新建的文件 {it['path']} —— "
                    f'调用 undo_restore(run_id="{source_run_id}", seq={it["seq"]}) 完成，'
                    "不要用 shell 拼接或猜测内容"
                )
            else:
                lines.append(
                    f"{idx}. [seq={it['seq']}] file（可信度：{confidence}）"
                    f"：把 {it['path']} 还原为源 run 执行前的内容 —— "
                    f'调用 undo_restore(run_id="{source_run_id}", seq={it["seq"]}) 完成，'
                    "快照由工具从登记处直达目标机，你不要读写文件内容本身"
                )
        else:
            # noop/manual 理论上不进计划（plan 已排除）；兜底行为仍要诚实
            lines.append(f"{idx}. [seq={it['seq']}] 其他（{it['kind']}）：无可执行还原动作，跳过并如实报告")
    return "\n".join(lines)


@router.post("/rollback/plan")
async def build_rollback_plan(req: RollbackPlanIn):
    """组装回滚任务文本（只组装不执行）。执行由前端走既有 POST /api/agent/run。"""
    run = await asyncio.to_thread(_run_or_404, req.source_run_id)
    run_status = str(run.get("status") or "")
    if run_status not in ROLLBACKABLE_RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(f"run 状态为 {run_status or '未知'}，仅 completed/human_help/max_reached "
                    "的 run 可回滚（failed 已在收尾时自动回滚）"),
        )
    rows = await asyncio.to_thread(undo_store.get_undo_actions, req.source_run_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"run 无 undo 记录: {req.source_run_id}")
    # 只取可回滚项：pending 且非 manual（manual=无反向命令/无快照/无副作用，
    # 进了任务书也只是教模型做无意义动作）；LIFO = seq 倒序
    pending = [
        r for r in rows
        if r.get("status") == "pending" and r.get("confidence") != "manual"
    ]
    if not pending:
        raise HTTPException(status_code=404, detail="该 run 没有待回滚的副作用（均已回滚或无可信反向）")
    items_lifo = [
        {
            "seq": r.get("seq"),
            "kind": r.get("kind"),
            "tool": r.get("tool"),
            "command": r.get("command"),
            "reverse_command": r.get("reverse_command"),
            "path": r.get("path"),
            "existed": r.get("existed"),
            "confidence": r.get("confidence"),
            "warning": _item_warning(
                str(r.get("confidence") or "manual"),
                r.get("reverse_command") if isinstance(r.get("reverse_command"), str) else None,
            ),
            "status": r.get("status"),
            "host": r.get("host"),
            "environment": r.get("environment"),
        }
        for r in sorted(pending, key=lambda x: x.get("seq") or 0, reverse=True)
    ]
    host = next((str(r.get("host") or "") for r in rows if r.get("host")), "")
    environment = next((str(r.get("environment") or "") for r in rows if r.get("environment")), "") or "test"
    ssh_hint = ",".join(sorted({str(r.get("ssh_session_id") or "") for r in rows if r.get("ssh_session_id")}))
    task_text = _build_task_text(req.source_run_id, host, environment, items_lifo)
    if len(task_text) > _TASK_TEXT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"回滚任务文本 {len(task_text)} 字符超上限 {_TASK_TEXT_MAX}"
                   "（常见成因：某条 shell 反向命令/路径过长——反向命令不截断、必须全量"
                   "下发）。请先缩小该 run 的回滚范围（分多次回滚）",
        )
    return {
        "source_run_id": req.source_run_id,
        # 回滚消息落原会话、对用户可见（routes/agent.py 用户/assistant 消息都写进该 conversation）
        "conversation_id": str(run.get("conversation_id") or ""),
        "task_text": task_text,
        "host": host,
        "environment": environment,
        # 源 run 的 SSH 会话 id 是临时号、大概率已失效：仅作提示，前端不做自动重连
        "ssh_session_hint": ssh_hint,
        "items": items_lifo,
    }


@router.post("/rollback/register")
async def register_rollback(req: RollbackRegisterIn):
    """登记源 run → 回滚 run 关联（幂等）。undo_restore 归属校验的数据前提。"""
    await asyncio.to_thread(_run_or_404, req.source_run_id)
    await asyncio.to_thread(_run_or_404, req.rollback_run_id)
    if req.source_run_id == req.rollback_run_id:
        raise HTTPException(status_code=400, detail="source_run_id 与 rollback_run_id 不能相同")
    try:
        row = await asyncio.to_thread(
            undo_store.register_rollback, req.source_run_id, req.rollback_run_id,
        )
    except Exception as e:
        # FK 竞态（校验后 run 刚被 prune）：按 409 语义让前端重试，不 500
        logger.warning("register_rollback 落库失败（%s → %s）：%s",
                       req.source_run_id, req.rollback_run_id, e)
        raise HTTPException(status_code=409, detail="run 记录刚被清理，请刷新后重试")
    return row
