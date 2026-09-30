/**
 * 运行视图的轨迹派生层（spec 2026-09-29-home-command-panel · 接缝 S1a）
 *
 * 只做一件事：把 agentStore.toolCalls 这条扁平流水，切成「左栏按批、右栏按序」
 * 两个视角。两个消费者必须读同一份输出 —— 分组口径各写一遍，就是时间线第 3 步
 * 与事件流第 3 张卡指向不同调用那种经典错位。
 *
 * 三条刻意在案的口径：
 *   1. 「未开始」这一步态**没有数据源**。它属于"计划条目"，而前端没有任何结构化
 *      步骤列表（§8）。所以状态集只有 完成/失败/在途 三态，不硬凑第四态。
 *   2. 时长 undefined（一条都没量到）与 0（量到 0）是两件事，不能折成 0。
 *   3. 按 iteration 归批、但 events 保持到达序 —— 后端重发同一 iteration 时，
 *      事件流不能因此把先到的结果挪到后面。
 */
import { describe, expect, it } from 'vitest'
import { buildTraceModel, type TraceInputEntry } from '@/utils/run-trace'

function entry(over: Partial<TraceInputEntry> & { tool: string }): TraceInputEntry {
  return { id: `e_${over.tool}_${Math.random()}`, timestamp: 0, ...over }
}

describe('buildTraceModel 分批', () => {
  it('空流水出空模型，而不是 undefined', () => {
    const m = buildTraceModel([])
    expect(m.events).toEqual([])
    expect(m.steps).toEqual([])
    expect(m.activeStepIndex).toBe(-1)
  })

  it('按 iteration 归批，events 仍是到达序', () => {
    const m = buildTraceModel([
      entry({ tool: 'shell_exec', iteration: 0 }),
      entry({ tool: 'file_read', iteration: 1 }),
      entry({ tool: 'shell_exec', iteration: 0 }),
    ])
    expect(m.steps.map(s => s.iteration)).toEqual([0, 1])
    // 第 3 条属于第 1 批，但在事件流里仍排最后
    expect(m.events.map(e => e.tool)).toEqual(['shell_exec', 'file_read', 'shell_exec'])
    expect(m.events[2].stepIndex).toBe(0)
    expect(m.steps[0].eventIndexes).toEqual([0, 2])
  })

  it('缺 iteration 的条目自成一类（不并进第 0 批、也不丢）', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0 }),
      entry({ tool: 'b' }),
    ])
    expect(m.steps.length).toBe(2)
    expect(m.steps[1].iteration).toBeNull()
    expect(m.events.length).toBe(2)
  })

  it('事件流里的 stepIndex 与步里的 eventIndexes 双向对得上', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0 }),
      entry({ tool: 'b', iteration: 1 }),
      entry({ tool: 'c', iteration: 1 }),
    ])
    for (const [si, step] of m.steps.entries()) {
      for (const ei of step.eventIndexes) expect(m.events[ei].stepIndex).toBe(si)
    }
  })
})

describe('步状态', () => {
  it('批内有未回填的调用 = 在途', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0, result: 'ok', success: true }),
      entry({ tool: 'b', iteration: 0 }),
    ])
    expect(m.steps[0].state).toBe('running')
    expect(m.activeStepIndex).toBe(0)
  })

  it('空串结果是已回填（无输出命令），不算在途', () => {
    const m = buildTraceModel([entry({ tool: 'touch', iteration: 0, result: '', success: true })])
    expect(m.steps[0].state).toBe('done')
    expect(m.activeStepIndex).toBe(-1)
  })

  it('任一调用失败即该批失败，即使同批别的都成功', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0, result: 'ok', success: true }),
      entry({ tool: 'b', iteration: 0, result: 'boom', success: false }),
    ])
    expect(m.steps[0].state).toBe('failed')
  })

  it('多批都在跑时 activeStepIndex 指向最新那批', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0 }),
      entry({ tool: 'b', iteration: 1 }),
    ])
    expect(m.steps.map(s => s.state)).toEqual(['running', 'running'])
    expect(m.activeStepIndex).toBe(1)
  })

  it('缺 success 的已回填条目算完成，不算失败', () => {
    const m = buildTraceModel([entry({ tool: 'a', iteration: 0, result: 'x' })])
    expect(m.steps[0].state).toBe('done')
  })
})

describe('步时长', () => {
  it('一条都没量到时是 undefined，不是 0', () => {
    const m = buildTraceModel([entry({ tool: 'a', iteration: 0, result: '', success: true })])
    expect(m.steps[0].durationMs).toBeUndefined()
    expect(m.steps[0].measuredCount).toBe(0)
  })

  it('量到的相加；没量到的那条不计入分母', () => {
    const m = buildTraceModel([
      entry({ tool: 'a', iteration: 0, result: '', success: true, durationMs: 120 }),
      entry({ tool: 'b', iteration: 0, result: '', success: true, durationMs: 80 }),
      entry({ tool: 'c', iteration: 0, result: '', success: true }),
    ])
    expect(m.steps[0].durationMs).toBe(200)
    // measuredCount < eventIndexes.length → 视图据此标「部分计时」，不装作是全批
    expect(m.steps[0].measuredCount).toBe(2)
    expect(m.steps[0].eventIndexes.length).toBe(3)
  })

  it('量到 0 如实参与求和，且 measuredCount 记 1', () => {
    const m = buildTraceModel([entry({ tool: 'a', iteration: 0, result: '', success: true, durationMs: 0 })])
    expect(m.steps[0].durationMs).toBe(0)
    expect(m.steps[0].measuredCount).toBe(1)
  })
})
