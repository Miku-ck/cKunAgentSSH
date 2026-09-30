/**
 * 审批结算的用户可见层（spec 2026-09-30-retire-ai-side-panel · R1）
 *
 * 批量结算原先长在侧栏托盘里（grantAll/denyAll）。托盘退役后它搬到
 * useApprovalActions —— 搬的是同一条理由：两个入口各写一遍循环，
 * 迟早变成一边弹错一边不弹。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

type Req = { id: string; resolved: boolean }

const permissionRequests = ref<Req[]>([])
const approvePermission = vi.fn(async (id: string): Promise<'granted' | 'skipped' | 'failed'> => {
  const r = permissionRequests.value.find(x => x.id === id)
  if (!r || r.resolved) return 'skipped'
  r.resolved = true
  return 'granted'
})
const denyPermission = vi.fn(async (id: string) => {
  const r = permissionRequests.value.find(x => x.id === id)
  // 与真实 store 同形：已结算/settling 也返回 false（幂等跳过，不是失败）
  if (!r || r.resolved) return false
  r.resolved = true
  return true
})
const error = vi.fn()

vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({
    // pinia 把 getter 解包成裸值，mock 必须同形
    get pendingPermissions() {
      return permissionRequests.value.filter(r => !r.resolved)
    },
    approvePermission,
    denyPermission,
  }),
}))

vi.mock('@/stores/toast', () => ({
  useToastStore: () => ({ error }),
}))

const { useApprovalActions } = await import('@/composables/useApprovalActions')

beforeEach(() => {
  permissionRequests.value = []
  approvePermission.mockClear()
  denyPermission.mockClear()
  error.mockClear()
})

describe('useApprovalActions 批量结算', () => {
  it('全部批准把待处理清空，且一条错误都不弹', async () => {
    permissionRequests.value = [{ id: 'a', resolved: false }, { id: 'b', resolved: false }]
    await useApprovalActions().settleAll('approve')
    expect(permissionRequests.value.filter(r => !r.resolved)).toEqual([])
    expect(error).not.toHaveBeenCalled()
  })

  it('只批快照里那几条 —— 循环途中新到的卡不该被"全部允许"顺手批掉', async () => {
    // 批量批准一条还没弹给用户的新命令是不可逆的：审批的意义就是那条命令必须由人点头。
    permissionRequests.value = [{ id: 'a', resolved: false }]
    approvePermission.mockImplementationOnce(async (id: string) => {
      permissionRequests.value.push({ id: 'late', resolved: false })
      const r = permissionRequests.value.find(x => x.id === id)!
      r.resolved = true
      return 'granted'
    })
    await useApprovalActions().settleAll('approve')
    expect(approvePermission).toHaveBeenCalledTimes(1)
    expect(approvePermission).toHaveBeenCalledWith('a', 'once')
  })

  it('按结果报数而不是按返回值：denyPermission 对已结算也返回 false，幂等跳过不能弹成失败', async () => {
    permissionRequests.value = [{ id: 'a', resolved: true }, { id: 'b', resolved: true }]
    await useApprovalActions().settleAll('deny')
    expect(error).not.toHaveBeenCalled()
  })

  it('有剩条才弹一次错，并说清剩几条', async () => {
    permissionRequests.value = [{ id: 'a', resolved: false }, { id: 'b', resolved: false }]
    // a 正常结算，b 在后端那侧没成（保持未 resolved）
    denyPermission.mockImplementationOnce(async () => false)
    await useApprovalActions().settleAll('deny')
    expect(error).toHaveBeenCalledTimes(1)
    expect(error).toHaveBeenCalledWith('1 条没结算掉，请逐条重试')
  })

  it('队列空时一次请求都不发', async () => {
    await useApprovalActions().settleAll('approve')
    expect(approvePermission).not.toHaveBeenCalled()
    expect(denyPermission).not.toHaveBeenCalled()
  })
})

describe('useApprovalActions 单条结算', () => {
  it('skipped（已结算/结算中）不打扰用户，只有 failed 弹错', async () => {
    approvePermission.mockResolvedValueOnce('skipped')
    await useApprovalActions().approve('a', 'once')
    expect(error).not.toHaveBeenCalled()
    approvePermission.mockResolvedValueOnce('failed')
    await useApprovalActions().approve('a', 'once')
    expect(error).toHaveBeenCalledWith('授权失败，请重试')
  })

  it('拒绝登记失败要弹 —— 静默的"没拒掉"比报错更难查', async () => {
    denyPermission.mockResolvedValueOnce(false)
    await useApprovalActions().deny('a')
    expect(error).toHaveBeenCalledWith('拒绝登记失败，请重试')
  })
})
