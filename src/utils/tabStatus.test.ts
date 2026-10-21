import { describe, expect, it } from 'vitest'
import { tabStatusKind } from './tabStatus'

describe('tabStatusKind：标签状态点判据', () => {
  const base = { id: 's1' }

  it('健康会话无状态点', () => {
    expect(tabStatusKind({ ...base }, '')).toBeNull()
  })

  it('后台活动：非活跃会话才有，活跃会话不亮（人正看着它）', () => {
    expect(tabStatusKind({ ...base, hasActivity: true }, 'other')).toBe('activity')
    expect(tabStatusKind({ ...base, hasActivity: true }, 's1')).toBeNull()
  })

  it('重连中：活跃会话也亮（状态比位置重要）', () => {
    const s = { ...base, reconnect: { attempt: 1, maxAttempts: 3 } }
    expect(tabStatusKind(s, 's1')).toBe('reconnecting')
    expect(tabStatusKind(s, 'other')).toBe('reconnecting')
  })

  it('优先级：gave-up > reconnecting > activity', () => {
    expect(tabStatusKind({ ...base, reconnectGaveUp: true, reconnect: { attempt: 1, maxAttempts: 3 }, hasActivity: true }, 'other')).toBe('gave-up')
    expect(tabStatusKind({ ...base, reconnect: { attempt: 1, maxAttempts: 3 }, hasActivity: true }, 'other')).toBe('reconnecting')
  })
})
