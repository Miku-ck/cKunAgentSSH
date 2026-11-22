/**
 * 会话级 goal/plan 结构化状态 store（M2）。
 *
 * 与 stores/ssh-ai.ts（会话与消息）、stores/agent.ts（单次 run 执行态）的分工：
 * 本 store 只管「这个会话要达成什么、计划走到哪一步」—— 数据面唯一真值在
 * 后端 SQLite（db/goal_plan_store），前端是投影：
 * - 快照：REST GET /goal-plan/state 全量（attach 切会话 / snapshot 事件信号）；
 * - 增量：SSE `goal_plan_progress` 事件经 sse.ts 订阅总线旁路进来
 *   （agent.ts 零改动），字段级归并复用 utils/goal-plan-core.applyDelta；
 * - 裁决动作（批准/重置/放弃）走 REST 用户侧入口，成功后重新拉全量。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { goalPlanApi, subscribeGoalPlanProgress, type AgentSSEEvent } from '@/api/client'
import { applyDelta, emptySnapshot, type GoalAdjudication, type GoalPlanSnapshot } from '@/utils/goal-plan-core'
import { logWarn } from '@/utils/logger'
import { useToastStore } from './toast'

export const useGoalPlanStore = defineStore('goal-plan', () => {
  const snapshot = ref<GoalPlanSnapshot>(emptySnapshot())
  /** 当前快照归属的后端会话 id（run 用的同一把 id，非本地会话 id） */
  const conversationId = ref('')
  const busy = ref(false)
  /** 过期响应守卫：attach 连续切换时，旧请求的晚到结果不得覆盖新快照 */
  let loadSeq = 0

  const hasContent = computed(() => snapshot.value.goal != null || snapshot.value.plan != null)
  const awaitingApproval = computed(() => snapshot.value.plan?.status === 'awaiting_approval')
  /** 批准 / 重置 / 放弃只对活动 plan 有意义（其余状态动作入口不露出） */
  const planActionable = computed(() =>
    snapshot.value.plan != null
    && ['awaiting_approval', 'approved', 'active'].includes(snapshot.value.plan.status))

  function clear(): void {
    loadSeq += 1
    conversationId.value = ''
    snapshot.value = emptySnapshot()
  }

  /** 拉取会话全量快照（无 cid = 清空；网络失败保留现有投影不清空）。 */
  async function loadSnapshot(cid: string): Promise<void> {
    if (!cid) { clear(); return }
    const seq = ++loadSeq
    try {
      const snap = await goalPlanApi.state(cid)
      if (seq !== loadSeq) return
      conversationId.value = cid
      snapshot.value = snap
    } catch (e) {
      // 拉取失败保留现有投影不清空：渲染不该被网络抖动打断（下次事件再对齐）
      logWarn('goal-plan', '快照拉取失败，保留现有投影:', e)
    }
  }

  /** 切会话 / 面板挂载入口：会话变化才拉（同会话重复 attach 不打 REST）。 */
  async function attach(cid: string): Promise<void> {
    if (!cid) { clear(); return }
    if (cid === conversationId.value) return
    await loadSnapshot(cid)
  }

  function handleProgressEvent(event: AgentSSEEvent): void {
    if (event.type !== 'goal_plan_progress') return
    // 归属校验：事件携带的 conversation_id 与当前投影对不上 → 串会话事件，丢弃
    const evCid = event.goal?.conversation_id || event.plan?.conversation_id || ''
    if (evCid && conversationId.value && evCid !== conversationId.value) return
    if (event.mode === 'snapshot') {
      // snapshot 模式是「重新拉 REST 全量」的信号（事件刻意不带完整 steps）
      void loadSnapshot(conversationId.value)
      return
    }
    snapshot.value = applyDelta(snapshot.value, {
      mode: 'delta',
      goal: event.goal,
      plan: event.plan,
      step: event.step ?? null,
      progress: event.progress,
    })
  }

  // store setup 只执行一次：进程级订阅（handler 内部已做异常与归属隔离）
  subscribeGoalPlanProgress(handleProgressEvent)

  async function _runAction(label: string, fn: () => Promise<unknown>): Promise<void> {
    if (busy.value) return
    busy.value = true
    try {
      await fn()
      await loadSnapshot(conversationId.value)
    } catch (e) {
      const toast = useToastStore()
      const detail = e instanceof Error ? e.message : String(e)
      toast.error(`${label}失败：${detail}`)
    } finally {
      busy.value = false
    }
  }

  function approvePlan(planId: string): Promise<void> {
    return _runAction('批准计划', () => goalPlanApi.approvePlan(planId))
  }

  function replanPlan(planId: string): Promise<void> {
    return _runAction('重置计划', () => goalPlanApi.replanPlan(planId))
  }

  function abandonPlan(planId: string): Promise<void> {
    return _runAction('放弃计划', () => goalPlanApi.abandonPlan(planId))
  }

  /** 用户裁决目标：achieved 仅 adjudication='user_confirmed' 下可达（后端硬校验）。
   *  与 criteria 逐条自检是两条独立判定 —— UI 不得拿 criteria.met 硬阻断裁决。 */
  function adjudicateGoal(goalId: string, payload: {
    adjudication: GoalAdjudication
    achieved: boolean
  }): Promise<void> {
    return _runAction('裁决目标', () => goalPlanApi.adjudicateGoal(goalId, payload))
  }

  /** 修订目标标准（乐观锁）：criteria 传字符串列表，后端重建后勾选态归零。 */
  function reviseGoal(goalId: string, payload: {
    expected_version: number
    title?: string
    statement?: string
    context?: string
    criteria?: string[]
  }): Promise<void> {
    return _runAction('修订目标', () => goalPlanApi.reviseGoal(goalId, payload))
  }

  return {
    snapshot,
    conversationId,
    busy,
    hasContent,
    awaitingApproval,
    planActionable,
    clear,
    attach,
    loadSnapshot,
    handleProgressEvent,
    approvePlan,
    replanPlan,
    abandonPlan,
    adjudicateGoal,
    reviseGoal,
  }
})
