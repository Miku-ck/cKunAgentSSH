"""
打包内置技能加载器 — 随 app 分发的运维技能

backend/skills_builtin/<slug>/SKILL.md 是包内知识内容（extraResources 整目录
拷贝 backend/ 时自动随包）。启动时 upsert 进数据库（按 slug 幂等，保留用户
enabled 选择），并把 slug 集合交给 routes/skills 的孤儿清理作用域 —
前端 ~/.agents/skills 同步路径不会把这些包内技能当孤儿删掉。

frontmatter 解析用轻量正则（requirements 无 pyyaml）：只支持
`key: value` 与 `key: [a, b]` 两种形态，够 SKILL.md 规范使用。
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_SKILLS_DIR = Path(__file__).parent / "skills_builtin"

# 本模块负责（同步 + 孤儿清理豁免）的 slug 集合
_managed_slugs: set[str] = set()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (frontmatter 字典, 正文)。缺 frontmatter 时返回 ({}, 原文)。"""
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", text)
    if not m:
        return {}, text.strip()
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                # JSON 兼容 YAML 流式列表（要求元素带引号或为裸词时逐个加引号）
                inner = raw[1:-1].strip()
                items = []
                for part in inner.split(","):
                    p = part.strip()
                    if not p:
                        continue
                    if p[0] in "\"'" and p[-1] == p[0] and len(p) >= 2:
                        p = p[1:-1]
                    items.append(p)
                meta[key] = items
            except Exception:
                meta[key] = []
        else:
            if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
                raw = raw[1:-1]
            meta[key] = raw
    return meta, m.group(2).strip()


def scan_builtin_skills() -> list[dict]:
    """扫描包内技能目录，返回 [{slug,name,description,category,trigger_words,system_prompt}]"""
    results = []
    if not BUILTIN_SKILLS_DIR.is_dir():
        return results
    for skill_dir in sorted(BUILTIN_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = next((skill_dir / n for n in ("SKILL.md", "skill.md") if (skill_dir / n).is_file()), None)
        if md is None:
            continue
        try:
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[builtin_skills] 读取 %s 失败: %s", md, e)
            continue
        name = meta.get("name") or skill_dir.name
        if not body:
            logger.warning("[builtin_skills] %s 正文为空，跳过", md)
            continue
        tw = meta.get("trigger_words") or []
        results.append({
            "slug": meta.get("slug") or skill_dir.name,
            "name": name,
            "description": meta.get("description", ""),
            "category": meta.get("category", "内置"),
            "trigger_words": [str(w) for w in tw] if isinstance(tw, list) else [],
            "system_prompt": body,
        })
    return results


def sync_packaged_skills() -> dict:
    """启动同步：upsert 包内技能 + 首次插入即启用（运维内置技能开箱可用）。

    返回 {upserted, slugs}。数据库不可用时只 warning，不阻塞启动。
    """
    import database as db

    skills = scan_builtin_skills()
    if not skills:
        # 空扫描 ≠ 包内技能被删光了：BUILTIN_SKILLS_DIR 不存在/不可读（打包漏带目录、
        # 升级中途文件占用）同样交出空列表。按孤儿清理语义走下去 = 把上一版同步过的
        # 全部内置技能连用户在 skill_mode_config 里的开关一起删干净，且 _persist 之后
        # 这次误删还会被固化。与 routes/skills.py 的 sync-builtin 空上报保护同源。
        logger.warning("[builtin_skills] 包内技能扫描为空，跳过同步与孤儿清理")
        return {"upserted": 0, "slugs": sorted(_managed_slugs)}
    _managed_slugs.clear()
    upserted = 0
    for s in skills:
        try:
            db.upsert_builtin_skill(
                name=s["name"],
                slug=s["slug"],
                system_prompt=s["system_prompt"],
                description=s["description"],
                category=s["category"],
                trigger_words=s["trigger_words"],
                enabled_on_insert=1,
            )
            _managed_slugs.add(s["slug"])
            upserted += 1
        except Exception as e:
            logger.warning("[builtin_skills] 同步 %s 失败: %s", s["slug"], e)

    # 孤儿清理仅限本模块此前管理过的 slug（升级后删除的包内技能）
    try:
        orphans = set(db.list_builtin_skill_slugs()) & _previous_managed_slugs() - _managed_slugs
        for slug in orphans:
            db.delete_builtin_skill_by_slug(slug)
    except Exception as e:
        logger.warning("[builtin_skills] 孤儿清理失败（不阻塞）: %s", e)

    _persist_managed_slugs()
    if upserted:
        try:
            from skill_registry import get_skill_registry
            get_skill_registry().reload()
        except Exception as e:
            logger.warning("[builtin_skills] 注册表刷新失败: %s", e)
    logger.info("[builtin_skills] 包内技能同步完成：%d 条", upserted)
    return {"upserted": upserted, "slugs": sorted(_managed_slugs)}


_STATE_FILE = Path.home() / ".ckunagentic" / "builtin_skills_state.json"


def _previous_managed_slugs() -> set[str]:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("slugs", []))
    except Exception:
        return set()


def _persist_managed_slugs() -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps({"slugs": sorted(_managed_slugs)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[builtin_skills] 状态文件写入失败: %s", e)


def get_managed_slugs() -> set[str]:
    """当前包内技能 slug 集合（孤儿清理豁免名单）"""
    return set(_managed_slugs)
