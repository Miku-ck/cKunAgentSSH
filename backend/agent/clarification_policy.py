"""澄清策略：纯函数的风险分级与未答出口决策。

本模块不读写 RunContext，不导入 harness；所有副作用（权限模式切换）在
clarification_mode.py。Wire 字段 `risk` 的值域是 low/medium/high，与已有的
ToolMeta.risk_level（safe/moderate/dangerous）是不同概念，禁止混用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

OUTCOME_ANSWERED = "answered"
OUTCOME_SKIPPED = "skipped"
OUTCOME_EXPIRED = "expired"
OUTCOME_UNATTENDED = "unattended"
OUTCOME_BLOCKED = "blocked"
# 用户主动终止整个任务。与 skipped 的区别：skipped 只作废这一次调用、任务继续；
# cancelled 表示 run 本身要终止，策略值实际不会被 driver 消费（它直接退出），
# 但矩阵仍要给答案，避免 decide_policy 落进 fail-closed 默认分支产生误导性审计。
OUTCOME_CANCELLED = "cancelled"

POLICY_NONE = "none"
POLICY_DOWNGRADE_NORMAL = "downgrade_normal"
POLICY_READONLY_FALLBACK = "readonly_fallback"
POLICY_BLOCKED = "blocked"

KIND_INTENT = "intent"
KIND_SCOPE = "scope"
KIND_RISK = "risk"

# high：未答必须停；middle：未答转 plan；low：不额外限制。
_MATRIX = {
    RISK_HIGH: {
        OUTCOME_ANSWERED: POLICY_NONE,
        OUTCOME_SKIPPED: POLICY_BLOCKED,
        OUTCOME_EXPIRED: POLICY_BLOCKED,
        OUTCOME_UNATTENDED: POLICY_BLOCKED,
        OUTCOME_CANCELLED: POLICY_BLOCKED,
    },
    RISK_MEDIUM: {
        OUTCOME_ANSWERED: POLICY_DOWNGRADE_NORMAL,
        OUTCOME_SKIPPED: POLICY_READONLY_FALLBACK,
        OUTCOME_EXPIRED: POLICY_READONLY_FALLBACK,
        OUTCOME_UNATTENDED: POLICY_READONLY_FALLBACK,
        OUTCOME_CANCELLED: POLICY_BLOCKED,
    },
    RISK_LOW: {
        OUTCOME_ANSWERED: POLICY_NONE,
        OUTCOME_SKIPPED: POLICY_NONE,
        OUTCOME_EXPIRED: POLICY_NONE,
        OUTCOME_UNATTENDED: POLICY_NONE,
        OUTCOME_CANCELLED: POLICY_NONE,
    },
}

TOOL_FACT_KEYS = ("environment", "command_level", "tool_risk", "is_write", "tool")
TASK_FACT_KEYS = ("environment", "is_change", "destructive", "missing_slots")


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    missing_slots: tuple[str, ...] = field(default_factory=tuple)
    source: str = "rules"


def classify_task_risk(
    *,
    environment: str = "",
    is_change: bool = False,
    destructive: bool = False,
    missing_slots: tuple[str, ...] | list[str] = (),
) -> str:
    """任务级风险。

    missing_slots 只用于解释/审计，不抬高等级：非 prod 的写任务即使目标不清，
    仍是 medium，应降级为 plan 而不是直接终止。high 只表示生产写或不可逆动作。
    """
    if not is_change:
        return RISK_LOW
    if str(environment or "").lower() == "prod" or destructive:
        return RISK_HIGH
    return RISK_MEDIUM


def classify_tool_risk(
    *,
    environment: str = "",
    command_level: str = "",
    tool_risk: str = "",
    is_write: bool = False,
    **_rest,
) -> str:
    """工具级风险。

    command_level/tool_risk 使用现有安全分类口径；`tool_risk=="safe"` 不代表
    只读，只有 is_write=False 才可能是 low。
    """
    level = str(command_level or "").lower()
    if level in ("dangerous", "hardline"):
        return RISK_HIGH
    if str(tool_risk or "").lower() == "dangerous":
        return RISK_HIGH
    if str(environment or "").lower() == "prod" and is_write:
        return RISK_HIGH
    return RISK_MEDIUM if is_write else RISK_LOW


def decide_policy(risk: str, outcome: str) -> str:
    table = _MATRIX.get(str(risk), _MATRIX[RISK_HIGH])
    return table.get(str(outcome), POLICY_BLOCKED)


def skip_policy_for_tool(**facts) -> str:
    return decide_policy(classify_tool_risk(**facts), OUTCOME_SKIPPED)


def skip_policy_for_task(**facts) -> str:
    return decide_policy(classify_task_risk(**facts), OUTCOME_SKIPPED)


def task_risk_facts(task: str, environment: str, is_change: bool) -> dict:
    """稳定快照；后续建题、消费、超时都复用同一组键。"""
    return {
        "environment": str(environment or ""),
        "is_change": bool(is_change),
        "destructive": False,
        "missing_slots": (),
    }


def is_blocked(policy: str) -> bool:
    return policy == POLICY_BLOCKED


def is_readonly_fallback(policy: str) -> bool:
    return policy == POLICY_READONLY_FALLBACK
