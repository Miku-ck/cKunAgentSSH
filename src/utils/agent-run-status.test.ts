/**
 * S9-13 回归：run 终态标签的单一事实源。
 *
 * 症状两处：
 *   1. `AgentTraceSnapshot.status` 只声明两值，`agentStore.snapshot()` 被迫把
 *      human_help / max_reached 折成 failed（注释自述「以兼容类型」）；
 *   2. `AgentFullTrace.vue` 用 `=== 'completed' ? '完成' : '失败'` 渲染后端原始
 *      终态 ⇒ 「迭代耗尽」和「请求人工介入」都被念成「失败」。
 * 这三种结局用户下一步该做的事完全不同（补预算 / 接手回答 / 查报错），
 * 合并成一句话等于把判断权从界面偷走。
 */
import { describe, expect, it } from 'vitest'
import {
  RUN_STATE_LABELS,
  RUN_TERMINAL_STATES,
  isRunTerminalState,
  runStatusChip,
  runStatusLabel,
} from './agent-run-status'

describe('runStatusLabel', () => {
  it('四种终态各有各的文案，不合并成「失败」', () => {
    expect(runStatusLabel('completed')).toBe('完成')
    expect(runStatusLabel('failed')).toBe('失败')
    expect(runStatusLabel('human_help')).toBe('请求人工介入')
    expect(runStatusLabel('max_reached')).toBe('达到迭代上限')
    expect(new Set(RUN_TERMINAL_STATES.map(runStatusLabel)).size).toBe(4)
  })

  it('每个终态都有标签（枚举与文案表不会各改各的）', () => {
    for (const s of RUN_TERMINAL_STATES) {
      expect(RUN_STATE_LABELS[s]).toBeTruthy()
    }
  })

  it('在途状态也在同一张表里 —— 筛选下拉靠它当枚举源', () => {
    expect(RUN_STATE_LABELS.init).toBe('初始化')
    expect(RUN_STATE_LABELS.running).toBe('运行中')
  })

  it('认不出的串原样显示，空值说「状态未知」，都不降级成「失败」', () => {
    expect(runStatusLabel('paused_by_kernel')).toBe('paused_by_kernel')
    expect(runStatusLabel('')).toBe('状态未知')
    expect(runStatusLabel(undefined)).toBe('状态未知')
  })
})

describe('runStatusChip / isRunTerminalState', () => {
  it('只有 completed 绿、只有 failed 红，其余中性', () => {
    expect(runStatusChip('completed')).toBe('ok')
    expect(runStatusChip('failed')).toBe('err')
    expect(runStatusChip('human_help')).toBe('muted')
    expect(runStatusChip('max_reached')).toBe('muted')
    expect(runStatusChip('running')).toBe('muted')
    expect(runStatusChip('whatever')).toBe('muted')
  })

  it('在途值不算终态', () => {
    expect(isRunTerminalState('completed')).toBe(true)
    expect(isRunTerminalState('max_reached')).toBe(true)
    expect(isRunTerminalState('running')).toBe(false)
    expect(isRunTerminalState('')).toBe(false)
    expect(isRunTerminalState(undefined)).toBe(false)
  })
})
