"""
审计路由 — 运维审计查询 / 链完整性校验 / 导出

对齐等保2.0「安全审计」：Agent 对本地/远程主机执行的命令与审批结论已由
AuditEngine 追加式写入 audit_log（链式签名防篡改）。本路由仅提供只读查询，
不提供任何更新/删除接口，维持审计的追加式契约。
"""
import asyncio
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import Response

from agent.audit import count, export_rows, query, verify

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
    items, total = await asyncio.to_thread(
        lambda: (
            query(
                environment=environment,
                target_id=target_id,
                action=action,
                conclusion=conclusion,
                run_id=run_id,
                limit=limit,
                offset=offset,
            ),
            # 审查修复：count 过滤集与 query 对齐（原实现缺 action/run_id）
            count(
                environment=environment,
                target_id=target_id,
                action=action,
                conclusion=conclusion,
                run_id=run_id,
            ),
        )
    )
    return {"items": items, "total": total}


@router.get("/audit/verify")
async def audit_verify():
    """全链完整性校验：重算每条记录的 hash 与 prev_hash 是否一致"""
    return verify()


@router.get("/audit/export")
async def audit_export():
    """导出全部审计记录为 CSV（追加式导出，含链式字段）

    审查修复：原实现同步全表拉取 + CSV 构建直接跑在事件循环上，
    审计表大时冻结全部请求并内存激增 → 移入线程池
    """
    def _build() -> str:
        rows = export_rows()
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys() if rows else ["id"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    content = await asyncio.to_thread(_build)
    return Response(
        content="\ufeff" + content,  # BOM，便于 Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )