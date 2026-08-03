"""
Harness — 驾驭工程框架

把 AI 模型视为"被驾驭者"，通过一套工程化的 Harness 装备来约束、引导、监控、优化模型行为。

组件命名借用马术装备：
- Driver   驭手 — ReAct 决策循环的执行者
- Bridle   笼头 — 输入组装 + 上下文压缩
- Reins    缰绳 — 策略点 + 拦截器（统一 hooks/permissions/self_monitor）
- Saddle   马鞍 — 工具执行 + 沙箱 + Undo
- Stirrups 马镫 — 错误恢复 + LLM 重试 + 回滚
- Stables  马厩 — 长期记忆
- Forge    锻造炉 — 反思 + 演化
- Pasture  牧场 — 后台心智维护
- TelemetryBus 遥测总线 — 统一事件流
"""

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

