"""S8-04：技能写侧的显式刷新必须绕过节流，失败不得被路由吞掉。

背景：reload() 原先对「上次失败 < 5s」直接 return，而写侧（禁用/删除/同步）
唯一的刷新手段就是调它。于是一次 DB 抖动之后，用户刚关掉/删掉的技能在
5s 窗口内根本不会重读；_loaded 仍为 True，读侧的 lazy 自愈也永不触发 ⇒
Agent 下一轮照样命中用户已经关掉的技能，删除路径甚至会把不存在的技能正文
喂给模型。
"""
import time

import database as db
import pytest

from skill_registry import SkillRegistry


def _skill(sid: str, enabled: int = 1) -> dict:
    return {
        "id": sid,
        "slug": sid,
        "name": sid,
        "enabled": enabled,
        "trigger_words": "[]",
        "priority": 0,
    }


def test_forced_reload_bypasses_retry_throttle(monkeypatch):
    """写侧强制刷新：节流窗口内也必须立刻重读，让禁用即时生效。"""
    reg = SkillRegistry()
    reg._loaded = True
    reg._skills = [_skill("sk-1", enabled=1)]
    reg._last_fail_time = time.monotonic()  # 刚失败过，处于节流窗口

    monkeypatch.setattr(db, "list_all_skills", lambda: [_skill("sk-1", enabled=0)])

    assert reg.reload(force=True) is True
    assert reg.get_enabled_skills("ssh") == []
    assert reg._last_fail_time == 0


def test_plain_reload_still_throttled(monkeypatch):
    """非强制刷新保留节流：窗口内连 DB 都不碰（防故障期间反复查询）。"""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("database is locked")

    reg = SkillRegistry()
    reg._last_fail_time = time.monotonic()
    monkeypatch.setattr(db, "list_all_skills", boom)

    assert reg.reload() is False
    assert calls["n"] == 0


def test_reload_reports_failure_and_keeps_old_cache(monkeypatch):
    """失败返回 False 且保留旧缓存（调用方据此把失败显式暴露出去）。"""
    reg = SkillRegistry()
    reg._loaded = True
    reg._skills = [_skill("sk-1")]
    reg._last_fail_time = 0

    monkeypatch.setattr(db, "list_all_skills", lambda: (_ for _ in ()).throw(RuntimeError("down")))

    assert reg.reload(force=True) is False
    assert [s["id"] for s in reg._skills] == ["sk-1"]
    assert reg._last_fail_time > 0


def test_route_refresh_failure_is_not_swallowed(monkeypatch):
    """路由写侧刷新失败必须转成 500，不能记一条 warning 就当成功。"""
    import skill_registry as reg_mod
    import routes.skills as skills_route
    from fastapi import HTTPException

    class _Failing:
        def reload(self, force: bool = False) -> bool:
            return False

    monkeypatch.setattr(reg_mod, "get_skill_registry", lambda: _Failing())

    with pytest.raises(HTTPException) as ei:
        skills_route._reload_skill_registry()
    assert ei.value.status_code == 500
    assert "刷新" in ei.value.detail
