/**
 * 审批卡可见的授权档位 — 与后端 PermissionGate 的判定口径单点对齐。
 *
 * 后端契约（gate.check_permission，B-6）：risk === 'dangerous' 时忽略 session 级
 * 缓存，dangerous 需每次授权。因此 dangerous 卡不提供「本会话允许」——按钮存在
 * 却不被后端兑现就是假承诺。除精确 'moderate' 外的值（含空/脏值）一律按
 * dangerous 的保守口径处理。
 */
export type GrantScope = 'once' | 'session' | 'always'

export function grantScopesForRisk(risk: string): GrantScope[] {
  if (risk === 'moderate') return ['once', 'session', 'always']
  return ['once', 'always']
}

/**
 * once 授权入口是否必须禁用：once 按 run 键控（后端 `tool:<sha16>` 键 + run 隔离，
 * run 终态时 `cleanup_run_once` 清池）。run 已结束后点击只会落进一个永不被消费的
 * 死键 —— 后端现在直接 409，前端必须提前禁用，别让用户点出一个假「已授权」。
 * session（按会话键控）与 always（永久）跨 run 仍可消费，不受影响。
 */
export function onceGrantBlocked(runRunning: boolean): boolean {
  return !runRunning
}

/**
 * 「全部批准」必须禁用的情形：这批里只要有一条不可批量放行，整批都不给一键。
 *
 * 一次点击批掉多条命令，用户根本来不及逐条读后果，而卡片区是可滚动的 ——
 * 滚出视口的那条同样被批掉。因此只有低风险（safe/moderate）且目标不是生产
 * 环境的批次才允许批量；dangerous、空/脏风险值（与 grantScopesForRisk 同一条
 * fail-closed 口径）与 prod 一律要求逐条确认。
 */
const BATCH_APPROVABLE_RISKS = ['safe', 'moderate']

export function batchApproveBlocked(
  requests: ReadonlyArray<{ risk: string; environment?: string }>,
): boolean {
  return requests.some(r => r.environment === 'prod' || !BATCH_APPROVABLE_RISKS.includes(r.risk))
}
