"""意图识别新增的「变更类」维度（is_change）测试。

背景：`is_change` 与 `TaskType` 正交 —— task_type 说"做什么性质的事"，
is_change 说"有没有副作用"。它驱动运维方法论二选一注入
（OPS_METHOD_READONLY / OPS_METHOD_CHANGE）以及逐轮变更纪律提醒。

本文件同时锁住两条不可回退的性质：
1. 只读取证任务不得被判为变更（否则每次查日志都被要求给回滚方案）；
2. 变更动词不得被"纯对话"快速路径吞掉（"谢谢，重启一下"不是寒暄）。
"""
import pytest

from agent.intention import (
    IntentionLevel,
    IntentionResult,
    TaskType,
    _CHANGE_DISCIPLINE,
    classify,
    get_guide_for_iteration,
    reclassify,
)


# ── 1. 变更判定 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("task", [
    "重启 nginx",
    "重启一下 nginx 并确认端口在听",
    "帮我部署这个项目到生产环境",
    "删除 /tmp 下的临时文件",
    "清理一下 docker 无用镜像",
    "回滚到上一个版本",
    "备份一下 /var/www 目录",
    "把 redis 配置文件里的 maxmemory 改成 2gb",
    "实现一个日志清理脚本",
    "重构这个部署脚本",
    "pip install -r requirements.txt",
    "apt-get update 然后装 nginx",
    "systemctl stop firewalld",
    "修复 nginx 配置里的错误",
    "git commit 提交这次改动",
])
def test_change_tasks_flagged(task):
    assert classify(task).is_change is True


@pytest.mark.parametrize("task", [
    "看看系统情况",
    "看看磁盘还有多少空间",
    "为什么 nginx 起不来",
    "排查为什么 mysql 服务挂了",
    "分析下 nginx 的错误日志",
    "看看这个文件的内容",
    "读取 /etc/hosts",
    "检查一下 CPU 占用",
    "执行 df -h",
    "list files in /etc",
    "查一下 8080 端口谁在听",
])
def test_readonly_tasks_not_flagged(task):
    assert classify(task).is_change is False


def test_debug_with_change_verb_is_change():
    """调试本身是只读取证；但"修复"是变更动作 → 二者叠加即为变更"""
    assert classify("修复 nginx 配置里的错误").is_change is True
    assert classify("为什么 nginx 起不来").is_change is False


def test_convert_synonyms_not_treated_as_change():
    """"执行 X"本身不构成变更信号（"执行 df -h" 是纯只读）"""
    assert classify("执行一下 ls -la").is_change is False


@pytest.mark.parametrize("task", ["你好", "你是什么模型", "你能做什么", "谢谢"])
def test_conversation_not_change(task):
    r = classify(task)
    assert r.task_type is TaskType.CONVERSATION
    assert r.is_change is False


def test_greeting_plus_change_verb_is_not_conversation():
    """变更动词必须进入 has_action_signal，否则会被纯对话快速路径吞掉"""
    r = classify("谢谢，重启一下 nginx")
    assert r.task_type is not TaskType.CONVERSATION
    assert r.is_change is True


def test_conversation_fast_path_still_works_without_change_verb():
    """回归守护：新增变更信号不得破坏原有纯对话识别"""
    assert classify("你好呀").task_type is TaskType.CONVERSATION
    assert classify("hello").task_type is TaskType.CONVERSATION


def test_empty_task_defaults_to_not_change():
    r = classify("")
    assert r.is_change is False
    assert r.level is IntentionLevel.TRIVIAL


def test_change_flag_does_not_disturb_existing_level_and_budget():
    """回归守护：本次改动不触碰 TaskType / IntentionLevel / reasoning 预算"""
    ro = classify("读取 /etc/hosts")
    assert ro.task_type is TaskType.QUERY
    assert ro.level is IntentionLevel.TRIVIAL
    assert ro.reasoning_budget == 256


def test_to_dict_exposes_is_change():
    d = classify("重启 nginx").to_dict()
    assert d["is_change"] is True
    assert classify("看看系统情况").to_dict()["is_change"] is False


def test_is_change_defaults_false_on_dataclass():
    """字段必须带默认值 —— 其它构造点（含测试/第三方）不必同步改造"""
    r = IntentionResult(
        level=IntentionLevel.TRIVIAL,
        task_type=TaskType.QUERY,
        reasoning_budget=256,
        allow_planning_phase=False,
        guide_prompt="",
    )
    assert r.is_change is False


# ── 2. reclassify 透传 ─────────────────────────────────────────────────────

