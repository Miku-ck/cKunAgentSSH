/**
 * ssh-handshake 回归：connectRemote 后写命令的确认式握手。
 *
 * 守三件事：
 * 1. 探针「引号拆段」构造 —— 输入回显（PTY 逐字原样）永远不含连续标记串，只有
 *    shell 真正执行 echo 的输出才含，慢主机在横幅期间提前回显输入不误判就绪；
 * 2. 等待逻辑 —— 标记跨 chunk 撕裂、ANSI 序列包裹、超长横幅滚动窗口都要能命中，
 *    等不到（登录挂起/通道死）按时超时并退订；
 * 3. 整体编排 —— 真命令只在探针命中后写入（保底再等挂载地板），超时回落固定
 *    延时照常写入并如实返回 confirmed=false；探针写入失败则真命令不再尝试。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  awaitProbeEcho,
  buildProbeLine,
  makeHandshakeMarker,
  SHELL_GRACE_FLOOR_MS,
  writeAfterShellReady,
  type ShellSessionIo,
} from './ssh-handshake'

/** 可驱动的假会话：记录写入顺序、手动喂输出块、统计存活订阅 */
function fakeSession(opts: { write?: (data: string) => Promise<unknown> } = {}) {
  const writes: string[] = []
  const cbs = new Set<(chunk: string) => void>()
  return {
    writes,
    io: {
      write: opts.write ?? (async (data: string) => { writes.push(data) }),
      onData: (cb: (chunk: string) => void) => {
        cbs.add(cb)
        return () => { cbs.delete(cb) }
      },
    } satisfies ShellSessionIo,
    emit(chunk: string) { for (const cb of [...cbs]) cb(chunk) },
    get liveCount() { return cbs.size },
  }
}

/** 探针的 echo 执行输出：两段引号拼回的连续标记串（输入回显里不可能出现的形态） */
function tokenOf(probeLine: string): string {
  const m = probeLine.match(/^echo (.+)"(.+)"\r$/)
  if (!m) throw new Error(`探针行不合预期：${probeLine}`)
  return m[1] + m[2]
}

/** 模拟真实回显序列：提示符 + 逐字回显的输入行（引号原样）+ 执行输出 + 新提示符 */
function echoBlock(probeLine: string): string {
  const token = tokenOf(probeLine)
  const echoed = probeLine.replace(/\r$/, '')
  return `\r\n$ ${echoed}\r\n${token}\r\n$ `
}

describe('makeHandshakeMarker', () => {
  it('一次性：两次生成互不相同，且只含安全字符（不引入 shell 元字符）', () => {
    const a = makeHandshakeMarker()
    const b = makeHandshakeMarker()
    expect(a).not.toBe(b)
    expect(a).toMatch(/^[a-z0-9]+$/)
  })
})

