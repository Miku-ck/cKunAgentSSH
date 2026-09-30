/**
 * 审批档归属（spec 2026-09-29-home-command-panel · 接缝 S3 前置）
 *
 * approvalMode 原先是 SideChatPanel.vue:739 的组件局部 ref：首页输入卡要放一个
 * 审批下拉就只能自己再存一份，两处各持一档 = 第二条通路（用户在首页选「自动」，
 * 侧栏仍按「手动」发送）。提到 store 后两处读同一档。
 *
 * 刻意**不持久化** —— 与提升前一致：审批档是本次会话的意图，重启回到手动。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// 测试跑在 node 环境（vitest.config.ts），补 localStorage 最小垫片
const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

// store 工厂在 setup 内 fire-and-forget 调 syncBackendConversations，
// 不给垫片会在每次 import 时打真网络并留下未处理的 rejection。
vi.mock('@/api/client', () => ({
  api: {
    listConversations: vi.fn(async () => []),
    getDefaultModel: vi.fn(async () => undefined),
  },
  workflowApi: { get: vi.fn(async () => null) },
  agentApi: { grantPermission: vi.fn(async () => ({})), denyCommand: vi.fn(async () => ({})) },
  streamAgentRun: vi.fn(() => () => {}),
}))

const { useSshAiStore } = await import('@/stores/ssh-ai')

describe('ssh-ai 审批档', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('默认手动审批', () => {
    expect(useSshAiStore().approvalMode).toBe('normal')
  })

  it('setApprovalMode 切换档位', () => {
    const store = useSshAiStore()
    store.setApprovalMode('auto')
    expect(store.approvalMode).toBe('auto')
    store.setApprovalMode('normal')
    expect(store.approvalMode).toBe('normal')
  })

  it('切换审批档不落 localStorage', () => {
    useSshAiStore().setApprovalMode('auto')
    expect([...lsMap.keys()].filter(k => k.includes('approval'))).toHaveLength(0)
  })
})

/**
 * 对话姿态（目标/计划）—— 故事 5 的 ＋ 菜单项要落在首页，而它原先是
 * SideChatPanel.vue:649 的组件私有 ref。与审批档同一条理由：两处各存一份
 * 就是第二条通路，用户在首页点「计划」、侧栏仍按未表态发送。
 */
describe('ssh-ai 姿态轴', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('默认未表态（null），不是 goal —— 缺省 goal 会让「目标」chip 关不掉', () => {
    expect(useSshAiStore().posture).toBeNull()
  })

  it('setPosture 点亮与退出', () => {
    const store = useSshAiStore()
    store.setPosture('plan')
    expect(store.posture).toBe('plan')
    store.setPosture(null)
    expect(store.posture).toBeNull()
  })

  it('点亮计划不覆写审批档：休眠只发生在发送装配时，退出计划回到用户原档', () => {
    const store = useSshAiStore()
    store.setApprovalMode('auto')
    store.setPosture('plan')
    expect(store.approvalMode).toBe('auto')
    store.setPosture(null)
    expect(store.approvalMode).toBe('auto')
  })

  it('姿态与审批档互不落 localStorage（本次会话的意图，重启回默认）', () => {
    const store = useSshAiStore()
    store.setPosture('plan')
    store.setApprovalMode('auto')
    expect([...lsMap.keys()].filter(k => /approval|posture|mode/i.test(k))).toHaveLength(0)
  })
})