def test_reclassify_preserves_is_change_on_change():
    original = classify("重启 nginx")
    assert original.is_change is True
    # 本例只关心「重分类换了对象时 is_change 是否透传」，触发信号用**仍在的**升级
    # 分支（连续失败 ≥3 ⇒ TRIVIAL→MODERATE）。原实现用的是 remaining≤3 的紧急度
    # 提升分支，那条已在 aca7427 作为死码删除（driver 每 reclassify_interval=5 轮
    # 才调本函数，max_iterations=100 时永不命中），测试当时没跟着改 ⇒ master 红。
    log = [{"success": False} for _ in range(5)]
    new = reclassify(
        original, iteration=5, tool_calls_log=log, errors=["boom"] * 5, max_iterations=20,
    )
    assert new is not original
    assert new.level is not original.level
    assert new.is_change is True


def test_reclassify_returns_same_object_when_unchanged():
    original = classify("重启 nginx")
    same = reclassify(
        original, iteration=1, tool_calls_log=[], errors=[], max_iterations=20,
    )
    assert same is original
    assert same.is_change is True


def test_reclassify_preserves_false_for_readonly():
    original = classify("看看系统情况")
    new = reclassify(
        original, iteration=5, tool_calls_log=[], errors=[], max_iterations=6,
    )
    assert new.is_change is False


# ── 3. 逐轮引导：变更纪律 ──────────────────────────────────────────────────

def test_change_discipline_appended_every_iteration():
    r = classify("重启 nginx")
    for it in (0, 1, 3, 9):
        assert _CHANGE_DISCIPLINE in get_guide_for_iteration(r, it)


def test_readonly_guide_has_no_change_discipline():
    r = classify("看看系统情况")
    assert "[变更纪律]" not in get_guide_for_iteration(r, 0)
    assert "[变更纪律]" not in get_guide_for_iteration(r, 3)


def test_change_discipline_coexists_with_planning_phase():
    """complex + 变更：首轮规划引导不得被纪律替换，只做追加"""
    r = classify("重构整个项目的部署脚本")
    assert r.is_change is True
    guide = get_guide_for_iteration(r, 0)
    assert "【规划阶段】" in guide
    assert _CHANGE_DISCIPLINE in guide


def test_change_discipline_not_duplicated():
    """纪律后缀只加一次（防止包装层被重复调用时累积）"""
    r = classify("重启 nginx")
    guide = get_guide_for_iteration(r, 0)
    assert guide.count("[变更纪律]") == 1


# ── 4. reclassify 思考预算钳制（与初始 classify 路径对称）─────────────────
# 历史缺陷（本轮修复）：reclassify 的升级信号（连续失败/工具密度）会把预算
# 推到默认表高档（MODERATE=2048/COMPLEX=4096），而 _compute_effective_budgets
# 在 max_budget<=4096 时直接返回默认表 — 无视模型上限。driver 把该值直接赋给
# ctx.config.max_reasoning_tokens，低上限模型（本地 vLLM/小模型）下一轮 LLM
# 调用带超限 reasoning_max_tokens → 400 → 整个 run 失败。
# 初始路径（driver classify 后 min(budget, model_max)）已防住，重分类路径必须对称。

def _upgrade_log(n_fail: int = 3) -> list[dict]:
    """构造触发 TRIVIAL→MODERATE 升级的工具日志（最近 N 次调用全部失败）"""
    return [{"tool": "ops_exec", "args": {}, "success": False} for _ in range(n_fail)]


@pytest.mark.parametrize("max_budget", [256, 512, 1024, 2048])
def test_reclassify_upgrade_budget_capped_at_model_max(max_budget):
    """升级后的预算不得超出模型配置上限（无论默认表档位多高）"""
    original = classify("读取 /etc/hosts", max_budget=max_budget)
    assert original.level is IntentionLevel.TRIVIAL
    new = reclassify(
        original, iteration=3, tool_calls_log=_upgrade_log(),
        errors=["x"] * 3, max_iterations=10, max_budget=max_budget,
    )
    assert new is not original
    assert new.level is IntentionLevel.MODERATE  # 升级确实发生了
    assert new.reasoning_budget is None or new.reasoning_budget <= max_budget


def test_reclassify_budget_unchanged_when_under_cap():
    """上限足够大时，升级预算不受钳制影响（保留默认表档位）"""
    original = classify("读取 /etc/hosts", max_budget=262144)
    new = reclassify(
        original, iteration=3, tool_calls_log=_upgrade_log(),
        errors=["x"] * 3, max_iterations=10, max_budget=262144,
    )
    assert new.level is IntentionLevel.MODERATE
    # 大上限走比例分配（5%*262144=13107 → 仍应 <= 上限），此处只验证钳制不误伤
    assert new.reasoning_budget is None or new.reasoning_budget <= 262144


def test_reclassify_no_change_returns_original_object():
    """未触发任何信号时返回原对象（原预算不变，无钳制必要）"""
    original = classify("检查 CPU 占用", max_budget=1024)
    same = reclassify(
        original, iteration=1, tool_calls_log=[], errors=[], max_iterations=20,
        max_budget=1024,
    )
    assert same is original
