"""插件（MCP server / 外部进程）管理路由。

同意流分三步，任何一步都不能被跳过：
  安装（保存即停用、未确认）→ 验证并加载工具（用户主动点击，看到工具清单）
  → 确认启用（服务端写 consented_at 与 enabled）。
配置变更一律撤销同意、递增 config_rev 并立刻关闭会话；
只读资格由用户逐项确认，服务端的 readOnlyHint 只作展示建议。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import database as db
from agent.plugins.manager import get_plugin_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["plugins"])

_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE")


class PluginCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    command: str = Field(..., min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    cwd: Optional[str] = Field(default=None, max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: int = Field(default=30, ge=5, le=120)

    @field_validator("command")
    @classmethod
    def no_shell_metachars(cls, value: str) -> str:
        # 命令走 create_subprocess_exec 数组参数，不经 shell；这里的字符若出现，
        # 说明调用方把整条命令行塞进来了，必须提前拒绝而不是静默当程序名。
        if any(ch in value for ch in (" ", "&", "|", ";", "$", "`")):
            raise ValueError("command 只能是可执行程序名，参数请放进 args")
        return value


class PluginUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    command: Optional[str] = Field(default=None, max_length=500)
    args: Optional[list[str]] = Field(default=None, max_length=50)
    cwd: Optional[str] = Field(default=None, max_length=500)
    env: Optional[dict[str, str]] = None
    timeout_s: Optional[int] = Field(default=None, ge=5, le=120)

    @field_validator("command")
    @classmethod
    def no_shell_metachars(cls, value: Optional[str]) -> Optional[str]:
        # 与 PluginCreate 同一防线：更新路径不得绕过创建路径的校验。
        if value is None:
            return value
        if any(ch in value for ch in (" ", "&", "|", ";", "$", "`")):
            raise ValueError("command 只能是可执行程序名，参数请放进 args")
        return value


class ReadonlyRequest(BaseModel):
    names: list[str]


class UpdateEnabledRequest(BaseModel):
    enabled: bool


def _pack_env(env: Optional[dict[str, str]]) -> Optional[str]:
    """环境变量整体加密成 DPAPI 信封；非 Windows 拒绝保存秘密项。"""
    if env is None:
        return None
    if not env:
        return ""
    if sys.platform != "win32":
        risky = [k for k in env if any(w in k.upper() for w in _SECRET_WORDS)]
        if risky:
            raise HTTPException(
                status_code=400,
                detail=f"非 Windows 平台不支持保存秘密环境变量: {', '.join(risky)}",
            )
    return db.protect_secret(json.dumps(env, ensure_ascii=False))


def _get_or_404(plugin_id: str) -> dict:
    row = db.get_plugin(plugin_id)
    if not row:
        raise HTTPException(status_code=404, detail="插件不存在")
    return row


def _tools_of(row: dict) -> list[dict]:
    try:
        return json.loads(row.get("tools_json") or "[]")
    except (TypeError, ValueError):
        return []


@router.get("/plugins")
def list_plugins():
    return db.list_plugins()


@router.post("/plugins", status_code=201)
def create_plugin(req: PluginCreate):
    payload = {
        "description": req.description,
        "args_json": json.dumps(req.args, ensure_ascii=False),
        "cwd": req.cwd,
        "timeout_s": req.timeout_s,
        "env_ciphertext": _pack_env(req.env),
    }
    try:
        return db.create_plugin(req.slug, req.name, req.command, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"slug 已存在: {req.slug}") from exc


@router.put("/plugins/{plugin_id}")
async def update_plugin(plugin_id: str, req: PluginUpdate):
    _get_or_404(plugin_id)
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        return db.get_plugin(plugin_id)
    env = payload.pop("env", None)
    args = payload.pop("args", None)
    if env is not None:
        payload["env_ciphertext"] = _pack_env(env)
    if args is not None:
        payload["args_json"] = json.dumps(args, ensure_ascii=False)
    try:
        updated = db.update_plugin_config(plugin_id, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await get_plugin_manager().close(plugin_id)
    return updated


@router.post("/plugins/{plugin_id}/validate")
async def validate_plugin(plugin_id: str):
    """用户主动点击才启动进程：发现工具并写入快照，插件仍保持停用。"""
    row = _get_or_404(plugin_id)
    try:
        specs = await get_plugin_manager().list_tools(
            db.get_plugin(plugin_id, include_env=True) or row,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"无法连接插件进程: {type(exc).__name__}: {exc}",
        ) from exc
    db.set_plugin_tools(plugin_id, specs)
    await get_plugin_manager().close(plugin_id)
    return {"tools": specs, "count": len(specs)}


@router.post("/plugins/{plugin_id}/consent")
async def consent_plugin(plugin_id: str):
    """看完工具清单后确认启用。未验证过工具清单的插件不能启用。"""
    row = _get_or_404(plugin_id)
    if not _tools_of(row):
        raise HTTPException(status_code=400, detail="请先验证并加载工具清单")
    return db.consent_plugin(plugin_id)


@router.post("/plugins/{plugin_id}/readonly")
async def set_readonly(plugin_id: str, req: ReadonlyRequest):
    """逐项确认只读。名字必须在已发现的工具清单里，不能凭空授权。"""
    row = _get_or_404(plugin_id)
    known = {s.get("name") for s in _tools_of(row)}
    unknown = [n for n in req.names if n not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知工具名: {', '.join(unknown)}")
    await get_plugin_manager().close(plugin_id)
    return db.set_plugin_readonly(plugin_id, req.names)


@router.patch("/plugins/{plugin_id}/enabled")
async def set_enabled(plugin_id: str, req: UpdateEnabledRequest):
    row = _get_or_404(plugin_id)
    if req.enabled and not row.get("consented_at"):
        raise HTTPException(status_code=400, detail="插件尚未确认授权，请先验证并确认")
    if not req.enabled:
        await get_plugin_manager().close(plugin_id)
    return db.set_plugin_enabled(plugin_id, 1 if req.enabled else 0)


@router.delete("/plugins/{plugin_id}")
async def delete_plugin(plugin_id: str):
    _get_or_404(plugin_id)
    await get_plugin_manager().close(plugin_id)
    if not db.delete_plugin(plugin_id):
        raise HTTPException(status_code=404, detail="插件不存在")
    return {"deleted": True}
