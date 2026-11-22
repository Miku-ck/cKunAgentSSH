/**
 * goal-plan-core 回归 —— 状态机转移、进度计算与 SSE delta 归并。
 *
 * 取值与 `backend/agent/harness/goal_plan_fsm.py` 手工同源：本文件里的
 * 合法/非法转移断言与后端 `test_goal_plan_state_machine.py` 成对，两侧
 * 必须一致（一侧放宽另一侧不放宽 = 跨端漂移）。
 */
import { describe, expect, it } from 'vitest'

import {
  allCriteriaMet,
  applyDelta,
  canTransition,
  computeProgress,
  emptySnapshot,
  type StepInfo,
} from './goal-plan-core'

function makeStep(overrides: Partial<StepInfo>): StepInfo {
  return {
    step_id: 's1', plan_id: 'p1', ordinal: 1, title: 't', detail: '',
    status: 'pending', blocked_reason: '', evidence: '', run_id: '',
    iteration: 0, started_at: '', finished_at: '', updated_at: '',
    ...overrides,
  }
}

describe('canTransition（与后端 FSM 同值）', () => {
  it('合法转移通过', () => {
    expect(canTransition('pending', 'in_progress').ok).toBe(true)
    expect(canTransition('pending', 'skipped').ok).toBe(true)
    expect(canTransition('in_progress', 'completed', '日志输出 done').ok).toBe(true)
    expect(canTransition('in_progress', 'blocked', '', '权限被拒').ok).toBe(true)
    expect(canTransition('blocked', 'in_progress').ok).toBe(true)
  })

  it('矩阵外转移与未知状态拒绝（fail-closed）', () => {
    expect(canTransition('completed', 'in_progress').ok).toBe(false)
    expect(canTransition('skipped', 'pending').ok).toBe(false)
    expect(canTransition('pending', 'completed', 'x').ok).toBe(false)
    expect(canTransition('blocked', 'skipped').ok).toBe(false)
    expect(canTransition('nope' as never, 'pending').ok).toBe(false)
    expect(canTransition('pending', 'nope' as never).ok).toBe(false)
  })

  it('completed 必须带 evidence、blocked 必须带 blocked_reason', () => {
    const noEvidence = canTransition('in_progress', 'completed')
    expect(noEvidence.ok).toBe(false)
    expect(noEvidence.reason).toContain('evidence')
    const noReason = canTransition('in_progress', 'blocked')
    expect(noReason.ok).toBe(false)
    expect(noReason.reason).toContain('blocked_reason')
    expect(canTransition('in_progress', 'completed', '   ').ok).toBe(false)
  })
})

describe('computeProgress', () => {
  it('按状态分桶计数，未知状态不计入分桶但计入 total', () => {
    const steps = [
      makeStep({ step_id: 'a', status: 'completed' }),
      makeStep({ step_id: 'b', status: 'in_progress' }),
      makeStep({ step_id: 'c', status: 'blocked' }),
      makeStep({ step_id: 'd', status: 'pending' }),
      makeStep({ step_id: 'e', status: 'weird' as never }),
    ]
    expect(computeProgress(steps)).toEqual({
      total: 5, pending: 1, in_progress: 1, completed: 1, blocked: 1, skipped: 0,
    })
  })

  it('空列表返回全零', () => {
    expect(computeProgress([]).total).toBe(0)
  })
})

describe('allCriteriaMet', () => {
  it('空 criteria 不算达成', () => {
    expect(allCriteriaMet([])).toBe(false)
  })
  it('全部 met 才为 true', () => {
    expect(allCriteriaMet([
      { id: 'c1', text: 'a', met: true, evidence: 'e1' },
      { id: 'c2', text: 'b', met: true, evidence: 'e2' },
    ])).toBe(true)
    expect(allCriteriaMet([
      { id: 'c1', text: 'a', met: true, evidence: '' },
      { id: 'c2', text: 'b', met: false, evidence: '' },
    ])).toBe(false)
  })
})

describe('applyDelta', () => {
  const base = emptySnapshot()

  it('goal/plan/progress 字段存在即替换，steps 由 REST 全量提供', () => {
    const snapshot = {
      ...base,
      steps: [makeStep({ step_id: 'a', status: 'completed' })],
    }
    const merged = applyDelta(snapshot, {
      mode: 'snapshot',
      goal: null,
      plan: null,
      progress: { total: 1, pending: 0, in_progress: 0, completed: 1, blocked: 0, skipped: 0 },
    })
    expect(merged.steps).toHaveLength(1) // steps 不被事件清空
    expect(merged.progress.completed).toBe(1)
    expect(merged.goal).toBeNull()
    expect(snapshot.steps).toHaveLength(1) // 入参不被修改
  })

  it('delta 模式按 step_id 原位替换并重算 progress，不修改入参', () => {
    const snapshot = {
      ...base,
      steps: [makeStep({ step_id: 'a' }), makeStep({ step_id: 'b' })],
    }
    const updated = applyDelta(snapshot, {
      mode: 'delta',
      step: makeStep({ step_id: 'b', status: 'completed', evidence: 'ok' }),
    })
    expect(updated.steps.find(s => s.step_id === 'b')?.status).toBe('completed')
    expect(updated.progress.completed).toBe(1)
    expect(snapshot.steps.find(s => s.step_id === 'b')?.status).toBe('pending')
  })

  it('delta 未知 step 追加（UI 不丢新步骤）', () => {
    const snapshot = { ...base, steps: [makeStep({ step_id: 'a' })] }
    const merged = applyDelta(snapshot, {
      mode: 'delta',
      step: makeStep({ step_id: 'new', ordinal: 2 }),
    })
    expect(merged.steps).toHaveLength(2)
    expect(merged.steps[1].step_id).toBe('new')
    expect(merged.progress.total).toBe(2)
  })
})
