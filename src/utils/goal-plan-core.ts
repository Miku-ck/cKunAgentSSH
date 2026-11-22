/**
 * goal / plan 前端纯逻辑 — 状态集合、转移矩阵、进度计算与 SSE delta 归并。
 *
 * 与后端 `backend/agent/harness/goal_plan_fsm.py`、`backend/db/goal_plan_store.py`
 * 的 DDL 注释**手工同源**：状态取值与合法转移两侧必须一致，改动必须同步。
 * 归并函数只做纯计算（无网络、无 store 依赖），供 `src/stores/goal-plan.ts`
 * 在订阅总线上消费 `goal_plan_progress` 事件使用。
 */

export type GoalStatus = 'draft' | 'active' | 'achieved' | 'revised' | 'abandoned'
export type GoalAdjudication = 'pending' | 'user_confirmed' | 'auto_suggested'
export type PlanStatus =
  | 'draft' | 'awaiting_approval' | 'approved' | 'active'
  | 'superseded' | 'completed' | 'abandoned'
export type StepStatus = 'pending' | 'in_progress' | 'completed' | 'blocked' | 'skipped'

/** 与后端 ACTIVE_PLAN_STATUSES 同值：同会话至多一个活动 plan。 */
export const ACTIVE_PLAN_STATUSES: readonly PlanStatus[] = [
  'awaiting_approval', 'approved', 'active',
]

/** 与后端 LEGAL_STEP_TRANSITIONS 同值：键为当前状态，值为允许的目标状态。 */
export const LEGAL_STEP_TRANSITIONS: Readonly<Record<StepStatus, readonly StepStatus[]>> = {
  pending: ['in_progress', 'skipped'],
  in_progress: ['completed', 'blocked'],
  blocked: ['in_progress'],
  completed: [],
  skipped: [],
}

export interface GoalCriterion {
  id: string
  text: string
  met: boolean
  evidence: string
}

export interface GoalInfo {
  goal_id: string
  conversation_id: string
  title: string
  statement: string
  context: string
  criteria: GoalCriterion[]
  status: GoalStatus
  version: number
  adjudication: GoalAdjudication
  adjudicated_by: string
  created_at: string
  updated_at: string
}

export interface PlanInfo {
  plan_id: string
  goal_id: string
  conversation_id: string
  version: number
  status: PlanStatus
  approved_by: string
  approved_at: string
  supersedes_plan_id: string
  created_at: string
  updated_at: string
}

export interface StepInfo {
  step_id: string
  plan_id: string
  ordinal: number
  title: string
  detail: string
  status: StepStatus
  blocked_reason: string
  evidence: string
  run_id: string
  iteration: number
  started_at: string
  finished_at: string
  updated_at: string
}

export interface GoalPlanProgress {
  total: number
  pending: number
  in_progress: number
  completed: number
  blocked: number
  skipped: number
}

export interface GoalPlanSnapshot {
  goal: GoalInfo | null
  plan: PlanInfo | null
  steps: StepInfo[]
  progress: GoalPlanProgress
}

/** SSE `goal_plan_progress` 事件 payload（mode=snapshot 整体替换 / mode=delta 局部更新）。 */
export interface GoalPlanDelta {
  mode: 'snapshot' | 'delta'
  goal?: GoalInfo | null
  plan?: PlanInfo | null
  step?: StepInfo | null
  /** 仅 snapshot 分支携带：服务端 get_snapshot 的完整步骤列表。 */
  steps?: StepInfo[]
  progress?: GoalPlanProgress
}

export function emptySnapshot(): GoalPlanSnapshot {
  return {
    goal: null,
    plan: null,
    steps: [],
    progress: { total: 0, pending: 0, in_progress: 0, completed: 0, blocked: 0, skipped: 0 },
  }
}

/** 判定一次 step 跃迁是否合法（与后端 can_transition 同语义，fail-closed）。 */
export function canTransition(
  current: StepStatus,
  target: StepStatus,
  evidence = '',
  blockedReason = '',
): { ok: boolean; reason: string } {
  if (!(current in LEGAL_STEP_TRANSITIONS)) {
    return { ok: false, reason: `未知当前状态 ${current}` }
  }
  if (!LEGAL_STEP_TRANSITIONS[current].includes(target)) {
    return { ok: false, reason: `不允许 ${current} → ${target} 的状态转移` }
  }
  if (target === 'completed' && !evidence.trim()) {
    return { ok: false, reason: '转移到 completed 必须携带 evidence（完成证据）' }
  }
  if (target === 'blocked' && !blockedReason.trim()) {
    return { ok: false, reason: '转移到 blocked 必须携带 blocked_reason（受阻原因）' }
  }
  return { ok: true, reason: '' }
}

/** 按状态分桶统计步骤进度（未知状态不计入任何分桶，total 仍取条数）。 */
export function computeProgress(steps: StepInfo[]): GoalPlanProgress {
  const counts: GoalPlanProgress = {
    total: steps.length, pending: 0, in_progress: 0, completed: 0, blocked: 0, skipped: 0,
  }
  for (const step of steps) {
    if (step && step.status in counts) {
      counts[step.status] += 1
    }
  }
  return counts
}

/** 全部条目 met 才算达成（空 criteria 不算达成）。 */
export function allCriteriaMet(criteria: GoalCriterion[]): boolean {
  if (criteria.length === 0) {
    return false
  }
  return criteria.every(item => item.met)
}

/**
 * 把一条 `goal_plan_progress` 事件归并进当前快照：
 * - `mode=snapshot`：整体替换（run 启动时对齐服务端真值）；
 * - `mode=delta`：局部更新 goal / plan / step / progress 任一存在的字段，
 *   step 按 step_id 定位替换，找不到时追加（保证 UI 不丢新步骤）。
 * 返回新对象，不修改入参快照。
 */
export function mergeSnapshotDelta(
  snapshot: GoalPlanSnapshot,
  delta: GoalPlanDelta,
): GoalPlanSnapshot {
  if (delta.mode === 'snapshot') {
    const steps = delta.steps ?? []
    return {
      goal: delta.goal ?? null,
      plan: delta.plan ?? null,
      steps,
      progress: delta.progress ?? computeProgress(steps),
    }
  }
  const next: GoalPlanSnapshot = {
    goal: delta.goal !== undefined ? delta.goal : snapshot.goal,
    plan: delta.plan !== undefined ? delta.plan : snapshot.plan,
    steps: snapshot.steps,
    progress: delta.progress !== undefined ? delta.progress : snapshot.progress,
  }
  if (delta.step) {
    const incoming = delta.step
    const index = next.steps.findIndex(item => item.step_id === incoming.step_id)
    next.steps = index >= 0
      ? next.steps.map((item, i) => (i === index ? incoming : item))
      : [...next.steps, incoming]
    next.progress = computeProgress(next.steps)
  }
  return next
}
