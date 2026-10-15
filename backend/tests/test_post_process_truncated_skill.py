"""技能 JSON 截断钝化解析回归 — LLM 输出被切在字符串中途也尽量不整条丢技能。

背景：本地小模型写长 system_prompt 时，max_tokens 不足会把输出切在
`"system_prompt": "# ...` 字符串中途，正常 json.loads 抛 Unterminated string，
于是整条技能（连已完整的 name/slug/description）被整条丢弃、自学习静默失败。

运行: python -m pytest backend/tests/test_post_process_truncated_skill.py -v
"""
from post_processor import (
    _parse_skills_lenient,
    _clean_trigger_words,
)


def _skill(name="技能A", slug="skill-a", description="描述A", system_prompt="x" * 60):
    return f'{{"name": "{name}", "slug": "{slug}", "description": "{description}", ' \
           f'"system_prompt": "{system_prompt}", "trigger_words": ["a", "b"]}}'


def _wrapped(*skills):
    return '{"skills": [' + ", ".join(skills) + "]}"


# ── 1. 完整正常 JSON ─────────────────────────────────────────────────────────


def test_lenient_parses_complete_json():
    text = _wrapped(_skill("技能A", "skill-a"))
    skills = _parse_skills_lenient(text)
    assert len(skills) == 1
    assert skills[0]["name"] == "技能A"
    assert skills[0]["slug"] == "skill-a"
    # 顶层列表/魔改形状也应保持既有语义
    assert _parse_skills_lenient(_skill("B", "skill-b"))[0]["name"] == "B"


# ── 2. 截断在 system_prompt 字符串中途（核心场景）────────────────────────────


def test_lenient_rescues_skill_when_system_prompt_truncated():
    """name/slug/description 完整 + 仅 system_prompt 被截断 → 至少救回一条。

    这是本修复的核心价值：正文写到一半被掐断，也不该把已完成的字段一起扔了。
    """
    text = ('{"skills": [{"name": "Nginx 日志排查", "slug": "nginx-log-triage", '
            '"description": "按状态码定位日志异常", '
            '"system_prompt": "第一步 检查错误日志地址 # 一段还没写完的正文')
    skills = _parse_skills_lenient(text)
    assert len(skills) == 1
    assert skills[0]["name"] == "Nginx 日志排查"
    assert skills[0]["slug"] == "nginx-log-triage"
    # 修复后补出了可解析的正文（即便内容是截断后的一段）
    assert skills[0]["system_prompt"].startswith("第一步")


def test_lenient_rescues_leading_complete_skill_then_truncated():
    """skills[0] 完整、skills[1] 中途被截断 → 也不能把 skills[0] 扔掉。"""
    text = _wrapped(_skill("已完整技能", "done-skill")) + (
        ', {"name": "被截断", "slug": "cut", "description": "d", '
        '"system_prompt": "写到一半'
    )
    skills = _parse_skills_lenient(text)
    assert len(skills) == 1
    assert skills[0]["name"] == "已完整技能"


# ── 3. 截断/逗号串 trigger_words 仍能归一 ────────────────────────────────────


def test_cleaned_trigger_words_from_recovered_skill():
    """救回的技能若带签名+逗号串触发词，仍应被 _clean_trigger_words 归一。"""
    text = ('{"skills": [{"name": "部署", "slug": "deploy-k8s", "description": "d", '
            '"system_prompt": "' + "x" * 60 + '", '
            '"trigger_words": "部署,发布,k8s"}]}')
    skills = _parse_skills_lenient(text)
    assert skills
    assert _clean_trigger_words(skills[0]["trigger_words"]) == ["部署", "发布", "k8s"]


def test_cleaned_trigger_words_from_truncated_skill():
    """system_prompt 与 trigger_words 数组都被截断 → 触发词仍走既有清洗语义。"""
    text = ('{"skills": [{"name": "部署", "slug": "deploy-k8s", "description": "d", '
            '"system_prompt": "' + "x" * 60 + '", '
            '"trigger_words": ["部署", "发布"')
    skills = _parse_skills_lenient(text)
    assert skills
    assert _clean_trigger_words(skills[0]["trigger_words"]) == ["部署", "发布"]


# ── 4. 完全不可解析的乱文本 ──────────────────────────────────────────────────


def test_lenient_returns_empty_on_garbage_without_raising():
    assert _parse_skills_lenient("not json {{{ at all") == []
    assert _parse_skills_lenient("") == []
    assert _parse_skills_lenient('{"skills": "not a list"}') == []