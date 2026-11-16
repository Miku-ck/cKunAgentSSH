/**
 * ExecRegistry — bridge ssh_kill op 查杀依据的回归测试（C-review M9 落地）。
 *
 * 守护点：
 * - 三态查杀：通道已建立 → signal('KILL')；通道未建立 → pendingKill 待补杀；
 *   无登记 → found:false（诚实上报，不是错误）；
 * - signal 失败降级 close（sshd 不支持 exec 通道 signal 的兜底）；
 * - attachStream 时 pendingKill 已置位 → 立即补杀（kill 早于回调的窗口）；
 * - 生命周期：register → attachStream → settle 摘除，防登记表泄漏。
 */
import { describe, expect, it, vi } from 'vitest'
import { ExecRegistry, type KillableStream } from './exec-registry'

function fakeStream(opts: { signalThrows?: boolean; closeThrows?: boolean } = {}): KillableStream & {
  signal: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
} {
  const signal = vi.fn(() => {
    if (opts.signalThrows) throw new Error('channel request refused')
  })
  const close = vi.fn(() => {
    if (opts.closeThrows) throw new Error('already closed')
  })
  return { signal, close }
}

describe('ExecRegistry.kill 三态', () => {
  it('通道已建立：signal KILL 成功并摘除登记', () => {
    const reg = new ExecRegistry()
    reg.register('e1')
    const stream = fakeStream()
    reg.attachStream('e1', stream)
    const out = reg.kill('e1')
    expect(out).toEqual({ found: true, signaled: true, pending: false })
    expect(stream.signal).toHaveBeenCalledWith('KILL')
    expect(reg.has('e1')).toBe(false)
  })

  it('通道未建立：登记 pendingKill，回调到达时补杀', () => {
    const reg = new ExecRegistry()
    reg.register('e1')
    const out = reg.kill('e1')
    expect(out).toEqual({ found: true, signaled: false, pending: true })
    // kill 先到，回调后到：attach 即补杀（不等到下一次 kill 请求）
    const stream = fakeStream()
    reg.attachStream('e1', stream)
    expect(stream.signal).toHaveBeenCalledWith('KILL')
    expect(reg.has('e1')).toBe(false)
  })

  it('无登记：found false（已结束或从未存在，不是错误）', () => {
    const reg = new ExecRegistry()
    expect(reg.kill('ghost')).toEqual({ found: false, signaled: false, pending: false })
  })

  it('signal 被服务端拒绝：降级 close 兜底', () => {
    const reg = new ExecRegistry()
    reg.register('e1')
    const stream = fakeStream({ signalThrows: true })
    reg.attachStream('e1', stream)
    const out = reg.kill('e1')
    expect(out).toEqual({ found: true, signaled: false, pending: false })
    expect(stream.close).toHaveBeenCalled()
    expect(reg.has('e1')).toBe(false)
  })
})

describe('ExecRegistry 生命周期', () => {
  it('settle 摘除后 kill 返回 found false（不误杀已结束的执行）', () => {
    const reg = new ExecRegistry()
    reg.register('e1')
    reg.attachStream('e1', fakeStream())
    reg.settle('e1')
    expect(reg.kill('e1').found).toBe(false)
  })

  it('attach 到已摘除的执行是 no-op（迟到回调不复活登记）', () => {
    const reg = new ExecRegistry()
    reg.register('e1')
    reg.settle('e1')
    reg.attachStream('e1', fakeStream())
    expect(reg.has('e1')).toBe(false)
  })

  it('clear 清空全部登记', () => {
    const reg = new ExecRegistry()
    reg.register('a')
    reg.register('b')
    reg.clear()
    expect(reg.has('a')).toBe(false)
    expect(reg.has('b')).toBe(false)
  })
})
