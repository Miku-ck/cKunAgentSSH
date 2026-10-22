/**
 * 审批结算的用户可见层
 *
 * 结算本身在 stores/agent.ts（approvePermission / denyPermission）—— 审批 dock 的
 * 逐条与批量入口走的是同一条。这里只包一层"后端失败才提示、重复点击静默"，
 * 两个入口各写一遍的话，迟早变成一边弹错一边不弹。
 */
import { useAgentStore } from '@/stores/agent'
import { useToastStore } from '@/stores/toast'
import type { GrantScope } from '@/utils/approval-scopes'

export function useApprovalActions() {
  const agent = useAgentStore()
  const toast = useToastStore()

  /** approvePermission 返回三态：skipped（已结算/结算中）不该打扰用户，只有 failed 弹错 */
  async function approve(id: string, scope: GrantScope): Promise<void> {
    if (await agent.approvePermission(id, scope) === 'failed') toast.error('授权失败，请重试')
  }

  async function deny(id: string): Promise<void> {
    if (!await agent.denyPermission(id)) toast.error('拒绝登记失败，请重试')
  }

  /**
   * 批量结算。两个细节：
   * - **先快照 id 再逐条走正常流程**：approvePermission/denyPermission 自身幂等
   *   （resolved/settling 自动跳过），循环期间新到的卡不在这份快照里，因此不会被
   *   "全部允许"顺手批掉 —— 批量批准一条还没弹给用户的命令是不可逆的。
   * - **按结果而不是按返回值报数**：denyPermission 对"已结算"也返回 false，
   *   拿返回值累加会把幂等跳过报成失败。结算完还剩几条，就是没批掉几条。
   */
  async function settleAll(action: 'approve' | 'deny'): Promise<void> {
    const ids = agent.pendingPermissions.map(r => r.id)
    if (ids.length === 0) return
    for (const id of ids) {
      if (action === 'approve') await agent.approvePermission(id, 'once')
      else await agent.denyPermission(id)
    }
    const left = agent.pendingPermissions.length
    if (left > 0) toast.error(`${left} 条没结算掉，请逐条重试`)
  }

  return { approve, deny, settleAll }
}
