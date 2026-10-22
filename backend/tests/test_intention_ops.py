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


# ── 寒暄 + 只读请求：driver 对 CONVERSATION 物理清空工具表 ──────────────────
# 只读动词与显式路径同样是动作信号。缺一条的后果不是"多花点 token"，而是
# "你好，看下磁盘"这类请求跑不了任何命令（driver 对 CONVERSATION return []），
# 模型只能凭训练印象作答。
@pytest.mark.parametrize("task", [
    "你好，看下磁盘",
    "谢谢，看看 nginx 状态",
    "你好 统计一下当前连接数",
    "你好，查询一下服务器列表",
    "你好，帮我查看 /var/log/messages",
    "hi check the disk usage",
    "hello, how many pods are running",
    "hi verify the backup finished",
    "hello look at the log",
    "hey, status of web-01",
    "hi list the processes",
])
def test_greeting_plus_read_request_keeps_tools(task: str):
    """带问候的只读请求必须离开 CONVERSATION（否则工具表被清空）"""
    assert classify(task).task_type is not TaskType.CONVERSATION


@pytest.mark.parametrize("task", [
    "你好", "你好呀", "谢谢", "在吗", "早上好", "再见", "你是真人吗",
    "你是什么模型", "你能做什么", "hello", "hi", "hey there",
    "who are you", "what are you", "which model are you", "what model are you",
    "thanks a lot", "thank you",
])
def test_pure_chat_still_takes_the_conversation_fast_path(task: str):
    """反向锁：只读动词表不得宽到把身份/能力询问推去调工具"""
    assert classify(task).task_type is TaskType.CONVERSATION


def test_显式路径本身就是动作信号():
    """无动词、只有路径的短请求也不能被当成寒暄"""
    assert classify("hi /etc/nginx/nginx.conf").task_type is not TaskType.CONVERSATION
    assert classify("hello config/app.yaml").task_type is not TaskType.CONVERSATION


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


@pytest.mark.parametrize("task", [
    "清理临时日志",
    "删除 /tmp 下的旧文件",
    "重启 nginx",
])
def test_change_only_task_gets_write_level_budget(task):
    """"清理临时日志"这类无显式只读动词的变更请求，实际是单步写操作。

    原实现里 task_type 默认 QUERY ⇒ level=TRIVIAL / 256 token：
    is_change 只影响方法论注入，破坏性任务反而拿到最低思考预算。
    写操作不得停留在只读档（与 simplify 分支的既有口径一致）。
    """
    r = classify(task)
    assert r.is_change is True
    assert r.level is not IntentionLevel.TRIVIAL
    assert r.reasoning_budget >= 512


def test_explicit_read_verb_keeps_readonly_level():
    """反向锁："查看启动日志"含变更词但显式只读动词 → 不得被抬成写档。

    is_change 仍按保守口径为 True（注入变更方法论），等级却应保留只读档，
    否则每次查日志都按写任务分配预算。
    """
    r = classify("查看启动日志")
    assert r.is_change is True
    assert r.level is IntentionLevel.TRIVIAL
    assert r.reasoning_budget == 256


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
    # 分支（连续失败 ≥3 ⇒ 升档）。原实现用的是 remaining≤3 的紧急度提升分支，
    # 那条已在 aca7427 作为死码删除（driver 每 reclassify_interval=5 轮才调本函数，
    # max_iterations=100 时永不命中），测试当时没跟着改 ⇒ master 红。
    # 「重启 nginx」初始 task_type=QUERY（变更词只进 is_change 不进类型词典），
    # P4 后该升级路径 QUERY → DEBUG。
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
    """构造触发连败升级的工具日志（最近 N 次调用全部失败；QUERY 任务升 DEBUG）"""
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
    # 「读取 /etc/hosts」是 QUERY 任务，P4 后连败升级走 DEBUG 档
    assert new.level is IntentionLevel.DEBUG
    assert new.reasoning_budget is None or new.reasoning_budget <= max_budget


