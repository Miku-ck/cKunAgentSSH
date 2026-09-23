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
/**
 * 对话姿态 = 工具条模式 chip 的 kind。
 * 侧栏组件、父面板与下面的映射函数共用它：字面量在两处各写一遍是本仓库
 * 反复出现的"跨层 key 漂移"来源（改了一处、另一处静默失配）。
 */
export type PostureKey = 'goal' | 'plan'

interface RunModes {
  /** undefined = 未表态：字段省略，后端不注入任何姿态段 */
  workflowMode?: PostureKey
  permissionMode: 'normal' | 'auto' | 'plan'
}

/**
 * 唯一的姿态+审批判定口径。
 *
 * agentMode 三态：'plan' / 'goal' / 其它（含未表态）。只有 'goal' 算目标姿态，
 * 未表态返回 undefined —— 因为「没选」和「选了目标」在后端是两件事：前者不注入
 * 姿态提示词，后者注入。把它们混为一谈，chip 的 × 就只是把同一个 'goal' 又发一遍。
 */
export function resolveRunModes(agentMode?: string, approval?: string): RunModes {
  const plan = agentMode === 'plan'
  return {
    workflowMode: plan ? 'plan' : agentMode === 'goal' ? 'goal' : undefined,
    permissionMode: plan ? 'plan' : approval === 'auto' ? 'auto' : 'normal',
  }
}
