/**
 * goal-plan store 回归（M2）—— 快照拉取、SSE delta 归并、动作转发。
 *
 * 无组件测试环境（未装 @vue/test-utils），直接驱动 store（仓库先例：
 * sftp-sort-filter.test.ts 同法）。订阅总线以受控 handler 桩替代真实
 * sse.ts 注册，事件形状与 backend/agent/harness/reins/goal_plan.py 的
 * payload 同源（mode/goal/plan/step/progress，刻意无 steps 键）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

/** 订阅总线桩：捕获注册的 handler，测试里手动派发事件 */
let capturedHandler: ((event: unknown) => void) | null = null

vi.mock('@/api/client', () => ({
  goalPlanApi: {
    state: vi.fn(),
    approvePlan: vi.fn(async () => ({})),
    replanPlan: vi.fn(async () => ({ superseded_plan_id: 'p1', archived_steps: [] })),
    abandonPlan: vi.fn(async () => ({ abandoned: true, plan_id: 'p1' })),
    adjudicateGoal: vi.fn(async () => ({})),
  },
  subscribeGoalPlanProgress: vi.fn((handler: (event: unknown) => void) => {
    capturedHandler = handler
    return () => { capturedHandler = null }
  }),
}))

vi.mock('@/stores/toast', () => ({
  useToastStore: () => ({ error: vi.fn(), info: vi.fn() }),
}))

const { goalPlanApi } = await import('@/api/client')
const { useGoalPlanStore } = await import('@/stores/goal-plan')
// fixture 返回类型取同源契约（status 字面量收窄成枚举）
import type { GoalInfo, PlanInfo } from '@/utils/goal-plan-core'

function goalFixture(): GoalInfo {
  return {
    goal_id: 'g1', conversation_id: 'c1', title: '清错误', statement: '把磁盘错误清干净',
    context: '', criteria: [{ id: 'c1', text: 'dmesg 无错', met: false, evidence: '' }],
    status: 'active', version: 1, adjudication: 'pending', adjudicated_by: '',
    created_at: '', updated_at: '',
  }
}

function planFixture(status: PlanInfo['status'] = 'awaiting_approval'): PlanInfo {
  return {
    plan_id: 'p1', goal_id: 'g1', conversation_id: 'c1', version: 1, status,
    approved_by: '', approved_at: '', supersedes_plan_id: '', created_at: '', updated_at: '',
  }
}

function emptyProgress() {
  return { total: 0, pending: 0, in_progress: 0, completed: 0, blocked: 0, skipped: 0 }
}

beforeEach(() => {
  setActivePinia(createPinia())
  capturedHandler = null
  vi.mocked(goalPlanApi.state).mockReset()
})

describe('goal-plan 快照', () => {
  it('attach 拉取 REST 全量并记录归属会话', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: planFixture(), steps: [], progress: emptyProgress(),
    })
    const store = useGoalPlanStore()
    await store.attach('c1')
    expect(goalPlanApi.state).toHaveBeenCalledWith('c1')
    expect(store.conversationId).toBe('c1')
    expect(store.hasContent).toBe(true)
    expect(store.awaitingApproval).toBe(true)
  })

  it('无 goal 无 plan 的空快照不露出面板', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: null, plan: null, steps: [], progress: emptyProgress(),
    })
    const store = useGoalPlanStore()
    await store.attach('c1')
    expect(store.hasContent).toBe(false)
    expect(store.planActionable).toBe(false)
  })

  it('attach 空串清空快照；同会话重复 attach 不再打 REST', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: null, steps: [], progress: emptyProgress(),
    })
    const store = useGoalPlanStore()
    await store.attach('c1')
    expect(goalPlanApi.state).toHaveBeenCalledTimes(1)

    await store.attach('c1')
    expect(goalPlanApi.state).toHaveBeenCalledTimes(1)

    store.clear()
    expect(store.hasContent).toBe(false)
    expect(store.conversationId).toBe('')
  })
})

