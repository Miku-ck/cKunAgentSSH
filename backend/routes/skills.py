"""
技能路由 — 技能 CRUD 与启用管理（支持按模式隔离）
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import database as db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skills"])


def _reload_skill_registry() -> None:
    """技能变更后刷新内存注册表缓存，确保 Agent 下次运行能看到最新技能"""
    try:
        from skill_registry import get_skill_registry
        get_skill_registry().reload()
    except Exception as e:
        logger.warning("刷新技能注册表缓存失败: %s", e)


class CreateSkillRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=100)
    system_prompt: str = Field(..., min_length=1)
    description: Optional[str] = Field(default="")
    category: Optional[str] = Field(default="")
    source: Optional[str] = Field(default="manual")
    is_builtin: Optional[int] = Field(default=0)
    enabled: Optional[int] = Field(default=0)
    priority: Optional[int] = Field(default=0)
    # 触发词列表，由 database.create_skill 序列化为 JSON 存储此处直接传 list
    trigger_words: Optional[list[str]] = Field(default_factory=list)


class UpdateEnabledRequest(BaseModel):
    enabled: bool


@router.get("/skills")
async def list_skills(limit: int = 200, offset: int = 0, mode: Optional[str] = None):
    """获取所有技能列表

    Args:
        mode: 若指定模式，则附加 per-mode 的 enabled 状态（mode_enabled 字段）。
              未指定 mode 时仅返回全局 enabled 状态。
    """
    skills = db.list_skills(limit=limit, offset=offset)
    mode_configs = db.list_all_skill_mode_configs()
    for s in skills:
        s['mode_configs'] = mode_configs.get(s['id'], {})
    if mode:
        disabled_ids = set(db.list_disabled_skills_for_mode(mode))
        for s in skills:
            # mode_enabled: 该技能在该模式下是否启用（全局 enabled=1 且未在该模式 disabled 列表中）
            s['mode_enabled'] = s['enabled'] == 1 and s['id'] not in disabled_ids
    return skills


@router.post("/skills")
async def create_skill(req: CreateSkillRequest):
    """创建新技能（自动处理 slug 冲突）"""
    result = db.create_skill(
        name=req.name,
        slug=req.slug,
        system_prompt=req.system_prompt,
        description=req.description or "",
        category=req.category or "",
        source=req.source or "manual",
        is_builtin=req.is_builtin or 0,
        enabled=req.enabled or 0,
        priority=req.priority or 0,
        trigger_words=req.trigger_words or [],
    )
    _reload_skill_registry()
    return result


@router.patch("/skills/{skill_id}/enabled")
async def update_skill_enabled(
    skill_id: str,
    req: UpdateEnabledRequest,
    mode: Optional[str] = None,
):
    """更新技能启用状态

    Args:
        mode: 若指定模式，则更新该模式下的启用状态（skill_mode_config 表）。
              未指定 mode 时更新全局 enabled 状态。
    """
    if mode:
        # 按模式隔离：写入 skill_mode_config 表
        result = db.upsert_skill_mode_config(skill_id, mode, 1 if req.enabled else 0)
        # 同时确保全局 enabled=1（否则 per-mode 启用无意义）
        if req.enabled:
            existing = db.get_skill(skill_id)
            if existing and existing.get('enabled') != 1:
                db.update_skill_enabled(skill_id, 1)
        _reload_skill_registry()
        return result
    else:
        result = db.update_skill_enabled(skill_id, 1 if req.enabled else 0)
        if not result:
            raise HTTPException(status_code=404, detail="Skill not found")
        _reload_skill_registry()
        return result


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: str):
    """删除非内置技能"""
    deleted = db.delete_skill(skill_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found or is builtin")
    _reload_skill_registry()
    return {"deleted": True}


class BuiltinSkillItem(BaseModel):
    """内置技能同步条目"""
    name: str
    slug: str
    system_prompt: str
    description: str = ""
    category: str = ""
    trigger_words: list[str] = Field(default_factory=list)


class SyncBuiltinSkillsRequest(BaseModel):
    """内置技能同步请求"""
    skills: list[BuiltinSkillItem]


@router.post("/skills/sync-builtin")
async def sync_builtin_skills(req: SyncBuiltinSkillsRequest):
    """同步内置技能到数据库

    - 按 slug upsert（保留用户已设置的 enabled 状态）
    - 清理文件系统已删除的孤儿技能（不在传入 slugs 中的）
    - 刷新 SkillRegistry 缓存

    前端在加载内置技能后调用此接口，让后端 Agent 也能匹配到内置技能。
    """
    incoming_slugs = set()
    upserted = 0
    for item in req.skills:
        incoming_slugs.add(item.slug)
        try:
            db.upsert_builtin_skill(
                name=item.name,
                slug=item.slug,
                system_prompt=item.system_prompt,
                description=item.description,
                category=item.category,
                trigger_words=item.trigger_words,
            )
            upserted += 1
        except Exception as e:
            logger.warning("同步内置技能 %s 失败: %s", item.slug, e)

    # 孤儿清理：数据库中存在但文件系统已删除的内置技能
    orphan_slugs = set(db.list_builtin_skill_slugs()) - incoming_slugs
    for slug in orphan_slugs:
        try:
            db.delete_builtin_skill_by_slug(slug)
        except Exception as e:
            logger.warning("清理孤儿内置技能 %s 失败: %s", slug, e)

    _reload_skill_registry()
    return {
        "upserted": upserted,
        "orphans_removed": len(orphan_slugs),
        "total": len(incoming_slugs),
    }
