/**
 * parseBackendTs —— 后端时间戳的时区口径（第 6 轮审查修复的回归）
 *
 * 后端统一存 UTC naive 字符串（database._now / compute_next_run），
 * 少了尾部 Z 就会被 new Date() 按本地时区解释：UTC+8 下定时任务的
 * 「下次执行」显示成 8 小时前，倒计时恒为「即将」。
 */
import { describe, expect, it } from 'vitest'

import { parseBackendTs } from '@/utils/format'

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
