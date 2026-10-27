/**
 * SchedulesView 历史接线守卫 —— spec 2026-10-27-schedule-history-render
 * 钉：列表主体已换成 HistoryList（旧内联 markup 移除）、失败态不再伪装空态、
 * 诊断按钮经 #actions slot 且反查原始 run 对象。
 */
import { describe, expect, it } from 'vitest'
import viewRaw from './SchedulesView.vue?raw'

const src = viewRaw.replace(/\r\n/g, '\n')

describe('历史接线', () => {
  it('列表主体经 HistoryList 渲染，:key 重挂载承担展开态重置', () => {
    expect(src).toContain('<HistoryList')
    expect(src).toContain(':key="historyFor?.id"')
    expect(src).toContain(':rows="historyRows"')
  })
  it('旧内联历史列表已移除', () => {
    expect(src).not.toContain('class="history-list"')
    expect(src).not.toContain('class="history-item"')
  })
  it('请求失败渲染错误条 + 重试，不再伪装空态', () => {
    expect(src).toContain('historyError')
    expect(src).toContain('fetchHistory()')
  })
  it('AI 诊断按钮经 #actions slot，反查原始 run 对象', () => {
    expect(src).toContain('#actions="{ row }"')
    expect(src).toContain('askAiDiagnose(historyFor, rawRunOf(row.id))')
  })
  it('行数据经 toScheduleRow 适配', () => {
    expect(src).toContain('toScheduleRow')
  })
})
