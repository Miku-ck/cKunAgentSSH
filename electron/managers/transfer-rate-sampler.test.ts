/**
 * 传输速率采样与动态停滞阈值（S5-34）。
 *
 * 原实现用固定 30s 停滞阈值：15GB 在慢链路上只要有一次网络抖动就误判 STALLED
 * 并中断，用户看到的是「传着传着就断了」。改成按「传满一个在途窗口（8×256KB
 * = 2MB）所需时间」的 3 倍算阈值 —— 慢链路自然放宽，快链路不放宽（钳到 30s）。
 *
 * 这里钉三件事：
 * 1. 采样器的滑动窗口语义（过期点丢弃、同毫秒合并、累计值而非增量）
 * 2. 动态阈值的钳制区间与边界值
 * 3. 速率未建立时兜底 30s（与原实现同口径，避免刚起步就判死）
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { TransferHeartbeat, TransferRateSampler } from './ssh-manager'

afterEach(() => vi.useRealTimers())

describe('TransferRateSampler', () => {
  it('样本不足两个点时速率未建立（返回 0）', () => {
    vi.useFakeTimers()
    const s = new TransferRateSampler(60000)
    expect(s.getRateBytesPerSec()).toBe(0)
    s.record(0)
    expect(s.getRateBytesPerSec()).toBe(0)
  })

  it('按窗口内首末两点算平均速率', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const s = new TransferRateSampler(60000)
    s.record(0)
    // 1 秒传了 1000 字节 → 速率 1000 B/s
    vi.setSystemTime(new Date('2026-01-01T00:00:01Z'))
    s.record(1000)
    expect(s.getRateBytesPerSec()).toBeCloseTo(1000, 0)
  })

  it('过期点被丢弃：窗口外的旧样本不参与计算', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const s = new TransferRateSampler(1000) // 1s 窗口
    s.record(0)
    vi.setSystemTime(new Date('2026-01-01T00:00:00.5Z'))
    s.record(500)
    // 跳到 2s 后：前两个点都过期，只剩新点 → 速率未建立
    vi.setSystemTime(new Date('2026-01-01T00:00:02Z'))
    s.record(2000)
    expect(s.getRateBytesPerSec()).toBe(0)
  })

  it('同毫秒内的多次记录只保留最后一个（避免高频上报撑成噪声）', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const s = new TransferRateSampler(60000)
    s.record(100)
    s.record(200)   // 同一毫秒
    s.record(300)   // 同一毫秒
    expect(s.getRateBytesPerSec()).toBe(0) // 只有一个点
  })

  it('时间跨度太短不算速率（<100ms）', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const s = new TransferRateSampler(60000)
    s.record(0)
    vi.setSystemTime(new Date('2026-01-01T00:00:00.05Z')) // 50ms 后
    s.record(1000)
    expect(s.getRateBytesPerSec()).toBe(0)
  })
})

describe('TransferHeartbeat 动态停滞阈值', () => {
  it('快链路阈值钳到下限 30s（不因为速率快就放松）', () => {
    vi.useFakeTimers()
    const h = new TransferHeartbeat()
    // 100MB/s：2MB 窗口 0.02s 传完，×3 = 60ms → 钳到 30s
    const threshold = (h as any).dynamicThreshold(100 * 1024 * 1024)
    expect(threshold).toBe(30000)
  })

  it('慢链路阈值放宽：100KB/s → 2MB 需 20.48s，×3 = 61.44s', () => {
    vi.useFakeTimers()
    const h = new TransferHeartbeat()
    const threshold = (h as any).dynamicThreshold(100 * 1024)
    expect(threshold).toBe(61440)
  })

  it('极慢链路阈值钳到上限 5min', () => {
    vi.useFakeTimers()
    const h = new TransferHeartbeat()
    // 1KB/s：2MB 需 2000s，×3 = 6000s → 钳到 300s
    const threshold = (h as any).dynamicThreshold(1024)
    expect(threshold).toBe(300000)
  })

  it('速率未建立（0 或负值）兜底 30s，与原实现同口径', () => {
    vi.useFakeTimers()
    const h = new TransferHeartbeat()
    expect((h as any).dynamicThreshold(0)).toBe(30000)
    expect((h as any).dynamicThreshold(-1)).toBe(30000)
    expect((h as any).dynamicThreshold(NaN)).toBe(30000)
  })

  it('不传采样器时保持固定阈值（向后兼容）', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    let stalled = false
    const h = new TransferHeartbeat()
    h.start(1000, 5000, () => Date.now() - 6000, () => { stalled = true })
    vi.advanceTimersByTime(2000)
    expect(stalled).toBe(true) // 6000ms 没进展 > 5000ms 阈值
  })

  it('传采样器时按动态阈值判定：慢链路 60s 阈值下 30s 无进展不算停滞', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    let stalled = false
    let lastProgress = Date.now()
    const h = new TransferHeartbeat()
    // 100KB/s → 动态阈值 60s
    h.start(1000, 30000, () => lastProgress, () => { stalled = true }, () => 100 * 1024)
    vi.advanceTimersByTime(30000) // 30s 后
    expect(stalled).toBe(false)     // 未超 60s 阈值
    vi.advanceTimersByTime(35000) // 共 65s
    expect(stalled).toBe(true)      // 超 60s 阈值
  })
})
