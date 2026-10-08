/**
 * S7-08 回归：定时任务桌面通知的选取判据。
 *
 * 原判据只看「last_status 字符串变了」，于是每天成功的任务只在第一次成功时通知一次、
 * 此后 completed→completed 全静默；每天失败的同理 —— 而「定时任务失败」恰恰是这条
 * 通知存在的理由。改按执行身份（last_run_id）去重后，这两件事必须同时成立：
 * 新的一次执行必发，同一次执行的反复轮询不重播。
 */
import { describe, expect, it } from 'vitest'

import type { ScheduleRecord } from '@/api/client'
import { pickNotices } from './schedule-notify'

function row(over: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    id: 'sch-1',
    name: '每日巡检',
    enabled: 1,
    last_status: 'completed',
    last_run_id: 'r-1',
    ...over,
  } as ScheduleRecord
}

describe('pickNotices', () => {
  it('每天同一状态的新一次执行照样要通知（旧判据在这里静默）', () => {
    const prev = [row({ last_status: 'completed', last_run_id: 'r-1' })]
    const next = [row({ last_status: 'completed', last_run_id: 'r-2' })]
    const notices = pickNotices(prev, next)
    expect(notices).toHaveLength(1)
    expect(notices[0].title).toBe('定时任务 · 每日巡检')
    expect(notices[0].body).toBe('执行完毕')
  })

  it('连续失败也每天提醒一次', () => {
    const prev = [row({ last_status: 'failed', last_run_id: 'r-1' })]
    const next = [row({ last_status: 'failed', last_run_id: 'r-2' })]
    expect(pickNotices(prev, next)).toHaveLength(1)
  })

  it('同一次执行被反复轮询只播一次', () => {
    const prev = [row({ last_status: 'failed', last_run_id: 'r-1' })]
    const next = [row({ last_status: 'failed', last_run_id: 'r-1' })]
    expect(pickNotices(prev, next)).toHaveLength(0)
  })

  it('首次加载只建基线，不重播存量历史', () => {
    expect(pickNotices([], [row()])).toHaveLength(0)
  })

  it('过程态与停用中的任务不发', () => {
    const prev = [row({ last_status: 'running', last_run_id: 'r-1' })]
    expect(pickNotices(prev, [row({ last_status: 'running', last_run_id: 'r-2' })])).toHaveLength(0)
    expect(pickNotices(
      [row({ last_status: 'completed', last_run_id: 'r-1' })],
      [row({ last_status: 'completed', last_run_id: 'r-2', enabled: 0 })],
    )).toHaveLength(0)
  })

  it('两侧都没有 run 号时退回「状态变化才发」，既不静默也不每轮都弹', () => {
    const prev = [row({ last_run_id: '' })]
    expect(pickNotices(prev, [row({ last_status: 'failed', last_run_id: '' })])).toHaveLength(1)
    expect(pickNotices(prev, [row({ last_run_id: '' })])).toHaveLength(0)
  })
})
