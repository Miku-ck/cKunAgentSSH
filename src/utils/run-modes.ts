/**
 * 对话姿态 + 审批档位 → 后端 AgentRunRequest 的**成对**字段映射。
 *
 * 「计划模式」在后端落成两个字段，各管一件事：
 *   workflow_mode:   'plan' → 注入计划姿态的 system prompt（只影响措辞）
 *   permission_mode: 'plan' → PlanModeGuard 只读强制（真正拦住写操作）
 *
 * 两者必须同时下发。只发前者就是「亮的空开关」：用户以为进了计划模式、
 * 后端却照常执行写操作 —— 前端 UI 亮着、约束不存在。
 * 拆成两处各写一遍条件，正是这种漂移最容易发生的地方，故收敛为一个函数：
 * 它一次返回两个字段，调用方无法只取其一。
 *
 * 审批轴（normal=手动 / auto=模型代判）是独立于姿态的另一维：
 * 但 plan 姿态下审批轴休眠 —— PlanModeGuard 在任何审批卡之前就拦写操作，
 * 此时发 'auto' 只会让用户误以为"计划模式还会弹审批卡让模型代判"。
 * 所以姿态为 plan 时 permissionMode 恒为 'plan'，approval 被忽略。
 */
export interface RunModes {
  workflowMode: 'goal' | 'plan'
  permissionMode: 'normal' | 'auto' | 'plan'
}

/** 唯一的姿态+审批判定口径：除 'plan' 姿态外一律普通工作流；审批档仅认 'auto'，其余手动 */
export function resolveRunModes(agentMode?: string, approval?: string): RunModes {
  const plan = agentMode === 'plan'
  return {
    workflowMode: plan ? 'plan' : 'goal',
    permissionMode: plan ? 'plan' : approval === 'auto' ? 'auto' : 'normal',
  }
}