describe('buildProbeLine', () => {
  it('行尾带 \\r，引号正好把标记拆成两段（拼回去等于标记本身）', () => {
    const token = makeHandshakeMarker()
    const line = buildProbeLine(token)
    expect(line.endsWith('\r')).toBe(true)
    const m = line.match(/^echo ([^"]+)"([^"]+)"\r$/)
    expect(m).not.toBeNull()
    expect(m![1] + m![2]).toBe(token)
  })

  it('输入回显原样不含连续标记串：PTY 在横幅期间提前回显输入不构成「就绪」', () => {
    const token = makeHandshakeMarker()
    const echoed = `$ ${buildProbeLine(token).replace(/\r$/, '')}`
    expect(echoed.includes(token)).toBe(false)
  })
})

describe('awaitProbeEcho', () => {
  it('标记出现在单条 chunk 即命中并退订', async () => {
    const s = fakeSession()
    const token = makeHandshakeMarker()
    const p = awaitProbeEcho(s.io.onData, token, 5000)
    s.emit(`banner\r\n${token}\r\n$ `)
    await expect(p).resolves.toBe(true)
    expect(s.liveCount).toBe(0)
  })

  it('标记跨 chunk 撕裂也能在累积后命中', async () => {
    const s = fakeSession()
    const token = makeHandshakeMarker()
    const p = awaitProbeEcho(s.io.onData, token, 5000)
    const half = Math.ceil(token.length / 2)
    s.emit(`$ echo …\r\n${token.slice(0, half)}`)
    s.emit(`${token.slice(half)}\r\n$ `)
    await expect(p).resolves.toBe(true)
  })

  it('输入回显（带引号）不算命中，等不到执行输出按时超时并退订', async () => {
    vi.useFakeTimers()
    try {
      const s = fakeSession()
      const probeLine = buildProbeLine(makeHandshakeMarker())
      const p = awaitProbeEcho(s.io.onData, tokenOf(probeLine), 5000)
      // 慢主机：横幅 + 提示符 + 逐字回显输入（引号原样在），shell 尚未执行探针
      s.emit(`Last login: …\r\n$ ${probeLine.replace(/\r$/, '')}`)
      await vi.advanceTimersByTimeAsync(5000)
      await expect(p).resolves.toBe(false)
      expect(s.liveCount).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('CSI/OSC 序列剥除后仍能命中（含拦腰截断标记的序列）', async () => {
    const s = fakeSession()
    const token = makeHandshakeMarker()
    const p = awaitProbeEcho(s.io.onData, token, 5000)
    s.emit('\x1b]0;host: ~\x07\x1b[?2004h')
    s.emit(`${token.slice(0, 4)}\x1b[0m${token.slice(4)}\r\n`)
    await expect(p).resolves.toBe(true)
  })

  it('超长横幅冲出滚动窗口后，标记仍可命中（窗口保住最近输出）', async () => {
    const s = fakeSession()
    const token = makeHandshakeMarker()
    const p = awaitProbeEcho(s.io.onData, token, 5000)
    s.emit('motd '.repeat(4000))
    s.emit(token)
    await expect(p).resolves.toBe(true)
  })
})

describe('writeAfterShellReady', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('探针先写；真命令等探针命中且挂载地板耗尽后才写，返回 confirmed=true', async () => {
    const s = fakeSession()
    const cmd = 'docker exec -it web bash\r'
    const p = writeAfterShellReady(s.io, cmd)
    expect(s.writes).toHaveLength(1)
    expect(s.writes[0].startsWith('echo ')).toBe(true)
    const probeLine = s.writes[0]
    s.emit(echoBlock(probeLine)) // 快主机：探针立即执行完
    // 挂载地板未到：真命令的回显不能早于新标签订阅（否则整段丢帧）
    expect(s.writes).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(SHELL_GRACE_FLOOR_MS)
    expect(await p).toEqual({ confirmed: true })
    expect(s.writes).toEqual([probeLine, cmd])
  })

  it('慢主机：横幅与输入回显先到不命中，探针输出出现后真命令才写', async () => {
    const s = fakeSession()
    const cmd = 'cd /var/www/app\r'
    const p = writeAfterShellReady(s.io, cmd)
    const probeLine = s.writes[0]
    // 横幅 + 逐字回显输入 —— 旧固定延时实现此时早已把真命令写偏
    s.emit(`Welcome to Ubuntu 22.04\r\n$ ${probeLine.replace(/\r$/, '')}`)
    await vi.advanceTimersByTimeAsync(2000)
    expect(s.writes).toEqual([probeLine])
    s.emit(`${tokenOf(probeLine)}\r\n$ `) // profile 跑完，探针输出到达
    expect(await p).toEqual({ confirmed: true }) // 地板早已耗尽，不再额外等
    expect(s.writes).toEqual([probeLine, cmd])
  })

  it('探针超时回落：命令照常写入并返回 confirmed=false（地板已并行耗尽，不加额外延时）', async () => {
    const s = fakeSession()
    const cmd = 'cd /srv\r'
    const p = writeAfterShellReady(s.io, cmd)
    const probeLine = s.writes[0]
    s.emit('登录横幅一直没完\r\n')
    await vi.advanceTimersByTimeAsync(5000)
    expect(await p).toEqual({ confirmed: false })
    expect(s.writes).toEqual([probeLine, cmd])
  })

  it('探针写入失败（通道已坏）：原样抛出、真命令不再尝试，探针等待到点自行退订', async () => {
    const s = fakeSession({ write: async () => { throw new Error('channel closed') } })
    await expect(writeAfterShellReady(s.io, 'cd /srv\r')).rejects.toThrow('channel closed')
    expect(s.writes).toEqual([])
    expect(s.liveCount).toBe(1) // 探针等待仍挂着（未超时）
    await vi.advanceTimersByTimeAsync(5000)
    expect(s.liveCount).toBe(0) // 到点自行退订，无泄漏
  })
})
