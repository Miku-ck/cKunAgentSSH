/**
 * 审批档位 → 后端 AgentRunRequest.permission_mode 的映射（M1 结构化重构）。
 *
 * 旧「对话姿态（goal/plan chip）+ workflow_mode 字段」已随结构化 goal/plan
 * 重构移除：计划只读档不再由前端选择 —— 会话存在等待批准 / 已批准 / 执行中的
 * 结构化 plan 时，服务端强制本次 run permission_mode='plan'（不信任客户端），
 * PlanModeGuard 才是真正的拦截；前端只保留审批轴。
 *
 * 审批轴（normal=手动 / auto=模型代判）：
 * 'auto' 时 reins.auto_approve 在弹审批卡前先让模型裁决一次，超时/异常/存疑
 * 一律照旧弹卡问用户（fail-closed）。prod 预授权强化与 hardline 阻断不受影响。
 */
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
 * 那是结构化 plan 待批准 / 执行中的只读强制档（服务端推导），用户点的是
 * 「手动 / 自动」而不是这第三档；run 头部展示它，是因为只读档的 UI 状态
 * 必须如实可见。
 */
export type PermissionModeKey = 'normal' | 'auto' | 'plan'

/** 未表态时后端实际跑的档位。请求体缺省与这里共用同一常量，
 *  免得"发出去的档位"和"UI 显示的档位"各写一遍默认值后对不上。 */
export const PERMISSION_MODE_DEFAULT: PermissionModeKey = 'normal'

/** run-head 上那一格审批方式的名字。plan 档文案与服务端推导档共用。 */
export const PERMISSION_LABELS: Record<PermissionModeKey, string> = {
  ...APPROVAL_LABELS,
  plan: '只读计划',
}

/**
 * 审批档位的图标，首页输入卡与运行通知区共用 —— 路径在两处各写一遍，改一处漏一处
 * 就是"同一个档位两副面孔"。底图都是那张盾（"这一枚管的是放行"），盾里的记号
 * 区分档位：手动 = 空盾（逐条由你按）；自动 = 一道闪电（模型代判）；只读计划 =
 * 一横（写操作根本不进审批）。记号按 PermissionModeKey 穷举 —— 漏一档是编译错误。
 */
export const PERM_SHIELD = 'M12 3l7 3v5c0 4.6-3 8.2-7 10-4-1.8-7-5.4-7-10V6z'

export const PERM_MARKS: Record<PermissionModeKey, string | null> = {
  normal: null,
  auto: 'M13.2 7.2l-3.4 4.6h2.3l-.8 3.2 3.4-4.5h-2.2z',
  plan: 'M9.2 12.2h5.6',
}

interface RunModes {
  permissionMode: PermissionModeKey
}

/**
 * 唯一的审批档判定口径。agentMode 参数已随姿态移除，保留无效入参容忍
 * （历史调用方传了也不会崩），只按 approval 求档。
 */
export function resolveRunModes(_agentMode?: string, approval?: string): RunModes {
  return { permissionMode: approval === 'auto' ? 'auto' : 'normal' }
}
