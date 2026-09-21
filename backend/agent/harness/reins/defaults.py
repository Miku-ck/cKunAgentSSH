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

    reins.register_all(interceptors)
    return reins
