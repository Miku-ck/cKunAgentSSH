"""专家（Expert）路由 —— 运行策略配置包的 CRUD。

边界：专家只改配置，不引入新可执行能力（那是插件）。收紧语义全部在
`agent/expert_policy.py`，本层只负责三件事：配置合法性、内置专家保护、
JSON 字段的持久化口径（None=继承 / []=显式空集，两者不许混）。
"""
import json
import logging
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database as db
from agent.expert_policy import MEMORY_SCOPES, PERMISSION_CEILINGS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["experts"])

_MAX_LIST_ITEMS = 200
_TOOL_NAME_CACHE: Optional[set] = None


class ExpertCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    identity_prompt: str = Field(default="", max_length=8000)
    model_id: Optional[str] = None
    tool_names: Optional[list[str]] = None
    skill_ids: Optional[list[str]] = None
    plugin_ids: list[str] = Field(default_factory=list)
    permission_ceiling: str = "inherit"
    max_iterations: Optional[int] = Field(default=None, ge=1, le=500)
    memory_scope: str = "inherit"


class ExpertUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    identity_prompt: Optional[str] = Field(default=None, max_length=8000)
    model_id: Optional[str] = None
    tool_names: Optional[list[str]] = None
    skill_ids: Optional[list[str]] = None
    plugin_ids: Optional[list[str]] = None
    permission_ceiling: Optional[str] = None
    max_iterations: Optional[int] = Field(default=None, ge=1, le=500)
    memory_scope: Optional[str] = None


class ExpertDuplicate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=80)


class UpdateEnabledRequest(BaseModel):
    enabled: bool


def _builtin_tool_names() -> set:
    """合法内置工具名。缓存：管理接口低频，且工具清单在进程内不变。"""
    global _TOOL_NAME_CACHE
    if _TOOL_NAME_CACHE is None:
        from agent.tools.builtin import get_all_tool_classes
        _TOOL_NAME_CACHE = {cls().name for cls in get_all_tool_classes()}
    return _TOOL_NAME_CACHE


def _json_or_none(values: Optional[list], field: str) -> Optional[str]:
    """list → JSON 文本；None 原样保留（继承语义不能序列化成 'null' 再存）。"""
    if values is None:
        return None
    if len(values) > _MAX_LIST_ITEMS:
        raise HTTPException(status_code=400, detail=f"{field} 最多 {_MAX_LIST_ITEMS} 项")
    return json.dumps(list(values), ensure_ascii=False)


def _check_enums(ceiling: Optional[str], scope: Optional[str]) -> None:
    if ceiling is not None and ceiling not in PERMISSION_CEILINGS:
        raise HTTPException(status_code=400, detail=f"非法 permission_ceiling: {ceiling}")
    if scope is not None and scope not in MEMORY_SCOPES:
        raise HTTPException(status_code=400, detail=f"非法 memory_scope: {scope}")


def _check_tool_names(names: Optional[list]) -> None:
    """拒绝打错的工具名：交集会静默过滤掉不存在的名字，用户会误以为已生效。"""
    if not names:
        return
    valid = _builtin_tool_names()
    unknown = [n for n in names if n not in valid and not n.startswith("mcp__")]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知工具名: {', '.join(unknown)}")


def _writable_fields(payload: dict) -> dict:
    """请求字段 → DB 列名。list 字段在此序列化，枚举与工具名在此校验。"""
    _check_enums(payload.get("permission_ceiling"), payload.get("memory_scope"))
    _check_tool_names(payload.get("tool_names"))
    fields: dict = {}
    for key in ("name", "description", "identity_prompt", "model_id",
                "permission_ceiling", "max_iterations", "memory_scope"):
        if key in payload and payload[key] is not None:
            fields[key] = payload[key]
    if "tool_names" in payload:
        fields["tool_names_json"] = _json_or_none(payload["tool_names"], "tool_names")
    if "skill_ids" in payload:
        fields["skill_ids_json"] = _json_or_none(payload["skill_ids"], "skill_ids")
    if "plugin_ids" in payload and payload["plugin_ids"] is not None:
        fields["plugin_ids_json"] = _json_or_none(payload["plugin_ids"], "plugin_ids") or "[]"
    return fields


def _get_or_404(expert_id: str) -> dict:
    row = db.get_expert(expert_id)
    if not row:
        raise HTTPException(status_code=404, detail="专家不存在")
    return row


def _reject_builtin(row: dict, action: str) -> None:
    if row.get("is_builtin"):
        raise HTTPException(
            status_code=400,
            detail=f"内置专家不可{action}，请复制后再{action}",
        )


@router.get("/experts")
def list_experts(include_disabled: bool = Query(default=True)):
    return db.list_experts(include_disabled=include_disabled)


@router.get("/experts/{expert_id}")
def get_expert(expert_id: str):
    return _get_or_404(expert_id)


@router.post("/experts", status_code=201)
def create_expert(req: ExpertCreate):
    # name 是 create_expert 的位置参数，不能同时出现在 **fields 里
    payload = req.model_dump(exclude={"slug", "name"})
    fields = _writable_fields(payload)
    try:
        return db.create_expert(req.slug, req.name, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"slug 已存在: {req.slug}") from exc


@router.post("/experts/{expert_id}/duplicate", status_code=201)
def duplicate_expert(expert_id: str, req: ExpertDuplicate):
    """复制专家（内置专家只能经此派生）。复制体一律 enabled、is_builtin=0。"""
    source = _get_or_404(expert_id)
    fields = _writable_fields({
        "name": (req.name or f"{source['name']} 副本"),
        "description": source.get("description"),
        "identity_prompt": source.get("identity_prompt") or "",
        "model_id": source.get("model_id"),
        "permission_ceiling": source.get("permission_ceiling"),
        "max_iterations": source.get("max_iterations"),
        "memory_scope": source.get("memory_scope"),
    })
    # 集合字段直接沿用原行的 JSON 文本：None 继承 / [] 空集的语义原样复制
    for key in ("tool_names_json", "skill_ids_json", "plugin_ids_json"):
        fields[key] = source.get(key)
    fields["name"] = req.name or f"{source['name']} 副本"
    try:
        return db.create_expert(req.slug, fields.pop("name"), **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"slug 已存在: {req.slug}") from exc


@router.put("/experts/{expert_id}")
def update_expert(expert_id: str, req: ExpertUpdate):
    row = _get_or_404(expert_id)
    _reject_builtin(row, "编辑")
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        return row
    fields = _writable_fields(payload)
    try:
        updated = db.update_expert(expert_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="专家不存在")
    return updated


@router.patch("/experts/{expert_id}/enabled")
def set_expert_enabled(expert_id: str, req: UpdateEnabledRequest):
    row = _get_or_404(expert_id)
    if not req.enabled:
        _reject_builtin(row, "禁用")
    updated = db.set_expert_enabled(expert_id, 1 if req.enabled else 0)
    if not updated:
        raise HTTPException(status_code=400, detail="内置专家不可禁用")
    return updated


@router.delete("/experts/{expert_id}")
def delete_expert(expert_id: str):
    row = _get_or_404(expert_id)
    _reject_builtin(row, "删除")
    if not db.delete_expert(expert_id):
        raise HTTPException(status_code=400, detail="内置专家不可删除")
    return {"deleted": True}
