"""
技能路由 — 技能 CRUD 与启用管理（支持按模式隔离）
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database as db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["skills"])


def _reload_skill_registry() -> None:
    """技能变更后强制刷新内存注册表缓存，确保 Agent 下次运行能看到最新技能。

    写侧必须绕过节流（force=True）：否则一次 DB 抖动后的 5s 窗口内，
    用户刚关掉/删掉的技能仍会继续被注入。刷新失败不能吞 —— 数据库已经
    写入成功而内存仍是旧状态，只回 200 等于告诉用户"已生效"。
    """
    from skill_registry import get_skill_registry
    if not get_skill_registry().reload(force=True):
        logger.error("技能已写入数据库，但刷新内存缓存失败")
        raise HTTPException(status_code=500, detail="技能变更已保存，但刷新技能缓存失败，请稍后重试")


class UpdateEnabledRequest(BaseModel):
    enabled: bool


@router.get("/skills")
def list_skills(limit: int = Query(default=200, ge=1, le=500),
                offset: int = Query(default=0, ge=0),
                mode: Optional[str] = None):
    """获取所有技能列表

    Args:
        mode: 若指定模式，则附加 per-mode 的 enabled 状态（mode_enabled 字段）。
              未指定 mode 时仅返回全局 enabled 状态。
    """
    skills = db.list_skills(limit=limit, offset=offset)
    if mode:
        disabled_ids = set(db.list_disabled_skills_for_mode(mode))
        for s in skills:
            # mode_enabled: 该技能在该模式下是否启用（全局 enabled=1 且未在该模式 disabled 列表中）
            s['mode_enabled'] = s['enabled'] == 1 and s['id'] not in disabled_ids
    return skills


@router.patch("/skills/{skill_id}/enabled")
def update_skill_enabled(
    skill_id: str,
    req: UpdateEnabledRequest,
    mode: Optional[str] = None,
):
    """更新技能启用状态

    Args:
        mode: 若指定模式，则更新该模式下的启用状态（skill_mode_config 表）。
              未指定 mode 时更新全局 enabled 状态。
    """
    # 取一次技能行：既做存在性校验，也供下方判断全局 enabled —— 原实现在
    # per-mode 分支里又 get_skill 一次，是对同一行的重复查询（中间
    # upsert_skill_mode_config 只写 skill_mode_config 表，不改 skills.enabled，
    # 复用首次读取安全）。per-mode 分支早期完全不校验存在性会写脏配置，故保留校验。
    existing = db.get_skill(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    enabled_val = 1 if req.enabled else 0
    if mode:
        # 按模式隔离：写入 skill_mode_config 表
        result = db.upsert_skill_mode_config(skill_id, mode, enabled_val)
        # 同时确保全局 enabled=1（否则 per-mode 启用无意义）
        if req.enabled and existing.get('enabled') != 1:
            db.update_skill_enabled(skill_id, 1)
        _reload_skill_registry()
        return result
    else:
        result = db.update_skill_enabled(skill_id, enabled_val)
        if not result:
            raise HTTPException(status_code=404, detail="Skill not found")
        _reload_skill_registry()
        return result


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str):
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
    enabled: int = Field(default=0, description="首次插入时的默认启用状态（已存在则保留用户选择）")


class SyncBuiltinSkillsRequest(BaseModel):
    """内置技能同步请求"""
    skills: list[BuiltinSkillItem]


@router.post("/skills/sync-builtin")
def sync_builtin_skills(req: SyncBuiltinSkillsRequest):
    """同步内置技能到数据库

    - 按 slug upsert（保留用户已设置的 enabled 状态）
    - 清理文件系统已删除的孤儿技能（不在传入 slugs 中的）
    - 刷新 SkillRegistry 缓存

    前端在加载内置技能后调用此接口，让后端 Agent 也能匹配到内置技能。
    """
    if not req.skills:
        # 空上报 ≠ 用户删光了技能：扫描 ~/.agents/skills 的逐项读取失败会被循环吞掉，
        # IPC 抖动或目录暂时不可读都会交出空列表。按孤儿清理语义走下去等于把已同步的
        # 全部用户目录内置技能（含 skill_mode_config 里的用户开关）一次性清空。
        logger.warning("内置技能同步收到空列表，跳过孤儿清理")
        return {"upserted": 0, "orphans_removed": 0, "total": 0}

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
                enabled_on_insert=item.enabled,
            )
            upserted += 1
        except Exception as e:
            logger.warning("同步内置技能 %s 失败: %s", item.slug, e)

    # 孤儿清理：数据库中存在但文件系统已删除的内置技能。
    # 包内技能（backend/skills_builtin，由 builtin_skills 模块管理）豁免 —
    # 前端只上报 ~/.agents/skills 目录内容，不排除会误删随包分发的技能。
    try:
        from builtin_skills import get_managed_slugs
        packaged_slugs = get_managed_slugs()
    except Exception:
        packaged_slugs = set()
    orphan_slugs = set(db.list_builtin_skill_slugs()) - incoming_slugs - packaged_slugs
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


@router.post("/skills/sync-packaged")
def sync_packaged_skills_endpoint():
    """手动触发包内技能同步（升级后 app 更新包内容但启动同步未跑时）"""
    try:
        from builtin_skills import sync_packaged_skills
        return sync_packaged_skills()
    except Exception as e:
        logger.warning("包内技能同步失败: %s", e)
        raise HTTPException(status_code=500, detail=f"包内技能同步失败: {e}")
