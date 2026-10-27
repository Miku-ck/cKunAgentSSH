/**
 * history-view 纯函数守卫 —— spec 2026-10-27-schedule-history-render
 * 折叠态预览句 / 耗时 / 状态标签 / 行适配器的行为契约。node 环境直测，
 * 不 import markdown.ts（顶层 DOMPurify.addHook 在 node 下抛错，mdPreviewLine
 * 因此落在本文件——这是位置决策，移走必须连测试一起搬）。
 */
import { describe, expect, it } from 'vitest'
import {
  mdPreviewLine, statusLabel, durationText, shortId,
  toScheduleRow, toRunbookRow,
  STATUS_LABEL_SCHEDULE, STATUS_LABEL_RUNBOOK,
} from '@/utils/history-view'
import type { ScheduleRun } from '@/api/agent'
import type { RunbookRunRecord } from '@/api/runbook'

describe('mdPreviewLine', () => {
  it('剥加粗/行内代码/链接语法留文字', () => {
    expect(mdPreviewLine('**主机 10.0.0.1** 内存告警 `90%+`，见 [报告](http://x)'))
      .toBe('主机 10.0.0.1 内存告警 90%+，见 报告')
  })
  it('去标题井号与列表符', () => {
    expect(mdPreviewLine('## 发现的问题')).toBe('发现的问题')
    expect(mdPreviewLine('- **磁盘** 87%')).toBe('磁盘 87%')
    expect(mdPreviewLine('1. 主机告警')).toBe('主机告警')
  })
  it('多行取第一行有效文本，跳过空行与分隔线', () => {
    expect(mdPreviewLine('---\n\n首行摘要\n次行')).toBe('首行摘要')
  })
  it('整段代码围栏无正文 → 空串', () => {
    expect(mdPreviewLine('```\nls\n```')).toBe('')
  })
  it('空文本/纯空白 → 空串', () => {
    expect(mdPreviewLine('')).toBe('')
    expect(mdPreviewLine('   \n  ')).toBe('')
  })
})

describe('statusLabel', () => {
  it('两域 label 表各自映射；未知值原样透出', () => {
    expect(statusLabel('completed', STATUS_LABEL_SCHEDULE)).toBe('已完成')
    expect(statusLabel('success', STATUS_LABEL_RUNBOOK)).toBe('成功')
    expect(statusLabel('weird', STATUS_LABEL_SCHEDULE)).toBe('weird')
  })
  it('空值兜底可配：卡片位「未执行」，历史行位空串', () => {
    expect(statusLabel(null, STATUS_LABEL_RUNBOOK, '未执行')).toBe('未执行')
    expect(statusLabel('', STATUS_LABEL_SCHEDULE)).toBe('')
  })
})

describe('durationText', () => {
  it('正常差值保留 1 位小数', () => {
    expect(durationText('2026-10-27 07:38:00', '2026-10-27 07:39:16')).toBe('76.0s')
  })
  it('缺失/倒挂/非法 → 空串', () => {
    expect(durationText('2026-10-27 07:38:00', undefined)).toBe('')
    expect(durationText('2026-10-27 07:39:00', '2026-10-27 07:38:00')).toBe('')
    expect(durationText('garbage', '2026-10-27 07:38:00')).toBe('')
  })
})

describe('行适配器', () => {
  const baseRun: ScheduleRun = {
    id: 'r1', schedule_id: 's1', run_id: 'a33632b5ffff', status: 'completed',
    summary: '**主机 A** 内存告警\n\n细节', error: '',
    started_at: '2026-10-27 07:38:00', finished_at: '2026-10-27 07:39:16', created_at: '',
  }
  it('定时任务行：preview=summary 首行剥制；summary 空回退 error', () => {
    expect(toScheduleRow(baseRun).preview).toBe('主机 A 内存告警')
    expect(toScheduleRow({ ...baseRun, summary: '', error: 'SSH 超时' }).preview).toBe('SSH 超时')
  })
  it('剧本行：preview=步骤命令优先回退 error；expandable 由步骤/诊断决定', () => {
    const rb = {
      id: 'x1', runbook_id: 'b1', run_id: '', status: 'success', current_step: 1,
      steps_state: [{ seq: 1, status: 'success', command: 'df -h' }],
      diagnosis: '', error: '', started_at: '', created_at: '',
    } as unknown as RunbookRunRecord
    expect(toRunbookRow(rb).preview).toBe('df -h')
    expect(toRunbookRow(rb).expandable).toBe(true)
    expect(toRunbookRow({ ...rb, steps_state: [], diagnosis: '' }).expandable).toBe(false)
    // 成功剧本行即便带 error 残留，预览仍是步骤命令（spec §2 决议）
    expect(toRunbookRow({ ...rb, error: '旧错' }).preview).toBe('df -h')
  })
  it('shortId 截 8 位、空值安全', () => {
    expect(shortId('a33632b5ffff')).toBe('a33632b5')
    expect(shortId('')).toBe('')
    expect(shortId(undefined)).toBe('')
  })
})
