"""
意图识别 — 基于 task 文本推断复杂度等级与任务类型，动态调整 reasoning 预算

设计目标：
- "真正的意图识别"：综合关键词、长度、结构信号、否定词等多维特征，而非单点关键词匹配
- 输出 (level, task_type, reasoning_budget, allow_planning_phase, guide_prompt) 五元组
- 额外输出 is_change：任务是否可能改变目标机 / 代码库状态，
  供运维方法论二选一注入（OPS_METHOD_READONLY / OPS_METHOD_CHANGE）
- Driver 根据结果动态调整每轮 LLM 调用的 reasoning 预算 + prompt 引导语

5 个复杂度等级：
  trivial  : 单步只读（"读 X 文件"、"列目录"）           → budget=256,  无规划
  simple   : 单步写（"创建 X"、"改 X 为 Y"）              → budget=512,  无规划
  moderate : 多步骤任务（"实现 X 功能"）                   → budget=2048, 无规划
  complex  : 复杂规划/重构（"重构 X 模块"、"重写 X"）     → budget=4096, 允许首轮规划
  debug    : 调试任务（"修复 bug"、"为什么 X 不工作"）    → budget=1024, 无规划（每步要看结果）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class IntentionLevel(str, Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    DEBUG = "debug"


class TaskType(str, Enum):
    QUERY = "query"          # 纯查询/只读
    IMPLEMENT = "implement"  # 新功能
    MODIFY = "modify"        # 修改既有代码
    REFACTOR = "refactor"    # 重构
    DEBUG = "debug"          # 调试
    RESEARCH = "research"    # 调研/分析
    CONVERSATION = "conversation"  # 纯对话（问候/身份/能力询问/闲聊），不调工具、不查记忆


@dataclass
class IntentionResult:
    """意图识别结果"""
    level: IntentionLevel
    task_type: TaskType
    reasoning_budget: Optional[int]   # None 表示不限制；int 表示 thinking token 上限
    allow_planning_phase: bool        # 是否允许首轮纯规划（不调工具）
    guide_prompt: str                 # 注入到 system_prompt 的引导语
    confidence: float = 0.7           # 识别置信度（0-1），用于日志/审计
    # 是否可能改变目标机 / 代码库状态（与 task_type 正交：task_type 描述任务性质，
    # is_change 描述"是否产生副作用"）。由 routes/agent.py 选用
    # OPS_METHOD_CHANGE / OPS_METHOD_READONLY 消费。
    is_change: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "task_type": self.task_type.value,
            "reasoning_budget": self.reasoning_budget,
            "allow_planning_phase": self.allow_planning_phase,
            "confidence": self.confidence,
            "is_change": self.is_change,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 信号词典（中文优先，含少量英文同义词）
# ──────────────────────────────────────────────────────────────────────────────

# 强信号：明确指示任务类型（按优先级从高到低匹配）
_REFACTOR_MARKERS = [
    "重构", "重写", "改造", "重新设计", "重新组织", "结构优化",
    "refactor", "rewrite", "restructure",
]
_IMPLEMENT_MARKERS = [
    "实现", "新增", "添加", "创建一个", "做一个", "开发",
    "implement", "add ", "create a", "build a",
]
_DEBUG_MARKERS = [
    "修复", "解决", "bug", "报错", "错误", "异常", "不工作", "失败", "为什么",
    "排查", "诊断", "定位", "无法", "不能", "卡住", "卡死",
    "fix", "debug", "error", "exception", "broken", "why ",
]
_RESEARCH_MARKERS = [
    "分析", "调研", "评估", "审查", "review", "检查", "explain", "理解",
]
_QUERY_MARKERS = [
    "读取", "列出", "查看", "显示", "show ", "list ", "cat ", "看一下", "查看一下",
    # 只读动词的高频口语形态。缺一条就被纯对话快速路径吞掉，而 driver 对
    # CONVERSATION 清空工具表 —— "你好，看下磁盘" 于是物理上跑不了任何命令。
    # 刻意不收 "what is"/"see"：前者命中身份询问（"what is your name"），
    # 后者是闲聊高频词（"nice to see you"），代价是把纯对话推去调工具。
    "看下", "看看", "查下", "查查", "查询", "统计",
    "check", "inspect", "verify", "status", "look at", "how many", "how much",
]
_MODIFY_MARKERS = [
    "修改", "改为", "改成", "替换", "更新", "调整", "改一下", "改下", "rename",
]

# 变更信号：任务可能改变目标机 / 代码库状态（文件、服务、包、权限、版本库）
# 与 TaskType 正交 —— task_type 说"做什么性质的事"，is_change 说"有没有副作用"。
# 判定偏保守（宁滥勿缺）：误判为变更的代价是多说一段影响说明；
# 漏判的代价是变更命令只拿到只读方法论 → 代价不对称。
# 注意：_any_match_wordbounded 对纯 ASCII 词做词边界匹配（"rm" 不命中 "confirm"，
# "start" 不命中 "start.sh"），故英文标记一律不带尾随空格，否则退化为子串匹配。
_CHANGE_MARKERS = [
    # 服务 / 进程生命周期
    "重启", "停止", "启动", "关掉", "关闭服务", "杀掉", "杀死", "重新加载",
    "restart", "stop", "start", "reload", "kill",
    # 文件 / 配置变更
    # 注意：不收录"执行/运行/run/exec" —— "执行 df -h" 也是纯只读，
    # 而"运行部署脚本"已被"部署"命中，无需借助通用动词。
    "删除", "删掉", "移除", "清理", "清空", "覆盖", "写入", "新建",
    "重命名", "移动", "改配置", "修复",
    "rm", "mv", "cp", "chmod", "chown", "mkdir", "truncate", "fix",
    # 部署 / 发布 / 容量与数据
    "部署", "发布", "上线", "回滚", "回退", "扩容", "缩容", "迁移", "备份", "恢复",
    "deploy", "rollback", "scale",
    # 包管理 / 提权
    "安装", "卸载", "升级", "降级", "apt-get", "yum", "dnf", "apk",
    "install", "uninstall", "upgrade", "sudo",
    # 版本控制
    "提交", "推送", "合并", "拉取",
    "commit", "push", "pull", "merge", "rebase", "checkout", "reset",
]

# 纯对话信号：问候/身份询问/能力询问/闲聊 — 不需要工具、不需要记忆、不需要规划
# 优先级最高（先判定，避免被其他 markers 误吞）
_CONVERSATION_MARKERS = [
    # 身份询问
    "你是什么", "你是谁", "你叫什么", "你的名字", "你是哪个", "你用的是",
    "什么模型", "哪个模型", "模型是什么", "你是ai", "你是机器人",
    "你是gpt", "你是claude", "你是qwen", "你是llama", "你是glm",
    "what are you", "who are you", "which model", "what model",
    # 能力询问
    "你能做什么", "你会什么", "你能帮我", "可以做什么", "擅长什么",
    "有什么功能", "能干什么", "what can you do",
    # 问候/闲聊
    "你好", "您好", "hi", "hello", "hey", "早上好", "下午好", "晚上好",
    "谢谢", "感谢", "thanks", "thank you", "再见", "拜拜", "bye",
    "在吗", "在不在", "你是真的", "你是真人",
]

# 否定/简化信号：与上面的强信号结合，降低复杂度
_SIMPLIFY_MARKERS = [
    "只是", "仅仅", "快速", "简单", "只改", "只读", "just ", "simply ", "only ",
]

# 结构信号：暗示多步骤/需要规划
# 注意：中文条目走的是子串匹配（_any_match_wordbounded 只对纯 ASCII 做词边界），
# 所以单字「先」会命中「优先/领先/率先」，"调整任务优先级" 曾被判成多步骤
_MULTI_STEP_MARKERS = [
    "然后", "接着", "第一步", "第二步", "第三步", "首先", "其次", "最后",
    "步骤", "phase", "step 1", "step 2",
]

# 复杂度提升信号：暗示需要全局思考
# 同上，中文按子串匹配 —— 「系统/所有/全部」在运维提问里是日常词
# （"查看系统负载""列出所有进程"），命中它们会把只读查询抬成 complex，
# 白付一轮「只规划不调工具」的开销，故此处只留真正指向全局改造的短语
_COMPLEXITY_MARKERS = [
    "整个", "模块", "架构", "性能优化", "全量",
    "整个项目", "整个目录",
]

# 文件路径信号：暗示需要操作文件系统
_PATH_PATTERN = re.compile(r"[a-zA-Z0-9_\-/\\]+\.[a-zA-Z]{1,8}")


# ──────────────────────────────────────────────────────────────────────────────
# 预算/引导配置
# ──────────────────────────────────────────────────────────────────────────────

# 各等级默认预算（thinking tokens）
_LEVEL_BUDGET: dict[IntentionLevel, Optional[int]] = {
    IntentionLevel.TRIVIAL: 256,
    IntentionLevel.SIMPLE: 512,
    IntentionLevel.MODERATE: 2048,
    IntentionLevel.COMPLEX: 4096,
    IntentionLevel.DEBUG: 1024,
}

# 各等级引导语（注入到 system_prompt 末尾）
_LEVEL_GUIDE: dict[IntentionLevel, str] = {
    IntentionLevel.TRIVIAL:
        "思考预算：本任务为简单只读或对话型操作，思考应极简（< 256 tokens）。"
        "**先判断是否需要工具**：纯对话问题（如身份、问候、能力询问）直接回答，不要调用任何工具；"
        "只有需要读取文件/查询信息时才调用对应工具，基于结果简短回答即可，不要做长篇规划。",
    IntentionLevel.SIMPLE:
        "思考预算：本任务为单步写操作，思考应简洁（< 512 tokens），"
        "明确目标路径与改动内容后直接执行，避免冗余推演。",
    IntentionLevel.MODERATE:
        "思考预算：本任务为多步骤实现，每轮思考应聚焦当前一步（< 2048 tokens），"
        "执行一步看结果再决定下一步，不要一次性在脑中走完所有步骤。"
        "每次只调用一个工具；若多个工具互不依赖可并行，但禁止基于未执行工具的想象结果做后续决策。",
    IntentionLevel.COMPLEX:
        "思考预算：本任务为复杂规划/重构，首轮允许较长思考（< 4096 tokens）做整体规划，"
        "但**首轮规划阶段不要调用工具**，只输出步骤清单。"
        "从第二轮起，每步思考必须收敛到 < 1024 tokens，逐步执行并基于真实结果决策。"
        "禁止在一次回复中调用多个有依赖关系的工具。",
    IntentionLevel.DEBUG:
        "思考预算：本任务为调试，每步思考应聚焦当前观察（< 1024 tokens）。"
        "调试的本质是「假设-验证-修正」，每步先提出假设→调用工具验证→基于结果修正假设，"
        "禁止在脑中连续推演多个未验证的假设。",
}

# 变更纪律（is_change=True 时追加到逐轮引导）
# 为什么不只靠 system prompt 里的 OPS_METHOD_CHANGE：driver 把本函数的返回值
# 作为**最后一条 user 消息**注入，是注意力最强的位置；而系统提示里的完整方法论
# 在长上下文中会被稀释。只读任务短路了，变更任务需要在尾部再钉一次。
_CHANGE_DISCIPLINE = (
    "\n\n[变更纪律] 本任务会改变目标机状态：动手前先用只读工具确认现状，"
    "执行前说明做什么 / 影响范围 / 是否可逆 / 如何回滚，"
    "一次只做一个变更并立即验证，目标含糊时先列出候选等用户确认。"
)


def _compute_effective_budgets(max_budget: Optional[int]) -> dict[IntentionLevel, Optional[int]]:
    """
    根据模型配置的 reasoning_tokens 上限，动态计算各等级的有效预算。

    策略：
    - 当 max_budget 未设置或较小时（<= 4096），使用默认的固定预算
    - 当 max_budget 较大时（如 262144），按比例放大各等级预算（只放大、不缩小，
      下限就是上面的固定值）：
      - trivial: max(256, max_budget * 0.1%)
      - simple: max(512, max_budget * 0.2%)
      - moderate: max(2048, max_budget * 5%)
      - complex: max(4096, max_budget * 50%)（允许深度推理）
      - debug: max(1024, max_budget * 10%)
    """
    if not max_budget or max_budget <= 4096:
        return _LEVEL_BUDGET

    # 大预算模型：按比例分配。取 max 而不是 min —— 配了更大的思考上限却把
    # trivial/simple 预算降到固定值以下（8192 时曾是 256→128、512→256）是反的。
    return {
        IntentionLevel.TRIVIAL: max(256, int(max_budget * 0.001)),
        IntentionLevel.SIMPLE: max(512, int(max_budget * 0.002)),
        IntentionLevel.MODERATE: max(2048, int(max_budget * 0.05)),
        IntentionLevel.COMPLEX: max(4096, int(max_budget * 0.5)),  # 复杂任务可用 50% 预算
        IntentionLevel.DEBUG: max(1024, int(max_budget * 0.1)),
    }


def classify(task: str, max_budget: Optional[int] = None) -> IntentionResult:
    """
    意图识别主入口

    Args:
        task: 用户原始任务文本
        max_budget: 模型配置的 reasoning_tokens 上限，用于动态调整各等级预算

    Returns:
        IntentionResult: 包含 level/task_type/budget/allow_planning/guide_prompt
    """
    # 动态预算：当模型配置了较大的 reasoning_tokens 时，按比例放大各等级预算
    # 对于 1M 上下文模型（reasoning_tokens=262144），complex 任务可使用 131072 tokens
    effective_budget = _compute_effective_budgets(max_budget)

    if not task or not task.strip():
        # 空任务兜底为 trivial
        return IntentionResult(
            level=IntentionLevel.TRIVIAL,
            task_type=TaskType.QUERY,
            reasoning_budget=effective_budget[IntentionLevel.TRIVIAL],
            allow_planning_phase=False,
            guide_prompt=_LEVEL_GUIDE[IntentionLevel.TRIVIAL],
            confidence=0.3,
        )

    text = task.lower()
    text_zh = task  # 中文匹配保留原文（lower 不影响中文）

    # 变更信号：命中运维变更动词（重启/删除/部署/安装/回滚…）
    has_change_signal = _any_match_wordbounded(_COMPILED_CHANGE, text, text_zh)

    # ── 0. 纯对话型任务识别（最高优先级，先判定）──
    # 短任务 + 命中对话 markers → 直接归为 CONVERSATION
    # 这样可以避免"你是什么模型"被误判为 trivial QUERY 并被引导去调工具
    # 注意：使用词边界匹配避免 "hello" 误命中 "hello.py" 等文件名
    # 例外：若任务同时命中强动作信号（debug/refactor/implement/modify/research），
    # 说明虽有问候词但本质是动作任务（如 "fix the hello bug"），不归为 CONVERSATION
    task_len = len(task)
    has_action_signal = (
        _any_match_wordbounded(_COMPILED_REFACTOR, text, text_zh)
        or _any_match_wordbounded(_COMPILED_DEBUG, text, text_zh)
        or _any_match_wordbounded(_COMPILED_IMPLEMENT, text, text_zh)
        or _any_match_wordbounded(_COMPILED_MODIFY, text, text_zh)
        or _any_match_wordbounded(_COMPILED_RESEARCH, text, text_zh)
        or has_change_signal
        # 只读动词与显式路径同样是动作信号。这里原先只看变更/调试类动词，于是
        # "hi list the processes on web-01"、"你好，帮我查看 /var/log/messages"
        # 因为不含变更动词就被判成 CONVERSATION —— 而 driver 对 CONVERSATION 直接
        # 清空工具表（_filter_tools_by_intention），这类带问候的请求于是物理上跑不了
        # 任何命令，模型只能凭训练印象作答（正是本模块立起来要避开的那个盲区）。
        # 纯寒暄仍然留在 CONVERSATION：它既没有查询动词也没有路径。
        or _any_match_wordbounded(_COMPILED_QUERY, text, text_zh)
        or bool(_PATH_PATTERN.search(task))
    )
    if (
        task_len < 60
        and not has_action_signal
        and _any_match_wordbounded(_COMPILED_CONVERSATION, text, text_zh)
    ):
        return IntentionResult(
            level=IntentionLevel.TRIVIAL,
            task_type=TaskType.CONVERSATION,
            reasoning_budget=effective_budget[IntentionLevel.TRIVIAL],
            allow_planning_phase=False,
            guide_prompt=(
                "思考预算：本任务为纯对话型问题，思考应极简（< 256 tokens）。"
                "**不要调用任何工具**，直接基于自身身份和能力简洁回答即可。"
                "不要做长篇规划，不要引用无关记忆或上下文。"
            ),
            confidence=0.95,
            is_change=False,  # 纯对话不产生副作用
        )

    # ── 1. 任务类型识别（按优先级，先匹配先返回）──
    # 优先级：refactor > debug > research > implement > modify > query
    task_type = TaskType.QUERY
    type_confidence = 0.5

    if _any_match_wordbounded(_COMPILED_REFACTOR, text, text_zh):
        task_type = TaskType.REFACTOR
        type_confidence = 0.9
    elif _any_match_wordbounded(_COMPILED_DEBUG, text, text_zh):
        task_type = TaskType.DEBUG
        type_confidence = 0.85
    elif _any_match_wordbounded(_COMPILED_RESEARCH, text, text_zh):
        task_type = TaskType.RESEARCH
        type_confidence = 0.75
    elif _any_match_wordbounded(_COMPILED_IMPLEMENT, text, text_zh):
        task_type = TaskType.IMPLEMENT
        type_confidence = 0.8
    elif _any_match_wordbounded(_COMPILED_MODIFY, text, text_zh):
        task_type = TaskType.MODIFY
        type_confidence = 0.75
    elif _any_match_wordbounded(_COMPILED_QUERY, text, text_zh):
        task_type = TaskType.QUERY
        type_confidence = 0.8

    # ── 2. 复杂度等级识别 ──
    # 起始等级由 task_type 决定，再由其他信号调整
    base_level = {
        TaskType.QUERY: IntentionLevel.TRIVIAL,
        TaskType.IMPLEMENT: IntentionLevel.MODERATE,
        TaskType.MODIFY: IntentionLevel.SIMPLE,
        TaskType.REFACTOR: IntentionLevel.COMPLEX,
        TaskType.DEBUG: IntentionLevel.DEBUG,
        TaskType.RESEARCH: IntentionLevel.MODERATE,
        TaskType.CONVERSATION: IntentionLevel.TRIVIAL,
    }[task_type]

    level = base_level
    level_confidence = type_confidence

    # 2.1 多步骤信号 → 提升到至少 moderate
    has_multi_step = _any_match_wordbounded(_COMPILED_MULTI_STEP, text, text_zh)
    if has_multi_step and level in (IntentionLevel.TRIVIAL, IntentionLevel.SIMPLE):
        level = IntentionLevel.MODERATE
        level_confidence = max(level_confidence, 0.75)

    # 2.2 复杂度信号 → 提升到 complex（只读查询只到 moderate）
    has_complexity = _any_match_wordbounded(_COMPILED_COMPLEXITY, text, text_zh)
    if has_complexity and level in (IntentionLevel.TRIVIAL, IntentionLevel.SIMPLE, IntentionLevel.MODERATE):
        # complex 的引导要求「首轮只做规划、不许调用工具」（见 _LEVEL_GUIDE）。
        # 只读查询没有数据无从规划 —— 抬到 complex 等于白烧一轮再拿 16 倍的思考预算，
        # 「查看整个目录的日志」这类提问需要的只是多走几步，不是先写计划。
        level = IntentionLevel.MODERATE if task_type == TaskType.QUERY else IntentionLevel.COMPLEX
        level_confidence = max(level_confidence, 0.8)

    # 2.3 长度信号（弱信号，仅作为提升依据，不单独降级）
    # task_len 已在前文（CONVERSATION 判定处）计算，此处复用
    if task_len > 500 and level in (IntentionLevel.TRIVIAL, IntentionLevel.SIMPLE):
        level = IntentionLevel.MODERATE
        level_confidence = max(level_confidence, 0.6)
    elif task_len > 1500 and level == IntentionLevel.MODERATE:
        level = IntentionLevel.COMPLEX
        level_confidence = max(level_confidence, 0.65)

    # 2.4 否定/简化信号 → 降一级（但不低于 trivial）
    # 注意：modify/implement 至少保持 simple（写操作不能降到 trivial）
    has_simplify = _any_match_wordbounded(_COMPILED_SIMPLIFY, text, text_zh)
    if has_simplify:
        new_level = _decrease_level(level)
        # 写操作类型不能降到 trivial（trivial 暗示只读）
        if task_type in (TaskType.MODIFY, TaskType.IMPLEMENT) and new_level == IntentionLevel.TRIVIAL:
            new_level = IntentionLevel.SIMPLE
        # debug 不被简化信号影响（调试本质不变）
        if level != IntentionLevel.DEBUG:
            level = new_level
            level_confidence = max(level_confidence - 0.1, 0.5)

    # 2.4.1 短任务降级：IMPLEMENT 类型 + 任务极短 + 含具体文件路径 → 降为 simple
    # 例：「创建一个 hello.py」（< 30 字 + 有路径）显然是单文件创建
    # 但「实现一个用户登录功能」（< 30 字 + 无路径）仍是 moderate（抽象功能实现）
    has_path = bool(_PATH_PATTERN.search(task))
    if (
        task_type == TaskType.IMPLEMENT
        and level == IntentionLevel.MODERATE
        and task_len < 30
        and has_path
        and not has_multi_step
        and not has_complexity
    ):
        level = IntentionLevel.SIMPLE
        level_confidence = max(level_confidence, 0.75)

    # 2.5 文件路径信号不改变等级：纯查询带路径仍是 trivial（如 "读取 /etc/hosts"）

    # 2.6 debug 任务的特殊处理：debug 即使有复杂信号也保持 debug（不提到 complex）
    # 因为调试需要的是"假设-验证"循环，而非"长规划"
    if task_type == TaskType.DEBUG and level == IntentionLevel.COMPLEX:
        # 复杂 debug 保持 debug 等级（限制每步思考），不切到 complex
        level = IntentionLevel.DEBUG

    # ── 2.7 变更类判定（is_change）──
    # 两条来源取或：
    #   a) 命中变更动词（has_change_signal）
    #   b) 任务性质本身即写操作（implement / modify / refactor）
    # debug 不自动算变更 —— "排查为什么服务挂了"是只读取证，
    # 只有同时命中变更动词（"修复 nginx 配置"）才算。
    is_change = has_change_signal or task_type in (
        TaskType.IMPLEMENT, TaskType.MODIFY, TaskType.REFACTOR,
    )

    # 2.8 变更任务不得停留在只读档位：无显式只读动词的变更请求（"清理临时日志"）
    # 在类型识别里落到默认 QUERY，实际是单步写操作，却只拿 256 token 只读预算。
    # 显式只读动词（"查看启动日志"）保留 trivial —— 查日志不该按写任务分配预算。
    if (
        is_change
        and level == IntentionLevel.TRIVIAL
        and not _any_match_wordbounded(_COMPILED_QUERY, text, text_zh)
    ):
        level = IntentionLevel.SIMPLE
        level_confidence = max(level_confidence, 0.75)

    # ── 3. 允许规划阶段？ ──
    # 仅 complex 等级且非 debug 允许首轮纯规划
    allow_planning = (level == IntentionLevel.COMPLEX and task_type != TaskType.DEBUG)

    # ── 4. 取预算 ──
    budget = effective_budget.get(level)

    # ── 5. 拼装引导语 ──
    guide = _LEVEL_GUIDE[level]

    # 复杂任务额外追加"单 tool_call"约束
    if level in (IntentionLevel.MODERATE, IntentionLevel.COMPLEX, IntentionLevel.DEBUG):
        guide += (
            "\n\n工具调用纪律：每次回复最多调用一个工具（除非多个工具完全独立、互不依赖）。"
            "禁止在一次回复中调用多个有依赖关系的工具——后续工具的参数依赖前一个工具的返回值时，"
            "必须等前一个工具执行完毕、拿到真实结果后再决定下一步。"
            "脑补未执行工具的结果会导致幻觉和错误决策。"
        )

    # 调试任务额外强调假设-验证
    if level == IntentionLevel.DEBUG:
        guide += (
            "\n\n调试纪律：不要在思考中连续推演「如果 A 不对，那 B 可能...」的假设链。"
            "每次只提出一个假设，调用工具验证，根据真实结果决定下一个假设。"
            "未经验证的假设不得作为后续推理的依据。"
        )

    # 查询/调研任务：引导模型在需要时主动联网
    if task_type in (TaskType.QUERY, TaskType.RESEARCH):
        guide += (
            "\n\n联网提示：本任务可能涉及最新信息或外部知识。"
            "如果已有上下文和记忆不足以给出准确回答，请优先调用 web_search 进行联网搜索，"
            "或用 web_fetch 抓取具体网页内容；不要仅凭内部知识做判断。"
        )

    return IntentionResult(
        level=level,
        task_type=task_type,
        reasoning_budget=budget,
        allow_planning_phase=allow_planning,
        guide_prompt=guide,
        confidence=level_confidence,
        is_change=is_change,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 内部辅助
# ──────────────────────────────────────────────────────────────────────────────

# 预编译：将纯 ASCII 词边界正则从每次调用编译提升为模块加载时一次性编译。
# classify() 对同一组 marker 列表调用 _any_match_wordbounded 约 8 次（每级任务类型判断一次），
# 每次 70+ 个 marker 各自 re.compile + re.search 是纯粹的重复开销。
# 编译后的 (is_word, compiled_or_escaped, is_regex) 元组在模块生命周期内缓存。
_PreCompiled = tuple[bool, re.Pattern | str, bool]


def _precompile_markers(markers: list[str]) -> tuple[_PreCompiled, ...]:
    """模块加载时一次性预编译 marker 正则/子串描述符。

    返回值可被 _any_match_wordbounded 复用，消除每次调用的 re.compile 开销。
    """
    result: list[_PreCompiled] = []
    for m in markers:
        w = m.strip()
        if not w:
            continue
        # 含空格或非 ASCII 字母数字 _：直接 substring 匹配（短语足够特异）
        if ' ' in w or not re.fullmatch(r'[A-Za-z0-9_]+', w):
            result.append((False, re.escape(w), True))  # (非整词, 转义文本, 走substring)
            continue
        # 纯 ASCII 字母数字：前后不能是字母数字或常见代码/文件名分隔符
        pattern = (
            rf'(?<![A-Za-z0-9._/\\()\-]){re.escape(w)}'
            rf'(?![A-Za-z0-9._/\\()\-])'
        )
        result.append((True, re.compile(pattern, re.IGNORECASE), False))
    return tuple(result)


# 全量 marker 预编译：覆盖 classify / reclassify / _any_match_wordbounded 全部调用点
_COMPILED_REFACTOR = _precompile_markers(_REFACTOR_MARKERS)
_COMPILED_IMPLEMENT = _precompile_markers(_IMPLEMENT_MARKERS)
_COMPILED_DEBUG = _precompile_markers(_DEBUG_MARKERS)
_COMPILED_RESEARCH = _precompile_markers(_RESEARCH_MARKERS)
_COMPILED_QUERY = _precompile_markers(_QUERY_MARKERS)
_COMPILED_MODIFY = _precompile_markers(_MODIFY_MARKERS)
_COMPILED_CHANGE = _precompile_markers(_CHANGE_MARKERS)
_COMPILED_CONVERSATION = _precompile_markers(_CONVERSATION_MARKERS)
_COMPILED_SIMPLIFY = _precompile_markers(_SIMPLIFY_MARKERS)
_COMPILED_MULTI_STEP = _precompile_markers(_MULTI_STEP_MARKERS)
_COMPILED_COMPLEXITY = _precompile_markers(_COMPLEXITY_MARKERS)


def _any_match_wordbounded(
    precompiled: tuple[_PreCompiled, ...],
    text: str,
    search_text: str,
) -> bool:
    """词边界感知的 marker 匹配（避免英文 marker 误命中文档名/代码片段）

    接受预编译描述符（替代原始 marker 列表），消除每次调用的正则编译开销。
    语义与原实现完全一致：
    - 纯 ASCII 字母数字 marker：词边界匹配，例 'list' 不命中 'list()'、'hello' 不命中 'hello.py'。
    - 含空格/中文/连字符 marker：re.escape 后子串匹配。

    Args:
        precompiled: _precompile_markers() 在模块加载时产出的描述符元组。
        text: 已 .lower() 的文本（ASCII 正则搜索源）。
        search_text: 保留原文的文本（中文/短语 substring 搜索源）。
    """
    if not text:
        return False
    for is_word, compiled, is_substr in precompiled:
        if is_substr:
            if re.search(compiled, search_text, re.IGNORECASE):
                return True
        else:
            if compiled.search(text):
                return True
    return False


def _decrease_level(level: IntentionLevel) -> IntentionLevel:
    """降低一级复杂度（不低于 trivial）

    降级链：complex → moderate → simple → trivial
    debug 单独处理（不参与降级链，由调用方决定）
    """
    order = [
        IntentionLevel.TRIVIAL,
        IntentionLevel.SIMPLE,
        IntentionLevel.MODERATE,
        IntentionLevel.COMPLEX,
    ]
    try:
        idx = order.index(level)
    except ValueError:
        # debug 不在降级链中，直接返回
        return level
    return order[max(0, idx - 1)]


def get_guide_for_iteration(
    intention: IntentionResult,
    iteration: int,
) -> str:
    """逐轮引导语（公开入口）。

    在 `_guide_for_iteration_base` 的基础上，对变更类任务（is_change）追加
    `_CHANGE_DISCIPLINE` —— 该返回值由 driver 作为最末一条 user 消息注入，
    是压过 system prompt 里长方法论的最后一次提醒。
    """
    guide = _guide_for_iteration_base(intention, iteration)
    if intention.is_change and _CHANGE_DISCIPLINE not in guide:
        guide += _CHANGE_DISCIPLINE
    return guide


def _guide_for_iteration_base(
    intention: IntentionResult,
    iteration: int,
) -> str:
    """
    根据当前迭代轮次返回动态引导语（不含变更纪律，由外层追加）

    - iteration=0 + allow_planning_phase → 规划阶段引导
    - iteration=0 + 不允许规划 → 直接执行引导
    - iteration>0 → 收敛思考引导

    Args:
        intention: 意图识别结果
        iteration: 当前迭代轮次（0-based）

    Returns:
        本轮适用的引导语
    """
    # 规划阶段（仅 complex 任务首轮）
    if iteration == 0 and intention.allow_planning_phase:
        return (
            "【规划阶段】本轮你**不应调用任何工具**，只输出简洁的执行计划：\n"
            "1. 用编号列出关键步骤（每步一句话，不超过 10 步）\n"
            "2. 标注步骤间的依赖关系\n"
            "3. 识别需要先确认的歧义点\n"
            "规划完成后，下一轮开始按计划逐步执行。\n"
            "思考预算：< 4096 tokens。"
        )

    # 首轮但不需要规划 → 直接进入执行
    if iteration == 0:
        return intention.guide_prompt

    # 后续轮次：收紧思考预算
    if intention.level == IntentionLevel.COMPLEX:
        return (
            "【执行阶段】规划已完成，现在按计划逐步执行。\n"
            "本轮思考预算：< 1024 tokens（已收紧）。\n"
            "每次只调用一个工具，基于真实结果再决定下一步。\n"
            "若实际执行中发现规划不适用，可简要说明并调整，但不要重新做长规划。"
        )

    # 其他等级后续轮次保持原引导
    return intention.guide_prompt


def reclassify(
    original: IntentionResult,
    iteration: int,
    tool_calls_log: list[dict],
    errors: list[str],
    max_iterations: int,
    max_budget: Optional[int] = None,
) -> IntentionResult:
    """
    动态重分类 — 根据已积累的执行上下文重新评估意图等级

    触发条件（由 Driver 在每 reclassify_interval 轮调用）：
    - 连续多轮工具失败 → 可能任务比预期复杂，升级
    - 工具调用数远超预期 → 任务拆解不足，升级
    - 执行顺畅、工具调用少 → 任务比预期简单，降级
    （曾有第四条「接近末尾 → 追加收敛提示」，因 driver 每 5 轮才调本函数、
    max_iterations=100 时永不命中，已随 aca7427 删除；每轮检查的同类提示在
    driver 侧，每个 run 只发一次。）

    Args:
        max_budget: 模型配置的 reasoning_tokens 上限，用于动态调整预算

    Returns:
        更新后的 IntentionResult（如果无需变化则返回原对象）
    """
    effective_budget = _compute_effective_budgets(max_budget)
    total_tool_calls = len(tool_calls_log)
    failed_calls = sum(1 for e in tool_calls_log if e.get("success") is False)
    recent_failures = sum(
        1 for e in tool_calls_log[-5:] if e.get("success") is False
    )
    # aca7427 删掉 remaining≤3 的紧急度分支后这个差值就没人读了（F841）；
    # max_iterations 形参保留 —— 它是 driver 传进来的重分类上下文，签名即契约。

    level = original.level
    budget = original.reasoning_budget
    guide = original.guide_prompt
    confidence = original.confidence
    changed = False

    # ── 升级信号 ──
    # 连续失败 ≥ 3 次：任务比预期复杂
    if recent_failures >= 3 and level in (IntentionLevel.TRIVIAL, IntentionLevel.SIMPLE):
        level = IntentionLevel.MODERATE
        budget = effective_budget[level]
        guide = _LEVEL_GUIDE[level]
        confidence = max(confidence - 0.1, 0.5)
        changed = True
    elif recent_failures >= 4 and level == IntentionLevel.MODERATE:
        level = IntentionLevel.DEBUG
        budget = effective_budget[level]
        guide = _LEVEL_GUIDE[level]
        confidence = max(confidence - 0.1, 0.5)
        changed = True

    # 工具调用数远超预期（每轮平均 > 2 个）：任务拆解不足
    if iteration > 0 and total_tool_calls / max(iteration, 1) > 2.5:
        if level in (IntentionLevel.TRIVIAL, IntentionLevel.SIMPLE):
            level = IntentionLevel.MODERATE
            budget = effective_budget[level]
            guide = _LEVEL_GUIDE[level]
            changed = True

    # ── 降级信号 ──
    # 执行顺畅（失败率 < 10%）+ 等级偏高 → 降一级
    if (
        iteration >= 4
        and total_tool_calls > 0
        and failed_calls / total_tool_calls < 0.1
        and level in (IntentionLevel.COMPLEX, IntentionLevel.MODERATE)
    ):
        new_level = _decrease_level(level)
        if new_level != level:
            level = new_level
            budget = effective_budget.get(level, budget)
            guide = _LEVEL_GUIDE.get(level, guide)
            changed = True

    if not changed:
        return original

    # 预算钳制（与初始 classify 路径对称）：重分类的升级信号（连续失败/工具密度）
    # 可能把预算推到默认表的高档（MODERATE=2048/COMPLEX=4096），而模型配置的
    # reasoning 上限可能更低（本地 vLLM / 小模型常见 ≤4096）。不钳制时 driver
    # 会把超限值直接赋给 max_reasoning_tokens（driver.py 赋值点与
    # get_reasoning_budget_for_iteration 的 base 都消费这个字段），下一轮 LLM
    # 调用带超限 reasoning_max_tokens → 400 → 整个 run 失败。初始路径
    # （driver.py classify 后 min(budget, model_max)）防了这一层，重分类路径
    # 必须对称防住。
    if max_budget is not None and budget is not None:
        budget = min(budget, max_budget)

    return IntentionResult(
        level=level,
        task_type=original.task_type,
        reasoning_budget=budget,
        allow_planning_phase=False,  # 重分类后不再允许规划阶段
        guide_prompt=guide,
        confidence=confidence,
        # is_change 由初始任务文本判定，执行过程中的工具结果不改变任务性质 → 原样透传
        is_change=original.is_change,
    )


# ── 渐进式信息收集工具名（只读类）──
# 原集合只有 web_*，注释还写着「ssh 模式文件操作经 shell_exec，无法按名识别」—
# 那是拆包前的旧世界：ssh 模式现在有一批按名注册的只读工具，其中最常用的
# file_read/file_glob/file_grep 不在集合里，等于「一口气读 5 个文件」这个
# 最典型的批量读取形态永远触发不了渐进收集引导。
_READ_TOOLS = frozenset({
    "web_search", "web_fetch",
    "file_read", "file_glob", "file_grep", "log_read",
})


def detect_batch_reading(tool_calls: list, threshold: int = 3) -> bool:
    """
    检测 LLM 是否在一次回复中批量发起多个读取操作

    Args:
        tool_calls: LLMOutput.tool_calls 列表
        threshold: 触发阈值

    Returns:
        True 表示检测到批量读取，应注入渐进引导
    """
    read_count = sum(1 for tc in tool_calls if tc.name in _READ_TOOLS)
    return read_count >= threshold


def get_progressive_gathering_guide(iteration: int) -> str:
    """
    渐进式信息收集引导语

    当检测到 LLM 一次性发起多个读取操作时注入，引导它采用
    「先概览 → 判断 → 再深入」的策略。
    """
    if iteration <= 1:
        return (
            "\n\n[信息收集策略] 检测到你在一次性读取多个文件/目录。"
            "请采用渐进式收集策略：\n"
            "1. 先用 shell_exec（ls -la / find）获取目录结构概览\n"
            "2. 基于概览判断哪些文件真正相关\n"
            "3. 每次只看 1-2 个最相关的文件（head/tail 片段即可），看完评估是否足够\n"
            "4. 如果信息不够再看下一个，而不是一次性读完所有可能相关的文件\n"
            "\n批量读取会消耗上下文窗口，且很多文件读完后发现并不需要。"
        )
    else:
        return (
            "\n\n[信息收集策略] 你在执行阶段仍在一批次读取多个文件。"
            "请聚焦当前步骤真正需要的文件，读完后基于结果决定下一步，"
            "而不是预先读取所有可能相关的文件。"
        )


def get_reasoning_budget_for_iteration(
    intention: IntentionResult,
    iteration: int,
    max_budget: Optional[int] = None,
) -> Optional[int]:
    """
    根据当前迭代轮次返回 reasoning token 预算

    渐进式预算分配策略（取代原先的"首轮宽、后续严"简单二分）：
    - 首轮 + 允许规划 → 完整预算（规划需要充分思考）
    - 首轮 + 不允许规划 → 70% 预算（避免一次性深度思考耗尽）
    - 第 2-3 轮（信息收集阶段）→ 50% 预算（聚焦当前一步）
    - 第 4+ 轮（执行阶段）→ 40% 预算（快速决策）

    max_budget = 模型配置的 reasoning_tokens 上限。driver 每轮把本函数的返回值
    直接赋给 ctx.config.max_reasoning_tokens，因此这里必须自己再钳一次：
    intention.reasoning_budget 是未钳制的等级预算，超过模型上限的值会让下一轮
    LLM 调用带 reasoning_max_tokens 超限 → 400 → 整个 run 失败
    （classify 路径由 driver 的 min() 防住，reclassify 路径由 max_budget 参数防住，
    逐轮调整这条路径原先两边都没防）。
    """
    if intention.reasoning_budget is None:
        return None

    base = intention.reasoning_budget

    def _cap(value: int) -> int:
        if max_budget is None:
            return value
        return min(value, max_budget)

    # 首轮 + 规划阶段：完整预算
    if iteration == 0 and intention.allow_planning_phase:
        return _cap(base)

    # 首轮但不规划：70%（避免首次思考就过长）
    if iteration == 0:
        return _cap(max(int(base * 0.7), 256))

    # 第 2-3 轮：信息收集阶段，50%（聚焦单步决策）
    if iteration <= 3:
        return _cap(max(int(base * 0.5), 256))

    # 第 4+ 轮：执行阶段，40%（快速决策+执行）
    return _cap(max(int(base * 0.4), 256))
