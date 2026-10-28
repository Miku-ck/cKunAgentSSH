import { describe, expect, it } from 'vitest'
import { formatLocalDateTime } from './datetime'

const pad = (n: number) => String(n).padStart(2, '0')

describe('formatLocalDateTime', () => {
  it('把 UTC 字符串按本地时区显示', () => {
    const expected = new Date('2026-10-28T02:54:30Z')
    const stamp = `${pad(expected.getMonth() + 1)}-${pad(expected.getDate())} ${pad(expected.getHours())}:${pad(expected.getMinutes())}`

    expect(formatLocalDateTime('2026-10-28 02:54:30', new Date(2026, 9, 28, 12))).toBe(stamp)
  })

  it('跨年时带年份，避免把去年的记录看成今年', () => {
    const expected = new Date('2025-06-15T02:00:00Z')
    const stamp = `${pad(expected.getMonth() + 1)}-${pad(expected.getDate())} ${pad(expected.getHours())}:${pad(expected.getMinutes())}`

    expect(formatLocalDateTime('2025-06-15 02:00:00', new Date(2026, 0, 1))).toBe(`2025-${stamp}`)
  })

  it('空值或非法值返回空串', () => {
    expect(formatLocalDateTime('')).toBe('')
    expect(formatLocalDateTime('not-a-date')).toBe('')
  })
})
