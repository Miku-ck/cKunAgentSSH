"""
Harness — 驾驭工程框架

把 AI 模型视为"被驾驭者"，通过一套工程化的 Harness 装备来约束、引导、监控、优化模型行为。

组件命名借用马术装备：
- Driver   驭手 — ReAct 决策循环的执行者
- Bridle   笼头 — 输入组装 + 上下文压缩
- Reins    缰绳 — 策略点 + 拦截器（统一 hooks/permissions/self_monitor）
- Saddle   马鞍 — 工具执行 + 沙箱 + Undo
- Stirrups 马镫 — 错误恢复 + LLM 重试 + 回滚
- TelemetryBus 遥测总线 — 统一事件流

注：长期记忆（agent.memory）、反思/演化（agent.reflection / agent.evolution）
与后台维护（agent.consciousness_tick）是 Harness 外围模块，不属于本包组件，
勿在此虚构对应命名。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期直接可见
    from .harness import Harness, HarnessConfig

# H13 修复 C10：导出策略说明
# 仅导出对外公开 API（Harness / HarnessConfig）。
# 内部组件（Driver/Bridle/Reins/Saddle/Stirrups/TelemetryBus）通过 Harness 间接访问，
# 不直接导出，避免调用方绕过 Harness 直接操作内部组件破坏封装。
# 如需测试或高级用法，可显式 from agent.harness.xxx import Yyy。

__all__ = [
    "Harness",
    "HarnessConfig",
]


def __getattr__(name: str):
    """按需求值导出 Harness / HarnessConfig（PEP 562）。

    本文件原先在末尾直接 `from .harness import Harness, HarnessConfig`，于是任何
    `import agent.harness.reins.xxx` / `agent.harness.contracts` 都会先跑本包
    __init__，把 harness.py → driver.py → reins 全套拖进导入链（实测 70ms+）。
    而后端启动阶段只需要 reins.gate 里的几个 setter（see main._load_permission_policy），
    并不需要 Harness 本体。改为惰性导出后 `from agent.harness import Harness`
    写法完全不变，只用到子模块的调用方不再付这份成本。
    """
    if name in __all__:
        from .harness import Harness, HarnessConfig
        return {"Harness": Harness, "HarnessConfig": HarnessConfig}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

