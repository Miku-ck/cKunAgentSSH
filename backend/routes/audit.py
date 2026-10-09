"""
审计路由 — 运维审计查询 / 链完整性校验 / 导出 / 归档轮转

对齐等保2.0「安全审计」：Agent 对本地/远程主机执行的命令与审批结论已由
AuditEngine 追加式写入 audit_log（链式签名防篡改）。本路由以只读查询为主；
唯一的受控写入口是 POST /audit/clear —— 它不是删除接口，而是「归档轮转」：
全量导出 CSV 归档后清空 live 表，并写入 audit_cleared 封缄记录作为新链起点
（见 agent.audit.archive_and_clear），历史证据经归档文件仍可离线验链。
"""
import asyncio
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from agent.audit import (
    AUDIT_COLUMNS,
    archive_and_clear,
    count,
    export_rows,
    query,
    verify,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audit"])


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
    return await asyncio.to_thread(verify)


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
