/**
 * 崩溃上报接线（electron/crash-reporter.ts）守卫。
 *
 * 这里的每一条都对应一个"看起来没问题、现场却抓不到"的坑：
 *   - 转储目录不固定到 userData → 诊断包按固定路径取，永远 0 份；
 *   - 忘记 uploadToServer: false → Electron 默认 true，会在没有 submitURL 的情况下报错；
 *   - 崩溃不落日志 → 只拿到一份 .dmp，看不出崩的是渲染进程还是 GPU 子进程。
 */
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  setPath: vi.fn(),
  start: vi.fn(),
  handlers: {} as Record<string, (...args: unknown[]) => void>,
  startThrows: false,
}))

vi.mock('electron', () => ({
  app: {
    getName: () => 'cKunAgentSSH',
    getVersion: () => '0.1.0',
    setPath: (kind: string, p: string) => {
      if (h.startThrows) throw new Error('setPath 失败')
      h.setPath(kind, p)
    },
    on: (event: string, fn: (...args: unknown[]) => void) => {
      h.handlers[event] = fn
    },
  },
  crashReporter: {
    start: (o: unknown) => {
      if (h.startThrows) throw new Error('start 失败')
      h.start(o)
    },
  },
}))

let root = ''

beforeEach(() => {
  vi.resetModules()
  h.setPath.mockClear()
  h.start.mockClear()
  h.handlers = {}
  h.startThrows = false
  root = mkdtempSync(join(tmpdir(), 'ckun-crash-'))
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

describe('setupCrashReporter', () => {
  it('转储目录固定到 userData/Crashpad（诊断包按这个路径取），并只收集不上传', async () => {
    const { setupCrashReporter } = await import('./crash-reporter')
    const dir = setupCrashReporter({ userDataDir: root })

    expect(dir).toBe(join(root, 'Crashpad'))
    // 目录要真建出来：crashpad 在目录不存在时不会自己创建
    expect(statSync(dir).isDirectory()).toBe(true)
    expect(h.setPath).toHaveBeenCalledWith('crashDumps', dir)
    expect(h.start).toHaveBeenCalledTimes(1)
    const opts = h.start.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.uploadToServer).toBe(false)
    expect(opts.productName).toBe('cKunAgentSSH')
    expect((opts.globalExtra as Record<string, string>)['app.version']).toBe('0.1.0')
  })

  it('渲染进程/子进程崩溃落进主日志（只留 .dmp 读不出"崩的是谁"）', async () => {
    const { setupCrashReporter } = await import('./crash-reporter')
    const lines: string[] = []
    setupCrashReporter({ userDataDir: root, log: (_l, m) => lines.push(m) })

    h.handlers['render-process-gone']!({}, {}, { reason: 'crashed', exitCode: 5 })
    h.handlers['child-process-gone']!({}, { type: 'GPU', reason: 'abnormal-exit', exitCode: 3, name: 'gpu-process' })

    expect(lines.some((l) => l.includes('渲染进程崩溃') && l.includes('reason=crashed') && l.includes('exitCode=5'))).toBe(true)
    expect(lines.some((l) => l.includes('子进程崩溃') && l.includes('type=GPU') && l.includes('name=gpu-process'))).toBe(true)
  })

  it('启动失败只警告，不抛 —— 崩溃上报不能变成新的起不来原因', async () => {
    h.startThrows = true
    const { setupCrashReporter } = await import('./crash-reporter')
    const warns: string[] = []
    const dir = setupCrashReporter({ userDataDir: root, log: (l, m) => { if (l === 'warn') warns.push(m) } })

    expect(dir).toBe(join(root, 'Crashpad'))
    expect(warns.join('\n')).toContain('崩溃上报未启用')
    // 即使启动链路全挂，进程崩溃事件仍要挂上（事故现场靠它解释）
    expect(h.handlers['render-process-gone']).toBeTypeOf('function')
    expect(h.handlers['child-process-gone']).toBeTypeOf('function')
  })

  it('重复调用幂等：只 start 一次', async () => {
    const { setupCrashReporter } = await import('./crash-reporter')
    setupCrashReporter({ userDataDir: root })
    setupCrashReporter({ userDataDir: root })
    expect(h.start).toHaveBeenCalledTimes(1)
  })
})
