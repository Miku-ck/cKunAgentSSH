/**
 * 传输心跳「粘滞复位」回归（审查 MINOR）。
 *
 * 原实现 stalled 一旦置 true 永不复位：一次瞬时卡顿之后，本次传输的后续任何
 * 错误都会被 isStalled() 误判为 STALLED —— 错误分类失真，前端提示与
 * retryable 重试策略跟着错（本该按真实错误归类并决定 .part 去留的路径，
 * 全被盖成「停滞自动续传」）。
 * 修法：心跳 tick 检测到进度恢复（Date.now() - lastProgress 回到阈值内）时
 * 把 stalled 复位为 false。
 *
 * 这类判定是纯时序行为，形状守卫钉不住语义（复位写在哪个位置不重要，
 * 「恢复后不再报停滞」才重要），所以 TransferHeartbeat 导出后直接驱动行为，
 * 用 fake timers 同时推进 setInterval 与 Date.now，确定性地复现
 * 「卡顿 → 恢复 → 再判定」的完整序列。
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))
vi.mock('ssh2', () => ({ Client: class {} }))

import { TransferHeartbeat } from './ssh-manager'

const INTERVAL = 1000
const THRESHOLD = 5000

/**
 * 起一个心跳：lastProgress 由测试侧闭包变量控制（对应生产线里
 * 写循环进度回调刷新 lastProgress 的职责）。
 */
function harness() {
  let lastProgress = Date.now()
  let stallHits = 0
  const hb = new TransferHeartbeat()
  hb.start(INTERVAL, THRESHOLD, () => lastProgress, () => { stallHits++ })
  return {
    hb,
    touch: () => { lastProgress = Date.now() },
    get stallHits() { return stallHits },
    get isStalled() { return hb.isStalled() },
  }
}

describe('TransferHeartbeat 停滞粘滞复位', () => {
  it('进度停摆超过阈值 → 判定停滞一次', () => {
    vi.useFakeTimers()
    try {
      const h = harness()
      vi.advanceTimersByTime(THRESHOLD + INTERVAL)
      expect(h.isStalled).toBe(true)
      expect(h.stallHits, '同一轮停滞只回调一次，不重复刷屏').toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('瞬时卡顿后进度恢复 → 解除粘滞，后续错误不再被误判 STALLED', () => {
    vi.useFakeTimers()
    try {
      const h = harness()
      // 第一段：卡过阈值，进入停滞
      vi.advanceTimersByTime(THRESHOLD + INTERVAL)
      expect(h.isStalled).toBe(true)
      // 第二段：链路恢复，进度回调刷新 lastProgress，再走完一个检查周期
      h.touch()
      vi.advanceTimersByTime(INTERVAL)
      expect(h.isStalled, '复位缺陷回归：恢复后仍报停滞会把一切后续错误盖成 STALLED').toBe(false)
      // 第三段：恢复之后再次停摆 → 能重新判定停滞（复位不是一次性的）
      vi.advanceTimersByTime(THRESHOLD + INTERVAL)
      expect(h.isStalled).toBe(true)
      expect(h.stallHits).toBe(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('start 重新起搏时清掉上一轮的停滞标记', () => {
    vi.useFakeTimers()
    try {
      const h = harness()
      vi.advanceTimersByTime(THRESHOLD + INTERVAL)
      expect(h.isStalled).toBe(true)
      h.hb.start(INTERVAL, THRESHOLD, () => Date.now(), () => {})
      expect(h.hb.isStalled(), '复用实例起新传输时必须从干净状态开始').toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('stop 后定时器清空，进度再停摆也不产生新的停滞判定', () => {
    vi.useFakeTimers()
    try {
      const h = harness()
      h.hb.stop()
      vi.advanceTimersByTime(THRESHOLD * 3)
      expect(h.isStalled).toBe(false)
      expect(h.stallHits).toBe(0)
      expect(vi.getTimerCount(), 'stop 不遗留定时器（传输收尾后不得继续空转）').toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
