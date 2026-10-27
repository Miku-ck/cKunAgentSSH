/**
 * SchedulesView 历史接线守卫 —— spec 2026-10-27-schedule-history-render
 * 钉：列表主体已换成 HistoryList（旧内联 markup 移除）、失败态不再伪装空态、
 * 诊断按钮经 #actions slot 且反查原始 run 对象。
 */
import { describe, expect, it } from 'vitest'
import viewRaw from './SchedulesView.vue?raw'

const src = viewRaw.replace(/\r\n/g, '\n')
const style = src.slice(src.indexOf('<style scoped>'))

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

describe('弹窗承载', () => {
  it('历史/编辑弹窗都走 BaseModal（Teleport + aria + 焦点陷阱）', () => {
    expect(src.match(/<BaseModal/g)).toHaveLength(2)
    expect(src).toContain('historyFor = null"')
    expect(src).toContain('editing = null"')
  })
  it('旧自绘遮罩样式已移除（字面 rgba 遮罩 / 10px 圆角）', () => {
    expect(src).not.toContain('.modal-mask')
    expect(src).not.toContain('rgba(0,0,0,0.6)')
    expect(src).not.toContain('border-radius: 10px')
  })
  it('Esc 交给 BaseModal，视图不再重复监听', () => {
    expect(src).not.toContain('handleKeydown')
  })
})

describe('圆角令牌', () => {
  it('样式块内不用字面 px 圆角（手记主题会把 8px 改小，字面值破相）', () => {
    expect(style).not.toMatch(/border-radius:\s*\d+px/)
  })
})

describe('清空历史', () => {
  it('弹窗底部有清空历史按钮，进行中禁用', () => {
    expect(src).toContain('clearHistory()')
    expect(src).toContain(':disabled="historyHasRunning"')
  })
  it('清空走危险确认 + clearRuns + 成功后刷新', () => {
    expect(src).toContain('scheduleApi.clearRuns')
    expect(src).toContain('danger: true')
    // 清空后靠 fetchHistory 回到空态
    expect(src).toMatch(/clearRuns[\s\S]*?await fetchHistory\(\)/)
  })
})
