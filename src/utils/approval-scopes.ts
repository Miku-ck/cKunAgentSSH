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