def test_reclassify_budget_unchanged_when_under_cap():
    """上限足够大时，升级预算不受钳制影响（保留默认表档位）"""
    original = classify("读取 /etc/hosts", max_budget=262144)
    new = reclassify(
        original, iteration=3, tool_calls_log=_upgrade_log(),
        errors=["x"] * 3, max_iterations=10, max_budget=262144,
    )
    # QUERY 任务连败升级走 DEBUG 档（P4）
    assert new.level is IntentionLevel.DEBUG
    # 大上限走比例分配（10%*262144=26214 → 仍应 <= 上限），此处只验证钳制不误伤
    assert new.reasoning_budget is None or new.reasoning_budget <= 262144


def test_reclassify_no_change_returns_original_object():
    """未触发任何信号时返回原对象（原预算不变，无钳制必要）"""
    original = classify("检查 CPU 占用", max_budget=1024)
    same = reclassify(
        original, iteration=1, tool_calls_log=[], errors=[], max_iterations=20,
        max_budget=1024,
    )
    assert same is original


# ── 5. 事故词盲区 + 命中计分制（P1/P2 修复回归）───────────────────────────
# P1：CONVERSATION 快速路径要求「所有动作词汇都在词典里」—— 无界要求。
# 「在吗，服务器挂了」曾因「挂了」不在词典被判成纯对话，driver 对
# CONVERSATION 物理清空工具表 → 智能体对生产故障只能幻觉应答。

@pytest.mark.parametrize("task", [
    "在吗，服务器挂了",
    "在吗，服务宕机了",
    "数据库崩了，在吗",
    "服务崩溃了",
    "内存爆了帮我看看",
    "hi server is down",
    "hi 连接被拒绝了",
])
def test_incident_slang_is_not_conversation(task):
    """事故口语词必须进入动作信号，不得落入 CONVERSATION（工具被清空）"""
    r = classify(task)
    assert r.task_type is not TaskType.CONVERSATION


def test_incident_slang_lands_on_debug():
    """事故词同时应把任务引导到 debug 档（假设-验证循环），而非普通查询"""
    r = classify("在吗，服务器挂了")
    assert r.task_type is TaskType.DEBUG
    assert r.is_change is False  # 「挂了」是故障描述，不是变更动作


def test_digit_heuristic_blocks_chat_fast_path():
    """数字启发：纯寒暄几乎不含数字 —— 含数字即离开 CONVERSATION 快速路径。

    这是词典之外的第二道兜底（词典永远列不全事故的表述方式），用不含任何
    动作词典词的例句独立验证启发式生效。
    """
    assert classify("在吗，5 点开会").task_type is not TaskType.CONVERSATION
    # 反向：无数字的纯寒暄不受启发式影响
    assert classify("在吗，晚上一起吃饭吗").task_type is TaskType.CONVERSATION


# P2：原 elif 优先级链在两组词典各命中一次时完全由固定优先级裁决 ——
# 「评估这次重构的收益」被判成 REFACTOR → complex + 首轮禁工具规划 +
# is_change（每轮注入变更纪律）三重误导。改为命中计分 + 首现位置仲裁。

def test_analysis_context_beats_refactor_priority():
    """「评估这次重构的收益」是只读分析：评估在前 → RESEARCH，非 REFACTOR"""
    r = classify("评估这次重构的收益")
    assert r.task_type is TaskType.RESEARCH
    assert r.is_change is False          # 「重构」是分析对象，不是变更动作
    assert r.allow_planning_phase is False


def test_first_verb_wins_on_tie():
    """同分仲裁：运维指令多为动词开头，首动词即主导意图"""
    # 分析在前 → research（原 elif 链 debug 优先级高于 research，曾判 DEBUG）
    assert classify("分析下 nginx 的错误日志").task_type is TaskType.RESEARCH
    # 重构在前 → refactor 保持（执行主导）
    r = classify("重构这个模块并分析影响")
    assert r.task_type is TaskType.REFACTOR
    assert r.is_change is True