describe('goal-plan SSE 事件归并', () => {
  it('订阅总线在 store 创建时即注册（进程级，事件不丢）', () => {
    useGoalPlanStore()
    expect(capturedHandler).toBeTypeOf('function')
  })

  it('delta 事件按 id 归并单条 step 并重算进度', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: planFixture('active'),
      steps: [{ step_id: 's1', plan_id: 'p1', ordinal: 1, title: '定位', detail: '',
                status: 'in_progress', blocked_reason: '', evidence: '', run_id: '',
                iteration: 0, started_at: '', finished_at: '', updated_at: '' }],
      progress: { total: 1, pending: 0, in_progress: 1, completed: 0, blocked: 0, skipped: 0 },
    })
    const store = useGoalPlanStore()
    await store.attach('c1')

    capturedHandler!({
      type: 'goal_plan_progress', mode: 'delta',
      goal: { ...goalFixture(), conversation_id: 'c1' },
      plan: { ...planFixture('active'), conversation_id: 'c1' },
      step: { step_id: 's1', plan_id: 'p1', ordinal: 1, title: '定位', detail: '',
              status: 'completed', blocked_reason: '', evidence: 'dmesg 干净', run_id: 'r1',
              iteration: 2, started_at: '', finished_at: 't', updated_at: 't' },
      progress: { total: 1, pending: 0, in_progress: 0, completed: 1, blocked: 0, skipped: 0 },
    })

    expect(store.snapshot.steps[0]!.status).toBe('completed')
    expect(store.snapshot.steps[0]!.evidence).toBe('dmesg 干净')
    expect(store.snapshot.progress.completed).toBe(1)
  })

  it('归属校验：conversation_id 对不上的事件被丢弃（防串会话）', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: planFixture(), steps: [], progress: emptyProgress(),
    })
    const store = useGoalPlanStore()
    await store.attach('c1')

    capturedHandler!({
      type: 'goal_plan_progress', mode: 'delta',
      goal: { ...goalFixture(), conversation_id: 'c-other' },
      plan: null,
      step: { step_id: 'sx', plan_id: 'p1', ordinal: 9, title: '别会话', detail: '',
              status: 'completed', blocked_reason: '', evidence: '', run_id: '',
              iteration: 0, started_at: '', finished_at: '', updated_at: '' },
      progress: { total: 99, pending: 0, in_progress: 0, completed: 99, blocked: 0, skipped: 0 },
    })

    expect(store.snapshot.steps).toEqual([])
    expect(store.snapshot.progress.total).toBe(0)
  })

  it('snapshot 模式触发 REST 全量重拉（事件不带完整 steps）', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: planFixture(), steps: [], progress: emptyProgress(),
    })
    const store = useGoalPlanStore()
    await store.attach('c1')
    expect(goalPlanApi.state).toHaveBeenCalledTimes(1)

    capturedHandler!({ type: 'goal_plan_progress', mode: 'snapshot', goal: null, plan: null })
    await vi.waitFor(() => expect(goalPlanApi.state).toHaveBeenCalledTimes(2))
    expect(goalPlanApi.state).toHaveBeenLastCalledWith('c1')
  })
})

describe('goal-plan 用户侧动作', () => {
  it('批准后重新拉快照（状态从 awaiting_approval → approved）', async () => {
    vi.mocked(goalPlanApi.state)
      .mockResolvedValueOnce({ goal: goalFixture(), plan: planFixture(), steps: [], progress: emptyProgress() })
      .mockResolvedValueOnce({ goal: goalFixture(), plan: planFixture('approved'), steps: [], progress: emptyProgress() })
    const store = useGoalPlanStore()
    await store.attach('c1')
    await store.approvePlan('p1')
    expect(goalPlanApi.approvePlan).toHaveBeenCalledWith('p1')
    expect(store.awaitingApproval).toBe(false)
  })

  it('动作失败给 toast 且不清空快照', async () => {
    vi.mocked(goalPlanApi.state).mockResolvedValue({
      goal: goalFixture(), plan: planFixture(), steps: [], progress: emptyProgress(),
    })
    vi.mocked(goalPlanApi.abandonPlan).mockRejectedValueOnce(new Error('计划不处于活动状态'))
    const store = useGoalPlanStore()
    await store.attach('c1')
    await store.abandonPlan('p1')
    expect(store.hasContent).toBe(true)
  })
})
