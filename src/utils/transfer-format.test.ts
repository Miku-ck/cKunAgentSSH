/**
 * transfer-format —— 传输行上的数字到底可不可信
 *
 * 这组函数原先长在 SftpView.vue 里、渲染时现读 Date.now()：没有 tick 驱动，
 * 暂停中与 total===0 的任务收不到进度事件，「剩余 / 已运行」永久僵在最后一条
 * 事件上。搬成纯函数并把 now 显式化之后，这里钉住三件事：
 *   1. 不该有值的时候给「—」而不是 0 —— 0 会被渲染成 00:00，读起来像"马上就完"；
 *   2. 非 running 的行速率恒为 0 —— 暂停中的行挂一个还在跳的速度是假信号；
 *   3. 时间一律人类可读 —— 裸秒（`剩余 125s`）在长任务下没法扫读。
 */
import { describe, expect, it } from 'vitest'

import { formatBytes } from '@/utils/format'
import {
  averageSpeed,
  formatClock,
  formatDuration,
  formatEta,
  formatRate,
  isIndeterminate,
  progressPercent,
  remainingSeconds,
  truncatePath,
  type TransferProgress,
} from '@/utils/transfer-format'

const T0 = 1_700_000_000_000

function item(over: Partial<TransferProgress> = {}): TransferProgress {
  return {
    status: 'running',
    transferred: 0,
    total: 100,
    startTime: T0,
    ...over,
  }
}

describe('progressPercent / isIndeterminate', () => {
  it('total 未知时百分比为 0，并由 isIndeterminate 决定画滑块而不是 0%', () => {
    expect(progressPercent(item({ total: 0 }))).toBe(0)
    expect(isIndeterminate(item({ total: 0, transferred: 500 }))).toBe(true)
  })

  it('钳制到 100，不被续传竞态下的 transferred > total 顶出进度条', () => {
    expect(progressPercent(item({ transferred: 120, total: 100 }))).toBe(100)
    expect(progressPercent(item({ transferred: 33, total: 100 }))).toBe(33)
  })

  it('已暂停的未知总量任务不算不确定：条该停在原地，不该继续跑动画', () => {
    expect(isIndeterminate(item({ status: 'paused', total: 0 }))).toBe(false)
    expect(isIndeterminate(item({ status: 'done', total: 0 }))).toBe(false)
  })
})

describe('averageSpeed / remainingSeconds', () => {
  it('只有 running 有速率：暂停行挂一个还在跳的 MB/s 是假信号', () => {
    const t = item({ transferred: 500, total: 1000, startTime: T0 })
    expect(averageSpeed(t, T0 + 1000)).toBe(500)
    expect(averageSpeed({ ...t, status: 'paused' }, T0 + 1000)).toBe(0)
    expect(averageSpeed({ ...t, status: 'done' }, T0 + 1000)).toBe(0)
  })

  it('now 早于或等于 startTime 时不除出 Infinity / 负速率', () => {
    const t = item({ transferred: 10 })
    expect(averageSpeed(t, T0)).toBe(0)
    expect(averageSpeed(t, T0 - 5000)).toBe(0)
    expect(Number.isFinite(averageSpeed(t, T0 + 1))).toBe(true)
  })

  it('速率或总量未知 → null，让调用方画「—」而不是 00:00', () => {
    expect(remainingSeconds(item({ total: 0 }), T0 + 1000)).toBeNull()
    expect(remainingSeconds(item({ transferred: 100, total: 100 }), T0 + 1000)).toBeNull()
    expect(remainingSeconds(item({ status: 'paused' }), T0 + 1000)).toBeNull()
    // 刚起步、还没收到任何字节：没有速率可外推
    expect(remainingSeconds(item({ transferred: 0 }), T0 + 1000)).toBeNull()
    expect(remainingSeconds(item({ transferred: 50, total: 100 }), T0 + 1000)).toBe(1)
  })

  it('formatEta 走同一条未知路径', () => {
    expect(formatEta(item({ total: 0 }), T0 + 1000)).toBe('—')
    expect(formatEta(item({ transferred: 50, total: 100 }), T0 + 1000)).toBe('00:01')
  })
})

describe('formatClock / formatRate', () => {
  it('剩余时间人类可读：125 秒是 02:05，不是「剩余 125s」', () => {
    expect(formatClock(125)).toBe('02:05')
    expect(formatClock(0)).toBe('00:00')
    expect(formatClock(3725)).toBe('1:02:05')
    expect(formatClock(59)).toBe('00:59')
  })

  it('负数与 NaN 落到「—」，不渲染 -1:-1', () => {
    expect(formatClock(-1)).toBe('—')
    expect(formatClock(Number.NaN)).toBe('—')
    expect(formatClock(Number.POSITIVE_INFINITY)).toBe('—')
  })

  it('速率单位阶梯与 formatBytes 对齐', () => {
    expect(formatRate(0)).toBe('0 B/s')
    expect(formatRate(512)).toBe('512 B/s')
    expect(formatRate(2048)).toBe('2.0 KB/s')
    expect(formatRate(3.2 * 1024 * 1024)).toBe('3.2 MB/s')
    expect(formatRate(2 * 1024 ** 3)).toBe('2.0 GB/s')
    // 钉住"就是同一份阶梯"而不是"恰好数值相同"：再抄一份 ladder 就会在这里红
    for (const v of [0, 512, 2048, 3.2 * 1024 * 1024, 2 * 1024 ** 3, 5 * 1024 ** 4]) {
      expect(formatRate(v)).toBe(formatBytes(v) + '/s')
    }
  })
})

describe('formatDuration', () => {
  it('亚秒级任务保留一位小数，否则 0.4 秒显示成 00:00 像是没跑完', () => {
    expect(formatDuration(item({ endTime: T0 + 400 }), T0 + 400)).toBe('0.4s')
    expect(formatDuration(item({ endTime: T0 + 41_000 }), T0 + 41_000)).toBe('00:41')
  })

  it('没有 startTime 的行不编造时长', () => {
    expect(formatDuration(item({ startTime: 0 }), T0)).toBe('—')
  })
})

describe('truncatePath', () => {
  it('短于阈值原样返回', () => {
    expect(truncatePath('/opt/ckun/app.js')).toBe('/opt/ckun/app.js')
  })

  it('长路径保留末两段，Windows 反斜杠同样能切', () => {
    expect(truncatePath('/opt/ckun/releases/dist-2026-10-13/assets/index-4f2a.js'))
      .toBe('.../assets/index-4f2a.js')
    expect(truncatePath('D:\\workspace\\ckun-agentic\\dist\\assets\\index-4f2a.js'))
      .toBe('.../assets/index-4f2a.js')
  })

  it('只有一段时退化为尾部截断，不返回空串', () => {
    const long = 'a'.repeat(40)
    expect(truncatePath(long)).toBe('...' + long.slice(-27))
  })
})