def test_multi_marker_hit_beats_priority_head():
    """命中更多词典条目的类型胜出（原 elif 链完全无视命中数）"""
    # 重构(1 hit) vs 修复+报错(2 hits) → DEBUG；原 elif 链 REFACTOR 先到先得
    assert classify("重构模块修复报错").task_type is TaskType.DEBUG


def test_confidence_reflects_match_strength():
    """confidence 不再是分支静态值：命中越多越高，类型冲突折减，无命中 0.5"""
    solo = classify("重构模块修复报错")        # DEBUG 双命中、无类型冲突
    mixed = classify("评估这次重构的收益")      # research/refactor 各 1，冲突
    assert solo.confidence > mixed.confidence
    assert classify("执行 df -h").confidence == 0.5  # 无任何词典命中


def test_pure_refactor_task_unchanged():
    """防回归：纯重构任务仍走 REFACTOR/complex/规划阶段，计分制不误伤"""
    r = classify("重构整个项目的部署脚本")
    assert r.task_type is TaskType.REFACTOR
    assert r.level is IntentionLevel.COMPLEX
    assert r.allow_planning_phase is True
    assert r.is_change is True


# ── 6. P3/P4 修复回归 ──────────────────────────────────────────────────────

# P3：「整个」「全量」是运维只读查询的日常词（"查看整个 /var/log 目录"
# "统计全量容器"），命中会把 trivial 抬成 moderate（256→2048 预算）还附带
# 「每次最多一个工具」纪律 —— 对批量统计是逆优化。长短语「整个项目」
# 「整个目录」保留（指全局改造时足够特异）。

def test_everyday_whole_quantity_keeps_readonly_level():
    """P3：只读批量查询不再被「整个/全量」抬档，保持 trivial 只读预算"""
    for task in ("查看整个 /var/log 目录的日志", "统计全量容器的内存占用"):
        r = classify(task)
        assert r.task_type is TaskType.QUERY
        assert r.level is IntentionLevel.TRIVIAL
        assert r.reasoning_budget == 256


def test_long_whole_phrases_still_boost():
    """P3 防回归：长短语「整个项目」保留抬档能力，全局改造信号不受影响"""
    # 「优化」不命中任何类型词典 → QUERY/TRIVIAL，靠「整个项目」抬到 moderate
    # （2.2 防呆：只读查询最多抬到 moderate，不给 complex 的禁工具规划轮）
    r = classify("优化整个项目的构建流程")
    assert r.level is IntentionLevel.MODERATE
    assert r.reasoning_budget == 2048


# P4：QUERY 任务（静态判成只读查询）连续失败，大概率实际在排障 ——
# 升级走 DEBUG 档（假设-验证引导），而非 moderate 的「多步骤实现」引导。

def test_reclassify_failed_query_goes_debug():
    """P4：QUERY 任务 3 连败升 DEBUG 档，task_type 保持透传"""
    original = classify("读取 /etc/hosts")
    assert original.task_type is TaskType.QUERY
    new = reclassify(
        original, iteration=3, tool_calls_log=_upgrade_log(),
        errors=["x"] * 3, max_iterations=10,
    )
    assert new.level is IntentionLevel.DEBUG
    assert new.reasoning_budget == 1024
    assert new.task_type is TaskType.QUERY       # 透传：工具结果不改变任务性质
    assert new.is_change is False
    assert "假设" in new.guide_prompt             # 引导语是 debug 的假设-验证版


def test_reclassify_failed_write_task_keeps_moderate():
    """P4 防回归：非 QUERY（写类）连败仍升 MODERATE，既有语义不变"""
    original = classify("改一下 nginx 配置")   # MODIFY → SIMPLE
    assert original.task_type is TaskType.MODIFY
    new = reclassify(
        original, iteration=3, tool_calls_log=_upgrade_log(),
        errors=["x"] * 3, max_iterations=10,
    )
    assert new.level is IntentionLevel.MODERATE
    assert "多步骤实现" in new.guide_prompt
