"""
Agent 记忆管理路由（v2：统一 memories 表 + jobs/audit/blocks）

从 routes/agent.py 拆出（P2 单文件单域）。端点路径与拆分前完全一致，
对外契约不变；原模块通过 re-export 保持 `routes.agent.create_agent_memory`
等符号仍可被既有测试直接调用。

  GET    /agent/memories                — 列表（embedding 不出后端）
  GET    /agent/memories/stats          — 按 kind/status 计数 + job 水位
  PATCH  /agent/memories/{memory_id}    — 编辑内容（旧向量作废）/ 置顶
  POST   /agent/memories                — 手动新增（直写 active + pinned）
  DELETE /agent/memories/{memory_id}    — 失效（不物理删，审计链保留）
  POST   /agent/memories/clear          — 清空非 pinned
  GET    /agent/memories/audit          — 审计流水
  GET    /agent/memories/jobs           — 任务队列
  POST   /agent/memories/jobs/{job_id}/retry
  GET    /agent/blocks                  — 全部记忆块当前值
  PUT    /agent/blocks/{key}            — 整块写入
  GET    /agent/blocks/{key}/history    — 块历史版本
"""
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agent.memory import mstore

router = APIRouter()


class MemoryEditRequest(BaseModel):
    content: Optional[str] = None
    title: Optional[str] = None
    pinned: Optional[bool] = None


class BlockPutRequest(BaseModel):
    value: str


class MemoryCreateRequest(BaseModel):
    kind: Literal["semantic", "episodic", "procedural"] = "semantic"
    content: str
    title: str = ""
    scope: str = "global"
    importance: float = Field(0.7, ge=0.0, le=1.0)


@router.get("/agent/memories")
def list_agent_memories(
    kind: Optional[str] = None,
    mem_type: Optional[str] = None,   # 旧参数名兼容（P4 前端切 kind 后移除）
    scope: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """v2 记忆列表（embedding 不出后端，仅 has_embedding 标记）"""
    rows = mstore.list_memories(
        kind=kind or mem_type, scope=scope, status=status, q=q, limit=limit, offset=offset,
    )
    return {"memories": rows, "count": len(rows)}


@router.get("/agent/memories/stats")
def memory_stats():
    """按 kind/status 的计数 + job 队列水位（管理面板头部）"""
    return mstore.stats()


@router.patch("/agent/memories/{memory_id}")
def edit_agent_memory(memory_id: str, req: MemoryEditRequest):
    """用户编辑：改内容（旧向量作废）与/或 pin 状态"""
    row = mstore.get_memory(memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if req.content is not None:
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="内容不能为空")
        mstore.user_edit(memory_id, content=req.content.strip(),
                         title=(req.title if req.title is not None else row.get("title") or ""))
    if req.pinned is not None:
        mstore.set_pinned(memory_id, req.pinned)
    return {"updated": True, "memory": mstore.get_memory(memory_id)}


@router.post("/agent/memories")
def create_agent_memory(req: MemoryCreateRequest):
    """用户手动新增：直写 active 记忆并置顶（pinned），不过对账管线。

    对账是为自动沉淀准备的（判重、消解矛盾）；用户在 UI 里敲定的事实本身就是仲裁
    结果，再让模型判一遍只会把它改写成另一句话。pinned 让它不参与池容量修剪与衰减
    ——见 mstore.trim_pools/decay_memories 的 pinned=0 条件。
    不嵌向量：本路径没有模型凭证上下文，宁缺毋滥（与 user_edit 置 NULL 同策略），
    FTS 关键词通道照常召回，列表里的 has_embedding 如实显示 false。
    """
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    scope = req.scope.strip() or "global"
    if scope != "global" and not (scope.startswith("host:") or scope.startswith("run:")):
        raise HTTPException(status_code=400, detail=f"非法 scope: {scope}")
    mid = mstore.add_memory(
        kind=req.kind, content=content, scope=scope,
        title=req.title.strip(), source="user_ui",
        importance=req.importance, pinned=1,
    )
    mstore.add_audit(mid, "ADD", new=content, reason="user_ui")
    return {"created": True, "id": mid, "memory": mstore.get_memory(mid)}


@router.delete("/agent/memories/{memory_id}")
def delete_agent_memory(memory_id: str):
    """用户删除 = invalidate（失效不物理删，审计链保留）"""
    ok = mstore.retire(memory_id, reason="user_delete")
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在或已失效")
    return {"deleted": True, "id": memory_id}


@router.post("/agent/memories/clear")
def clear_agent_memories():
    """清空：全部非 pinned 记忆 invalidate（pinned 是用户显式保留，永不被批量操作波及）"""
    return {"invalidated": mstore.retire_all_unpinned()}


@router.get("/agent/memories/audit")
def list_memory_audit(memory_id: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    """记忆变更审计流水，可按 memory_id 过滤，limit 1..500。"""
    return {"audit": mstore.list_audit(memory_id=memory_id, limit=limit)}


@router.get("/agent/memories/jobs")
def list_memory_jobs(status: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
    """记忆维护任务队列（后台心跳排的那批），可按状态过滤，limit 1..500。"""
    return {"jobs": mstore.list_jobs(status=status, limit=limit)}


@router.post("/agent/memories/jobs/{job_id}/retry")
def retry_memory_job(job_id: str):
    """重跑一个 failed 的记忆任务；job 不存在或不是 failed 状态返回 404。"""
    if not mstore.retry_job(job_id):
        raise HTTPException(status_code=404, detail="job 不存在或非 failed 状态")
    return {"retried": True, "id": job_id}


@router.get("/agent/blocks")
def list_memory_blocks():
    """全部记忆块的当前值。"""
    return {"blocks": mstore.list_blocks()}


@router.put("/agent/blocks/{key}")
def put_memory_block(key: str, req: BlockPutRequest):
    """UI 整块写入（agent 只能走 replace 工具，见 tools/memory.py）"""
    from agent.memory import blocks as memory_blocks
    if not memory_blocks.allowed_key(key):
        raise HTTPException(status_code=400, detail=f"非法块键: {key}")
    try:
        version = mstore.put_block(key, req.value, updated_by="user_ui")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"key": key, "version": version}


@router.get("/agent/blocks/{key}/history")
def memory_block_history(key: str, limit: int = Query(20, ge=1, le=100)):
    """某个记忆块的历史版本，limit 1..100。"""
    return {"history": mstore.list_block_history(key, limit=limit)}
