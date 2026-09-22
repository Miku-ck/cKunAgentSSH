"""默认 Reins 装配 — create_default_reins。拆分自原 reins.py。"""

from __future__ import annotations

from typing import Any, Optional

from ..contracts import Interceptor, ReinPoint
from .clarification import ClarificationInterceptor
from .core import Reins
from .flow import ConfidenceMonitor, CostBudgetRein, DynamicBudgetRein, LoopDetector
from .gate import _get_global_permission_gate_sync
from .guards import AuditLogger, FilesystemGuard
from .lifecycle import ReflectionRein, SkillRein
from .plan_mode import PlanDisciplineRein, PlanModeGuard

def create_default_reins(
    config: Optional[Any] = None,
    saddle: Optional[Any] = None,
) -> Reins:
    """创建默认 Reins — 注册所有内置拦截器

    Args:
        config: AgentConfig，用于控制各拦截器的启停与参数。
                None 时使用默认配置（全部启用）。
        saddle: Saddle 引用，PermissionGate 从 ToolRegistry 读取 ToolMeta（单一数据源）。
                None 时 PermissionGate 无法判定风险等级，所有工具都会被拦截。

    H13 修复 A6：PermissionGate 使用进程级单例，跨 run 共享 session scope 授权
    """
    # H13 修复 A6：使用进程级单例 PermissionGate
    permission_gate = _get_global_permission_gate_sync(saddle=saddle)
    # H13 修复 A6 bug：每次 run 都更新 saddle 引用
    # 原实现 `if saddle is not None and permission_gate.saddle is None` 只在首次注入 saddle，
    # 但每个 Harness 实例都会创建新的 Saddle（新 ToolRegistry），
    # 导致第二次 run 时单例仍持有旧 Saddle，查询工具会失败（"工具未注册"）
    if saddle is not None:
        permission_gate.saddle = saddle

    reins = Reins()

    interceptors: list[Interceptor] = [
        SkillRein(),  # AGENT_START: 技能注入
        FilesystemGuard(permission_gate=permission_gate, saddle=saddle),
        LoopDetector(),
        permission_gate,
        AuditLogger(),
    ]

    # 动态步数预算（POST_ITERATION）
    if config is None or config.enable_dynamic_budget:
        budget_rein = DynamicBudgetRein()
        if config is not None:
            budget_rein.file_mutation_interval = config.dynamic_budget_interval
            budget_rein.step_per_interval = config.dynamic_budget_step
            budget_rein.max_multiplier = config.dynamic_budget_cap
        interceptors.append(budget_rein)

    # C-review M10：成本/token 硬熔断（POST_ITERATION，priority=1 先于动态预算，
    # 触发后短路终止 — 最坏 1000 次 LLM 调用无人切断的问题由此兜底）
    interceptors.append(CostBudgetRein())

    interceptors.append(ConfidenceMonitor())

    # 反思触发（AGENT_COMPLETE + AGENT_FAILED）
    # 注册两个实例，分别绑定到不同 ReinPoint，确保成功/失败/超上限都会推送 reflection_request
    # 原实现 AGENT_FAILED 没有拦截器，导致失败运行错过反思，演化数据偏斜
    # P1 竞态修复：传递 permission_gate，运行终止时自动清理 once 授权池
    reflection_complete = ReflectionRein(permission_gate=permission_gate)
    interceptors.append(reflection_complete)  # AGENT_COMPLETE
    reflection_failed = ReflectionRein(point=ReinPoint.AGENT_FAILED, permission_gate=permission_gate)
    interceptors.append(reflection_failed)

    # ── 澄清向导（PRE_TOOL_CALL）──
    interceptors.append(ClarificationInterceptor())

    # ── 计划模式（Plan Mode）──
    # 制品落盘（spec/plan/goal.md）与 system prompt 注入由 agent/workflow.py 承担；
    # 这里补的是它明确不做的另一半 —— **只读强制**。workflow.py 自述「不做验收机…
    # 阶段推进与成功判定交由模型 + 用户决定，本模块不越权」，于是 plan_write 的
    # description 里那句「应等待用户批准计划后再动手执行」只是自觉、不是机制。
    # 本模块把「当前处于 plan 阶段」从提示词约定升级成会被强制执行的权限状态。
    #
    # 两者都先看 ctx.metadata["permission_mode"]，非 "plan" 时返回**全默认的
    # ReinDecision()**（逐字段零差异），所以注册后对既有运行零影响 ——
    # test_plan_mode.py 有 4 mode × 4 tool 的参数化回归钉住这一点。
    #
    # PlanModeGuard 的 priority=6 早于三个会弹审批卡的拦截器（ClarificationInterceptor 7、
    # PermissionGate 8、FilesystemGuard 10）：Reins.check() 首个
    # intercepted=True 即返回，plan 阶段的写操作应当**直接拒绝**，而不是先弹一张
    # 审批卡让用户点 —— 用户点了「始终允许」也不该在计划阶段生效（对齐 Claude Code
    # 「File edits are never auto-approved in plan mode, even when an allow rule matches」）。
    interceptors.append(PlanModeGuard(saddle=saddle))   # PRE_TOOL_CALL, priority 6
    interceptors.append(PlanDisciplineRein())           # PRE_ITERATION,  priority 12

    reins.register_all(interceptors)
    return reins
