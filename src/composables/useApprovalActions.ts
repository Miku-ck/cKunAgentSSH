/**
 * 审批结算的用户可见层
 *
 * 结算本身在 stores/agent.ts（approvePermission / denyPermission）—— 首页待放行
 * 队列与侧栏审批托盘走的是同一条。这里只包一层"后端失败才提示、重复点击静默"，
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

  return { approve, deny }
}
