# -*- coding: utf-8 -*-
"""第 13c 轮回归：意图识别 / 成本定价 / 技能触发与禁用 / 审计结论 / 反思→演化输入 / LLM 客户端池。

覆盖的缺陷家族：
- 「一个谓词两处定义」：意图预算钳制（classify 路径钳、逐轮路径不钳）
- 「子串当成模型系列」：裸 "gpt-4" 命中 gpt-4.1 → 成本放大十几倍 → 误熔断
- 「正则边界在中文里失效」：\\b502\\b 匹配不到「网站返回502错误」
- 「守护在最需要它时形同虚设」：审计结论白名单漏项 / 空扫描清空包内技能 /
  禁用清单读不出时当成「没人被禁用」
- 「按分数过滤的视图喂给了要看失败样本的读者」：演化输入复用高分反思视图
- 「计数器按端点键控却被当成按对象键控」：LRU 淘汰清零在途计数 → 掐断新客户端的流

运行: python -m pytest backend/tests/test_ai_loop_round13c.py -v
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import database as db
from agent import audit
from agent.cost_tracker import calc_cost, get_model_pricing, _DEFAULT_PRICING
from agent.intention import (
    IntentionLevel,
    IntentionResult,
    TaskType,
    _compute_effective_budgets,
    _LEVEL_BUDGET,
    classify,
    detect_batch_reading,
    get_reasoning_budget_for_iteration,
)


# ── 意图识别：只读提问不该被抬进「首轮只规划不调工具」的 complex ────────────
def test_classify_readonly_system_query_not_complex():
    """「查看系统负载」曾被「系统」这个日常词命中复杂度信号 → complex（4096 思考 + 纯规划轮）。"""
    r = classify("查看系统负载")
    assert r.level is not IntentionLevel.COMPLEX
    assert r.allow_planning_phase is False


def test_classify_query_with_global_scope_caps_at_moderate():
    """「查看整个目录的日志」需要多走几步，不需要先写计划 → 封顶 moderate。"""
    r = classify("查看整个目录的日志")
    assert r.task_type is TaskType.QUERY
    assert r.level is IntentionLevel.MODERATE
    assert r.allow_planning_phase is False


def test_classify_chinese_substring_markers_no_single_char_match():
    """「调整任务优先级」：单字「先」曾命中多步骤信号（优先/领先/率先）→ 虚增一级。"""
    r = classify("调整任务优先级")
    assert r.task_type is TaskType.MODIFY
    assert r.level is IntentionLevel.SIMPLE


def test_classify_non_query_complexity_still_reaches_complex():
    """反向保护：非只读任务命中复杂度信号仍应到 complex（不能把修复做成单向降级）。"""
    r = classify("实现整个模块的鉴权架构")
    assert r.task_type is TaskType.IMPLEMENT
    assert r.level is IntentionLevel.COMPLEX
    assert r.allow_planning_phase is True


# ── 逐轮思考预算必须钳在模型上限内（超限 → 400 → 整个 run 失败）────────────
def _intent(budget: int, planning: bool = False) -> IntentionResult:
    return IntentionResult(
        level=IntentionLevel.COMPLEX,
        task_type=TaskType.QUERY,
        reasoning_budget=budget,
        allow_planning_phase=planning,
        guide_prompt="",
    )


@pytest.mark.parametrize("iteration", [0, 1, 3, 4, 9])
def test_reasoning_budget_for_iteration_respects_model_cap(iteration):
    got = get_reasoning_budget_for_iteration(_intent(4096, planning=True), iteration,
                                            max_budget=1024)
    assert got is not None and got <= 1024


def test_reasoning_budget_for_iteration_unchanged_without_cap():
    """不传 max_budget（未配置模型上限）时保持原有的比例分配，不额外收紧。"""
    assert get_reasoning_budget_for_iteration(_intent(4096, planning=True), 0) == 4096
    assert get_reasoning_budget_for_iteration(_intent(4096), 0) == 2867
    assert get_reasoning_budget_for_iteration(_intent(4096), 2) == 2048
    assert get_reasoning_budget_for_iteration(_intent(4096), 5) == 1638


def test_reasoning_budget_none_means_unlimited():
    """reasoning_budget=None（模型未设思考上限）时逐轮也返回 None，不做任何钳制。"""
    unlimited = IntentionResult(
        level=IntentionLevel.TRIVIAL,
        task_type=TaskType.QUERY,
        reasoning_budget=None,
        allow_planning_phase=False,
        guide_prompt="",
    )
    assert get_reasoning_budget_for_iteration(unlimited, 3, max_budget=1024) is None


# ── 大预算模型：等级预算只放大不缩小 ───────────────────────────────────────
def test_effective_budgets_only_scale_up():
    """8192 上限的模型，trivial 曾被 0.1% 缩到 128（低于固定值 256）—— 方向是反的。"""
    b = _compute_effective_budgets(8192)
    for level, fixed in _LEVEL_BUDGET.items():
        assert b[level] >= fixed
    assert b[IntentionLevel.TRIVIAL] >= 256
    assert b[IntentionLevel.SIMPLE] >= 512
    # 小上限/未配置时沿用固定表
    assert _compute_effective_budgets(4096) == _LEVEL_BUDGET
    assert _compute_effective_budgets(None) == _LEVEL_BUDGET


# ── 批量读取识别：ssh 模式的 file_* 也是只读工具 ───────────────────────────
def _calls(*names):
    return [SimpleNamespace(name=n) for n in names]


def test_detect_batch_reading_counts_file_read_tools():
    """修复前 _READ_TOOLS 只有 web_*（拆包前的旧工具名集合）→ 一口气读 5 个文件永不触发引导。"""
    assert detect_batch_reading(_calls("file_read", "file_read", "file_grep")) is True
    assert detect_batch_reading(_calls("file_read", "log_read", "web_fetch")) is True
    assert detect_batch_reading(_calls("file_read", "file_read")) is False
    assert detect_batch_reading(_calls("shell_exec", "file_edit", "service_manage")) is False


# ── 模型定价边界：子串不得跨型号命中 ───────────────────────────────────────
@pytest.mark.parametrize("model_id,expected", [
    ("gpt-4o-mini",       (0.15, 0.60)),
    ("gpt-4o",            (2.50, 10.00)),
    ("chatgpt-4o-latest", (2.50, 10.00)),
    ("gpt-4-turbo",       (10.00, 30.00)),
    ("gpt-4",             (30.00, 60.00)),
    ("deepseek-chat",     (0.14, 0.28)),
    ("claude-3-5-haiku",  (0.80, 4.00)),
    ("qwen-max",          (2.50, 10.00)),
])
def test_pricing_hits_expected_row(model_id, expected):
    assert get_model_pricing(model_id) == expected


@pytest.mark.parametrize("model_id", [
    "gpt-4.1", "gpt-4.1-mini", "gpt-45",
    "Qwen/Qwen3-VL-8B-Instruct", "",
])
def test_pricing_unknown_families_fall_back_to_default(model_id):
    """裸 "gpt-4" 曾子串命中 gpt-4.1/gpt-45 → $30/$60（约 15 倍）→ $2 熔断误杀 run。"""
    assert get_model_pricing(model_id) == _DEFAULT_PRICING


def test_pricing_dash_still_counts_as_boundary():
    """'-' 保留为边界是有意的：deepseek-chat 归属 deepseek 行，
    gpt-4-1106-preview 归属 gpt-4 行（家族级粗粒度，宁可归到家族老价也不散落到 fallback）。"""
    assert get_model_pricing("deepseek-chat") == (0.14, 0.28)
    assert get_model_pricing("deepseek-reasoner") == (0.14, 0.28)
    assert get_model_pricing("gpt-4-1106-preview") == (30.00, 60.00)


def test_calc_cost_does_not_inflate_newer_models():
    """1M 输入 token 的 gpt-4.1 按默认价 ≈ $0.5，而非旧 GPT-4 的 $30。"""
    assert calc_cost(1_000_000, 0, "gpt-4.1") == pytest.approx(0.5)
    assert calc_cost(0, 1_000_000, "gpt-4.1") == pytest.approx(1.5)


# ── 触发词正则：中文相邻的 ASCII 词要能命中，同时不误伤长词 ────────────────
def test_trigger_pattern_matches_ascii_inside_chinese():
    """re.compile(r'\\b502\\b').search("网站返回502错误") → None：Python 的 \\w 含中文。"""
    from skill_registry import SkillRegistry
    p = SkillRegistry._compile_trigger_pattern
    assert p("502").search("网站返回502错误") is not None
    assert p("nginx").search("nginx配置需要重载") is not None
    assert p("CPU").search("服务器CPU占用90%") is not None
    # 相邻 ASCII/下划线仍是边界：git 不命中 digit/gitlab，但命中 git status
    assert p("git").search("digit") is None
    assert p("git").search("gitlab ci") is None
    assert p("git").search("git status") is not None
    assert p("502").search("http 502bad") is None
    # 中文短语走转义 substring
    assert p("服务重启").search("请先执行服务重启操作") is not None
    assert p("").search("任意文本") is None


# ── 禁用清单读取失败时不得当成「没人被禁用」──────────────────────────────
def test_disabled_ids_fall_back_to_last_good_read(monkeypatch):
    from skill_registry import SkillRegistry
    reg = SkillRegistry()
    n = {"i": 0}

    def fake(mode):
        n["i"] += 1
        if n["i"] == 1:
            return ["sk-1", "sk-2"]
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "list_disabled_skills_for_mode", fake)
    assert reg._disabled_ids_for_mode("ssh") == {"sk-1", "sk-2"}
    assert reg._disabled_ids_for_mode("ssh") == {"sk-1", "sk-2"}
    # 其他 mode 无缓存 → 空集（无从判断时不做删除动作）
    assert reg._disabled_ids_for_mode("chat") == set()


def test_get_enabled_skills_uses_disabled_cache(monkeypatch):
    from skill_registry import SkillRegistry
    reg = SkillRegistry()
    reg._skills = [
        {"id": "a", "enabled": 1}, {"id": "b", "enabled": 1}, {"id": "c", "enabled": 0},
    ]
    reg._loaded = True
    monkeypatch.setattr(db, "list_disabled_skills_for_mode", lambda m: ["b"])
    assert [s["id"] for s in reg.get_enabled_skills("ssh")] == ["a"]
    # DB 故障：沿用上次成功结果，用户关掉的 b 依然不回来
    def boom(m):
        raise RuntimeError("down")
    monkeypatch.setattr(db, "list_disabled_skills_for_mode", boom)
    assert [s["id"] for s in reg.get_enabled_skills("ssh")] == ["a"]


# ── 审计结论白名单：澄清向导的两个终态不得被改写 ──────────────────────────
def _audit_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(audit._AUDIT_SCHEMA)
    return conn


@pytest.mark.parametrize("conclusion", ["answered", "skipped"])
def test_clarification_conclusion_preserved(monkeypatch, conclusion):
    """修复前二者不在白名单 → 落 "executed"，审计页把「已作答/已跳过」显示成「执行」。"""
    conn = _audit_conn()
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    rec = audit.append_record(
        action=f"clarification_{conclusion}:run1",
        conclusion=conclusion,
        target_type="run",
        target_id="run1",
        result="ok",
    )
    assert rec is not None
    row = conn.execute(
        "SELECT conclusion FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["conclusion"] == conclusion


def test_audit_conclusion_whitelist_matches_frontend_list():
    """前端结论下拉与后端白名单必须同源：前端可筛而后端写不进的项 = 永远为空的筛选，
    后端能写而前端筛不出的项 = 审计页里看不见的记录。"""
    import re
    from pathlib import Path

    audit_src = Path(audit.__file__).read_text(encoding="utf-8")
    m = re.search(r"if conclusion not in \((.*?)\):", audit_src, re.S)
    assert m, "白名单写法变化，请同步本测试"
    written = set(re.findall(r'"([^"]+)"', m.group(1)))

    vue = Path(audit.__file__).parents[2] / "src" / "views" / "ssh" / "AuditView.vue"
    block = re.search(r'<select v-model="filters\.conclusion".*?</select>',
                      vue.read_text(encoding="utf-8"), re.S).group(0)
    shown = {v for v in re.findall(r'<option value="([a-z_]+)"', block) if v}

    assert shown == written
    assert {"answered", "skipped", "released", "revoked"} <= shown


# ── 包内技能空扫描不得当成「全删」 ────────────────────────────────────────
def test_sync_packaged_skills_skips_orphan_cleanup_on_empty_scan(monkeypatch):
    import builtin_skills as bs
    calls = {"list": 0, "delete": 0, "persist": 0}

    def fake_list():
        calls["list"] += 1
        return ["nginx-troubleshoot"]

    def fake_delete(slug):
        calls["delete"] += 1

    monkeypatch.setattr(bs, "scan_builtin_skills", lambda: [])
    monkeypatch.setattr(bs, "_managed_slugs", {"keep-me"})
    monkeypatch.setattr(bs, "_previous_managed_slugs", lambda: {"nginx-troubleshoot"})
    monkeypatch.setattr(bs, "_persist_managed_slugs", lambda: calls.__setitem__(
        "persist", calls["persist"] + 1))
    monkeypatch.setattr(db, "list_builtin_skill_slugs", fake_list)
    monkeypatch.setattr(db, "delete_builtin_skill_by_slug", fake_delete)

    out = bs.sync_packaged_skills()
    assert out["upserted"] == 0
    # 目录读不出来（打包漏带/升级占用）时把上一版同步的技能连用户开关一起删光
    assert calls == {"list": 0, "delete": 0, "persist": 0}
    assert bs.get_managed_slugs() == {"keep-me"}


def test_sync_packaged_skills_still_cleans_orphans_when_scan_ok(monkeypatch):
    """反向保护：真删掉的包内技能仍需被孤儿清理回收。"""
    import builtin_skills as bs
    deleted = []

    monkeypatch.setattr(bs, "scan_builtin_skills", lambda: [{
        "name": "n", "slug": "new-skill", "system_prompt": "p",
        "description": "d", "category": "内置", "trigger_words": [],
    }])
    monkeypatch.setattr(bs, "_managed_slugs", set())
    monkeypatch.setattr(bs, "_previous_managed_slugs", lambda: {"gone-skill"})
    monkeypatch.setattr(bs, "_persist_managed_slugs", lambda: None)
    monkeypatch.setattr(db, "list_builtin_skill_slugs", lambda: ["gone-skill", "new-skill"])
    monkeypatch.setattr(db, "delete_builtin_skill_by_slug", lambda s: deleted.append(s))
    monkeypatch.setattr(db, "upsert_builtin_skill", lambda **kw: None)

    out = bs.sync_packaged_skills()
    assert out["upserted"] == 1
    assert deleted == ["gone-skill"]


# ── 演化引擎要看到失败样本 ────────────────────────────────────────────────
def test_evolution_input_keeps_low_score_reflections(monkeypatch):
    from agent.reflection import MetaReflection
    rows = [
        {"id": "r1", "score": 0.9, "enabled": 1},
        {"id": "r2", "score": 0.2, "enabled": 1},   # 失败运行
        {"id": "r3", "score": 0.7, "enabled": 0},   # 已被演化下架
    ]
    monkeypatch.setattr(db, "get_agent_strategies",
                        lambda mode, strategy_type, limit: list(rows))
    mr = MetaReflection()
    ev = mr.load_reflections_for_evolution("ssh", limit=10)
    assert [r["id"] for r in ev] == ["r1", "r2"]
    # avg_score 现在可以 < 0.5（规则 6「综合分低 → 打开 enable_thinking」不再结构性不可达）
    assert min(r["score"] for r in ev) < 0.5


def test_evolution_input_survives_db_error(monkeypatch):
    from agent.reflection import MetaReflection

    def boom(mode, strategy_type, limit):
        raise RuntimeError("locked")

    monkeypatch.setattr(db, "get_agent_strategies", boom)
    assert MetaReflection().load_reflections_for_evolution("ssh") == []


# ── LRU 淘汰不能把在途计数清零（否则同 key 重建的客户端会被中途 aclose）───
class _FakePool:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _FakeClient:
    """替掉真 openai 客户端：只关心「淘汰时有没有被 aclose」"""

    def __init__(self, *args, **kwargs):
        self._client = _FakePool()


def test_lru_eviction_preserves_inflight_counter(monkeypatch):
    import llm_adapter as la

    cache_snapshot = dict(la._client_cache)
    flight_snapshot = dict(la._client_in_flight)
    la._client_cache.clear()
    la._client_in_flight.clear()
    monkeypatch.setattr(la, "_CLIENT_CACHE_MAX", 1)
    # openai 现为惰性导入（_openai() 返回 sys.modules['openai']），直接 patch 模块本体
    import openai as _openai_mod
    monkeypatch.setattr(_openai_mod, "AsyncOpenAI", _FakeClient)

    async def scenario():
        key_a = ("http://a.test/v1", "kA")
        key_b = ("http://b.test/v1", "kB")

        a = await la._get_client(*key_a)          # 客户端 A
        la._client_in_flight[key_a] = 1           # A 上 1 条在途流（chat_stream 进入时计数）
        b = await la._get_client(*key_b)          # 超容量 → 淘汰 A
        assert a._client.closed is False          # 有在途流 → 跳过 aclose
        assert b._client.closed is False
        # 修复点：这里若 pop 计数，A 的流仍在跑却被记成「无人在用」
        assert la._client_in_flight.get(key_a) == 1

        a2 = await la._get_client(*key_a)         # 同 key 重建为 A'（淘汰 B，B 空闲 → 关闭）
        assert b._client.closed is True
        la._client_in_flight[key_a] += 1          # A' 开始一条流
        assert la._client_in_flight[key_a] == 2
        la._client_in_flight[key_a] = max(
            0, la._client_in_flight.get(key_a, 0) - 1)  # A 的流结束（finally 递减）
        # 修复前这里是 0：A' 被淘汰时会走「无在途」分支被 aclose，掐断 A' 仍在跑的流
        assert la._client_in_flight[key_a] == 1
        assert a2._client.closed is False

    try:
        asyncio.run(scenario())
    finally:
        la._client_cache.clear()
        la._client_cache.update(cache_snapshot)
        la._client_in_flight.clear()
        la._client_in_flight.update(flight_snapshot)


def test_chat_stream_decrements_inflight_on_consumer_disconnect():
    """消费者提前 break 时，生成器 finally 必须把计数扣回去（否则客户端永不关闭）。"""
    import llm_adapter as la

    async def scenario():
        async def fake_impl(*a, **kw):
            yield la.StreamChunk(content="x")
            yield la.StreamChunk(content="y")

        def fake_stream(*a, **kw):
            return fake_impl()

        orig = la._chat_stream_impl
        la._chat_stream_impl = fake_stream
        try:
            gen = la.chat_stream("http://c.test/v1", "kC", "m", [{"role": "user",
                                                                  "content": "hi"}])
            async for _ in gen:
                break
            await gen.aclose()
            key = ("http://c.test/v1", "kC")
            assert la._client_in_flight.get(key, 0) == 0
        finally:
            la._chat_stream_impl = orig

    asyncio.run(scenario())
