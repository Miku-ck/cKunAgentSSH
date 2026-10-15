"""后处理 LLM 产出形状回归 — 记忆/技能提取不得因 JSON 形状偏离而静默丢失。

自演化链路的输入是本地小模型的自由 JSON，形状不受控。原实现按
「一定是对象 + 字段一定是数组/字符串」写死，四种常见偏离会让整轮提取
静默失败（异常被外层 except 吞掉，日志只有一行 exception，用户侧表现为
「技能生成了但永不触发」或直接没有技能）：

  1. trigger_words 写成逗号分隔字符串（最高频）
  2. 顶层直接回数组而非 {"skills": [...]}
  3. 数组字段显式为 null（.get(k, []) 的默认值只对缺失键生效）
  4. 文本字段为 null / 非字符串 → .strip() AttributeError

运行: python -m pytest backend/tests/test_post_process_llm_shapes.py -v
"""
import json
import os

import pytest

import database as db
from post_processor import (
    PostProcessConfig,
    _clean_trigger_words,
    _create_auto_skill_with_quality_gate,
    _llm_list,
)


@pytest.fixture
def base_db(tmp_workspace):
    db_path = os.path.join(tmp_workspace, "pp_shapes.db")
    db._close_all_connections()
    db.init_db(db_path)
    yield db_path
    db._close_all_connections()


def _skill_dict(**kw) -> dict:
    base = {
        "name": "Nginx 日志排查",
        "slug": "nginx-log-triage",
        "description": "按状态码定位 Nginx 日志异常",
        "system_prompt": "x" * 120,  # 过质量门（>=50 字）
        "trigger_words": ["nginx", "日志"],
    }
    base.update(kw)
    return base


# ── trigger_words ────────────────────────────────────────────────────────────


def test_trigger_words_comma_string_is_split():
    """模型回单串逗号分隔触发词时，仍应得到多个词而非空列表。"""
    assert _clean_trigger_words("部署,发布,k8s") == ["部署", "发布", "k8s"]
    assert _clean_trigger_words("部署、发布；k8s") == ["部署", "发布", "k8s"]


def test_trigger_words_list_behaviour_unchanged():
    """数组入参的既有清洗语义（长度门/字符门/去重/截断）不受影响。"""
    assert _clean_trigger_words(["nginx", "Nginx", "a", "docker-compose"]) == [
        "nginx",
        "docker-compose",
    ]
    assert len(_clean_trigger_words([f"w{i}d{i}" for i in range(10)])) == 5


def test_trigger_words_garbage_types_yield_empty():
    assert _clean_trigger_words(None) == []
    assert _clean_trigger_words({"a": 1}) == []


# ── 顶层 / 数组字段形状 ──────────────────────────────────────────────────────


def test_llm_list_accepts_top_level_array():
    assert _llm_list([{"name": "s"}], "skills") == [{"name": "s"}]


def test_llm_list_tolerates_null_and_scalar():
    assert _llm_list({"memories": None}, "memories") == []
    assert _llm_list({"memories": "一条记忆"}, "memories") == []
    assert _llm_list("not json object", "memories") == []
    assert _llm_list(None, "memories") == []
    assert _llm_list({"memories": ["a"]}, "memories") == ["a"]


# ── 建技能：字段形状与落库 ───────────────────────────────────────────────────


def test_null_text_fields_do_not_lose_the_skill(base_db):
    """slug/description 为 null 时仍应按 name 兜底建技能，而不是整条抛异常。"""
    created = _create_auto_skill_with_quality_gate(
        # 没有 mode 形参：db.create_skill 从来没有 mode 列，旧测试多传了一个
        # 「ssh」，函数改签名后这三条用例就一直在 TypeError 上红着。
        _skill_dict(name="Nginx Log Triage", slug=None, description=None),
        PostProcessConfig(),
    )
    assert created is not None
    assert created["slug"] == "nginx-log-triage"  # name 规范化兜底
    assert created["description"] == ""


def test_non_string_system_prompt_rejected_not_fabricated(base_db):
    """system_prompt 回数组不得被 str() 伪造成通过长度门的正文。"""
    assert _create_auto_skill_with_quality_gate(
        _skill_dict(system_prompt=["第一步", "第二步"]), PostProcessConfig()
    ) is None


def test_auto_skill_persists_split_trigger_words(base_db):
    """端到端：逗号串触发词必须真正落到 skills 行里，否则技能永不触发。"""
    created = _create_auto_skill_with_quality_gate(
        _skill_dict(trigger_words="nginx,日志,502"), PostProcessConfig()
    )
    assert created is not None
    row = db.get_skill_by_slug(created["slug"])
    assert json.loads(row["trigger_words"]) == ["nginx", "日志", "502"]
    # 触发词必须能命中匹配（原实现此处为空 → 技能成为不可达数据）
    from skill_registry import SkillRegistry
    assert SkillRegistry._compile_trigger_pattern("502").search("帮我看下 502 报错")
