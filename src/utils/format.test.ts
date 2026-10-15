/**
 * utils/format 单一源测试
 *
 * parseBackendTs —— 后端时间戳的时区口径（第 6 轮审查修复的回归）
 * formatBytesCompact：原 docker-core.ts humanSize 收编于此（ARCH-010），
 * docker 域语义（字符串直通 / 非法入 undefined）随实现一并迁移。
 */
import { describe, expect, it } from 'vitest'
import { formatBytesCompact, parseBackendTs } from './format'

describe('parseBackendTs', () => {
  it('UTC naive 串按 UTC 解释（不补 Z 就会被当本地时间）', () => {
    expect(parseBackendTs('2026-09-21 07:00:00').getTime()).toBe(Date.UTC(2026, 8, 21, 7, 0, 0))
  })

  it('已带时区标记的原样解析，不重复补 Z', () => {
    expect(parseBackendTs('2026-09-21T07:00:00Z').getTime()).toBe(Date.UTC(2026, 8, 21, 7))
    expect(parseBackendTs('2026-09-21T07:00:00+08:00').getTime()).toBe(Date.UTC(2026, 8, 20, 23))
    // 负偏移：旧 /[Z+]/ 判定漏掉 '-'，会误补 Z 得到 "...-05:00Z" → Invalid Date
    expect(parseBackendTs('2026-09-21T07:00:00-05:00').getTime()).toBe(Date.UTC(2026, 8, 21, 12))
  })

  it('空值只给出 Invalid Date，不抛异常', () => {
    expect(Number.isNaN(parseBackendTs('').getTime())).toBe(true)
    expect(Number.isNaN(parseBackendTs(undefined).getTime())).toBe(true)
  })
})

describe('formatBytesCompact', () => {
  it('bytes → 1024 进制人读（无空格分隔）', () => {
    expect(formatBytesCompact(0)).toBe('0B')
    expect(formatBytesCompact(1023)).toBe('1023B')
    expect(formatBytesCompact(1024)).toBe('1.0KB')
    expect(formatBytesCompact(1073741824)).toBe('1.0GB')
  })
  it('字符串透传 / 非法入 undefined', () => {
    expect(formatBytesCompact('62.1MB')).toBe('62.1MB')
    expect(formatBytesCompact('')).toBeUndefined()
    expect(formatBytesCompact(null)).toBeUndefined()
    expect(formatBytesCompact(undefined)).toBeUndefined()
    expect(formatBytesCompact(-1)).toBeUndefined()
    expect(formatBytesCompact(NaN)).toBeUndefined()
  })
})
