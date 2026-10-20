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
 * 输入卡、run 头部与下面的映射函数共用它：字面量在两处各写一遍是本仓库
 * 反复出现的"跨层 key 漂移"来源（改了一处、另一处静默失配）。
 */
export type PostureKey = 'goal' | 'plan'

/**
 * 审批档 = 用户可选的两种「谁来批准」。
 * 文案在此单点定义：首页输入卡与 run 头部各写一份的话，改了一处，两处就会对同样
 * 两个档位给出不同的说明 —— 而用户看不出自己点的到底是哪个。
 */
export type ApprovalKey = 'normal' | 'auto'

export const APPROVAL_OPTIONS: ReadonlyArray<{ key: ApprovalKey; label: string; desc: string }> = [
  { key: 'normal', label: '手动审批', desc: '危险操作逐条确认' },
  { key: 'auto', label: '自动审批', desc: '模型代判，拿不准仍弹卡' },
]

export const APPROVAL_LABELS: Record<ApprovalKey, string> = { normal: '手动审批', auto: '自动审批' }

/**
 * 后端 AgentRunRequest.permission_mode 的取值域。比审批档多一个 'plan'：
 * 那是计划姿态的只读强制档，用户点的是「计划」而不是这第三档。
 */
export type PermissionModeKey = 'normal' | 'auto' | 'plan'

/** 未表态时后端实际跑的档位。run 归因与请求体共用这一个常量，
 *  免得"发出去的档位"和"头部显示的档位"各写一遍默认值后对不上。 */
export const PERMISSION_MODE_DEFAULT: PermissionModeKey = 'normal'

/** run-head 上那一格审批方式的名字。两档文案复用 APPROVAL_LABELS，
 *  避免同一个按钮在首页叫「自动审批」、在 run 头部叫别的。 */
export const PERMISSION_LABELS: Record<PermissionModeKey, string> = {
  ...APPROVAL_LABELS,
  plan: '只读计划',
}

/**
 * 审批档位的图标，首页输入卡与运行通知区共用 —— 路径在两处各写一遍，改一处漏一处
 * 就是"同一个档位两副面孔"。底图都是那张盾（"这一枚管的是放行"），盾里的记号
 * 区分档位：手动 = 空盾（逐条由你按）；自动 = 一道闪电（模型代判）；只读计划 =
 * 一横（写操作根本不进审批）。共用工盾会骗人：切到自动审批，界面上什么都没变。
 * 记号按 PermissionModeKey 穷举 —— 漏一档是编译错误，不是"那一档悄悄没记号"。
 */
export const PERM_SHIELD = 'M12 3l7 3v5c0 4.6-3 8.2-7 10-4-1.8-7-5.4-7-10V6z'

export const PERM_MARKS: Record<PermissionModeKey, string | null> = {
  normal: null,
  auto: 'M13.2 7.2l-3.4 4.6h2.3l-.8 3.2 3.4-4.5h-2.2z',
  plan: 'M9.2 12.2h5.6',
}

interface RunModes {
  /** undefined = 未表态：字段省略，后端不注入任何姿态段 */
  workflowMode?: PostureKey
  permissionMode: PermissionModeKey
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
