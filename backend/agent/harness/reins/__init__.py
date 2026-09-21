"""Reins — 缰绳（统一策略层包）

原 reins.py（2200 行）按关注点拆分为包；本 __init__ 保持与原模块一致的导入面
（生产代码与测试均按 ``from agent.harness.reins import X`` 引用）。

- core.py          Reins 容器 + 共享常量（_COMMAND_EXEC_TOOLS / 审批超时）与助手
                   （_perm_pending / _audit_target_* / tool_call_signature）
- guards.py        FilesystemGuard（命令分类阻断 + dangerous 审批闭环）、AuditLogger
- gate.py          PermissionGate（三态授权 + prod 预授权 allowlist）与进程级单例
- flow.py          LoopDetector / ConfidenceMonitor / DynamicBudgetRein / CostBudgetRein
- lifecycle.py     SkillRein（AGENT_START 技能注入）、ReflectionRein（终态反思/回收）
- clarification.py 澄清向导（pending 存储 + 检测器/题集注册表 + 拦截器）
- defaults.py      create_default_reins 装配

测试注意：monkeypatch 模块级可重绑定名（append_record_async / ALLOWLIST_PATH /
_global_permission_gate / _CLARIFICATION_MAX_ENTRIES）必须 patch 定义它们的子模块
（如 agent.harness.reins.gate），patch 包属性不会传导至子模块全局。
"""

from .clarification import (
    _CLARIFIABLE_TOOLS,
    _CLARIFICATION_CONTEXT,
    _CLARIFICATION_DONE,
    _CLARIFICATION_MAX_ENTRIES,
    ClarificationInterceptor,
    _build_clarification_needed,
    _enforce_clarification_cap,
    _file_write_build_steps,
    _file_write_detect_vague,
    _format_answers,
    _purge_expired_clarifications,
    _service_build_steps,
    _service_detect_vague,
    _shell_build_steps,
    _shell_detect_vague,
    _step_to_dict,
    record_clarification_answers,
    release_clarification_run,
)
from ..contracts import ClarificationOption, ClarificationStep
from .core import (
    _COMMAND_EXEC_TOOLS,
    _PERMISSION_APPROVAL_FALLBACK_S,
    Reins,
    tool_call_signature,
)
from .defaults import create_default_reins
from .flow import CostBudgetRein, LoopDetector
from .gate import (
    PermissionGate,
    _get_global_permission_gate_sync,
)
from .guards import (
    AuditLogger,
    FilesystemGuard,
)
from .lifecycle import SkillRein

__all__ = [
    "Reins",
    "create_default_reins",
    "FilesystemGuard",
    "LoopDetector",
    "PermissionGate",
    "AuditLogger",
    "CostBudgetRein",
    "SkillRein",
    "ClarificationInterceptor",
    "record_clarification_answers",
    "release_clarification_run",
    "_get_global_permission_gate_sync",
    "tool_call_signature",
]
