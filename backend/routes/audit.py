"""
审计路由 — 运维审计查询 / 链完整性校验 / 导出 / 归档轮转 / 渲染层通道追加

对齐等保2.0「安全审计」：Agent 对本地/远程主机执行的命令与审批结论已由
AuditEngine 追加式写入 audit_log（链式签名防篡改）。本路由以只读查询为主，
受控写入口有两处：
- POST /audit/append —— 渲染层人工交互通道（批量下发、Docker 变更动作）的追加；
- POST /audit/clear —— 不是删除接口，而是「归档轮转」：全量导出 CSV 归档后
  清空 live 表，并写入 audit_cleared 封缄记录作为新链起点（见
  agent.audit.archive_and_clear），历史证据经归档文件仍可离线验链。
"""
import asyncio
import csv
import io
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.audit import (
    AUDIT_COLUMNS,
    LEGAL_CONCLUSIONS,
    append_record_async,
    archive_and_clear,
    count,
    export_rows,
    health,
    query,
    verify,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audit"])


class AuditAppendRequest(BaseModel):
    """渲染层通道审计的追加请求。

    extra="forbid"：operator / ts / hash / prev_hash 这些归因与链字段全部由
    服务端生成。尤其 operator —— 客户端多传一律 422，别让调用方把动作归到
    别人头上。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=128)
    conclusion: str
    target_type: str = Field(default="", max_length=64)
    target_id: str = Field(default="", max_length=256)
    environment: Literal["test", "prod"] = "test"
    command_raw: str = Field(default="", max_length=8192)
    result: str = Field(default="", max_length=2048)
    run_id: str = Field(default="", max_length=128)
    iteration: int = Field(default=0, ge=0)

    @field_validator("action")
    @classmethod
    def _action_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("action 不能为空")
        return v

    @field_validator("conclusion")
    @classmethod
    def _conclusion_is_legal(cls, v: str) -> str:
        # HTTP 边界不沿用 append_record 的「非法结论改 executed + 留痕」兜底：
        # 那是为了不阻塞 Agent 内部流程；调用方把 "executed " 拼错时，静默改名
        # 等于让链式签名记录声称一条没发生过的执行。
        if v not in LEGAL_CONCLUSIONS:
            raise ValueError(f"非法审计结论：{v!r}")
        return v


@router.post("/audit/append")
async def audit_append(payload: AuditAppendRequest):
    """追加一条渲染层通道审计记录。

    受全局 X-API-Token 中间件保护（与 /api/* 其余端点同口径）。写失败返回 500：
    渲染层只提示、不阻塞已发生的远端动作，但绝不能返回 ok 让调用方误以为记上了。
    """
    try:
        record = await append_record_async(
            action=payload.action,
            conclusion=payload.conclusion,
            target_type=payload.target_type,
            target_id=payload.target_id,
            environment=payload.environment,
            command_raw=payload.command_raw,
            result=payload.result,
            run_id=payload.run_id,
            iteration=payload.iteration,
        )
    except Exception as e:
        # append_record 已承诺「失败静默返回 None」，to_thread 本身抛异常是池层故障
        logger.warning("审计追加失败: action=%s err=%s", payload.action, e)
        raise HTTPException(status_code=500, detail="审计写入失败，请稍后重试") from e
    if record is None:
        # append_record 内部已记 warning；这里补上通道归因，便于区分是哪条链路丢的
        logger.warning(
            "审计追加失败: action=%s target=%s (append_record 返回 None)",
            payload.action, payload.target_id,
        )
        raise HTTPException(status_code=500, detail="审计写入失败，请稍后重试")
    return {"ok": True, "id": record["id"], "hash": record["hash"]}


@router.get("/audit/records")
async def audit_records(
    environment: Optional[str] = Query(default=None),
    target_id: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    conclusion: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """按条件查询审计记录（倒序）

    审查修复：query/count 是同步 sqlite，移入线程池避免阻塞事件循环
    """
    # query 与 count 共用同一过滤集 —— 二者曾各自手抄参数，count 一度漏掉
    # action/run_id 导致 total 与 items 过滤不一致；收敛到单一 filters 杜绝漂移。
    filters = dict(
        environment=environment,
        target_id=target_id,
        action=action,
        conclusion=conclusion,
        run_id=run_id,
    )
    try:
        items, total = await asyncio.to_thread(
            lambda: (
                query(**filters, limit=limit, offset=offset),
                count(**filters),
            )
        )
    except Exception as e:
        # S11-01：读不到就报 500。审计页有 error 通道，让它说「加载审计记录失败」，
        # 而不是拿着空 items 渲染出一个「一条记录都没有」的假清白。
        # detail 用固定文案，不把 sqlite 原始串端给用户（S0-24 同口径）。
        logger.warning("审计记录读取失败: %s", e)
        raise HTTPException(status_code=500, detail="审计记录读取失败，请稍后重试") from e
    return {"items": items, "total": total}


@router.get("/audit/verify")
async def audit_verify():
    """全链完整性校验：重算每条记录的 hash 与 prev_hash 是否一致

    审查修复：与 records/export 同批 — verify 全表扫 + 逐行 sha256 是同步重活，
    直接跑在事件循环上会冻结所有并发请求（含聊天 SSE），移入线程池
    """
    try:
        return await asyncio.to_thread(verify)
    except Exception as e:
        # 读不到就报 500。审计页的 verify 有 catch（→「审计链校验失败」），
        # 而原先返回的 intact=false 会被渲染成「链被篡改!」—— 见 agent.audit.verify
        # 的注释：那是把一次读故障指控成篡改证据，比报空更糟。
        logger.warning("审计链校验失败: %s", e)
        raise HTTPException(status_code=500, detail="审计链校验失败，请稍后重试") from e


@router.get("/audit/health")
async def audit_health():
    """审计写入健康度（最近一次写入是否失败、累计失败数）。

    为什么需要这个端点：审计写入失败一直是「一行 logger.warning」，而审计页
    拿不到任何信号 —— 一次磁盘满/锁冲突之后，页面照旧渲染出干净的一条链，
    用户会以为「确实没发生什么」，而不是「这几小时的证据没写进去」。
    ok=false 时前端应提示证据链当前不完整。
    """
    return health()


@router.get("/audit/export")
async def audit_export():
    """导出全部审计记录为 CSV（追加式导出，含链式字段）

    审查修复：原实现同步全表拉取 + CSV 构建直接跑在事件循环上，
    审计表大时冻结全部请求并内存激增 → 移入线程池
    """
    def _build() -> str:
        rows = export_rows()
        buf = io.StringIO()
        # 空表也要出完整表头：原先退化成 ["id"] 一列，导出的文件结构上
        # 就不是审计日志（而读失败现在会抛，不会再冒充成空表）
        writer = csv.DictWriter(buf, fieldnames=(rows[0].keys() if rows else AUDIT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    try:
        content = await asyncio.to_thread(_build)
    except Exception as e:
        # 与 /audit/records 同一口径（S11-01）：读不到就报错，别交付一份
        # 看起来「审计没有记录」的 CSV —— 那是会交到评估方手里的产物
        logger.warning("审计记录导出失败: %s", e)
        raise HTTPException(status_code=500, detail="审计记录导出失败，请稍后重试") from e
    return Response(
        content="\ufeff" + content,  # BOM，便于 Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@router.post("/audit/clear")
async def audit_clear():
    """归档式清空（轮转）：CSV 归档落盘 → 清空 live 表 → 封缄记录开新链。

    归档文件写在数据库同目录（audit_archive_*.csv），返回其路径供 UI 展示。
    同步 sqlite 移入线程池；失败抛 500，事务已在函数内回滚（链原样保留）。
    """
    try:
        result = await asyncio.to_thread(archive_and_clear)
    except Exception as e:
        logger.exception("审计归档轮转失败")
        raise HTTPException(status_code=500, detail=f"归档清理失败，审计链未变动：{e}")
    return result
