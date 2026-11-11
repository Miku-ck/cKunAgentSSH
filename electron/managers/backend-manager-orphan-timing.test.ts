/**
 * A1 时序契约回归：孤儿枚举的 await 汇合必须允许晚于 spawn（不再垫高启动
 * 关键路径），而防误杀由 killPids 的 exclude 承担 —— 新后端的 pid 出现在
 * 枚举结果里也不能被 taskkill。
 */
import { EventEmitter } from 'events'
import { describe, expect, it, vi, beforeEach, afterEach, type Mock } from 'vitest'

vi.mock('electron', () => ({
  app: { getPath: () => '', getAppPath: () => '/fake/app', isPackaged: false },
}))
vi.mock('fs', async (orig) => {
  const actual = await orig<typeof import('fs')>()
  return { ...actual, existsSync: () => true }
})
vi.mock('child_process', async (orig) => {
  const actual = await orig<typeof import('child_process')>()
  return { ...actual, execFile: vi.fn(), spawn: vi.fn() }
})

import { execFile, spawn } from 'child_process'
import { BackendManager } from './backend-manager'

const execFileMock = execFile as unknown as Mock
const spawnMock = spawn as unknown as Mock

// 每个用例重建实例并还原原型桩，避免实例间状态串味
const proto = BackendManager.prototype as unknown as Record<string, unknown>
const saved: Record<string, unknown> = {}

beforeEach(() => {
  for (const k of ['findFreePort', 'resolvePythonCommand', 'ensureDbPath', 'getBackendDir', 'waitForReady']) {
    saved[k] = proto[k]
  }
  proto.findFreePort = () => Promise.resolve(7777)
  proto.resolvePythonCommand = () => Promise.resolve({ cmd: 'python', prefixArgs: [] })
  proto.ensureDbPath = () => '/tmp/ckun-fake.db'
  proto.getBackendDir = () => '/fake/app/backend'
  proto.waitForReady = () => new Promise<void>((r) => { setTimeout(r, 0) })
})
afterEach(() => {
  Object.assign(proto, saved)
  execFileMock.mockReset()
  spawnMock.mockReset()
})

/** 可控的 CIM 枚举：回调扣在手里，由测试决定何时放行 */
function stubEnumeration() {
  let cimCb: ((e: Error | null, out: string) => void) | null = null
  execFileMock.mockImplementation((_cmd: string, args: string[], _o: unknown, cb: (e: Error | null, out: string) => void) => {
    // 查询串在 -Command 之后的位置参数里，必须扫全部 args 而非 args[0]
    if (args.some((a) => String(a).includes('Get-CimInstance'))) {
      cimCb = cb
    } else {
      process.nextTick(() => cb(null, '')) // taskkill 等：立即成功
    }
  })
  return {
    /** 放行枚举，三行模拟真机形态：
     *  - 9999  --port 7778：上一次会话残留的旧孤儿（不同端口）→ 必须被杀
     *  - 4321  --port 7777：本次 spawn 的直接子进程（== child.pid）→ 端口+pid 双重豁免
     *  - 5678  --port 7777：本次后端的孙进程（python.cmd 包装器拉起的真实 python.exe，
     *    pid ≠ child.pid，但服务同一个本次端口）→ 只能靠端口签名豁免（回归用例：
     *    旧实现按 child.pid 排除护不住它 → 被 taskkill 误杀 → 重启风暴）
     *  命令行带 --host 127.0.0.1 + \backend\main.py 才能过 isOurBackendProcess 宽口径 */
    resolve() {
      cimCb!(null,
        '9999|D:\\app\\backend\\main.py --port 7778 --host 127.0.0.1\r\n'
        + '4321|D:\\app\\backend\\main.py --port 7777 --host 127.0.0.1\r\n'
        + '5678|D:\\app\\backend\\main.py --port 7777 --host 127.0.0.1')
    },
  }
}

function stubSpawn() {
  const child = new EventEmitter() as EventEmitter & { pid: number; stdout: null; stderr: null }
  child.stdout = null
  child.stderr = null
  spawnMock.mockReturnValue(child)
  return child
}

describe('A1 孤儿枚举汇合时机', () => {
  it('spawn 不被枚举 await 阻塞：枚举未回，spawn 已发生', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const enumStub = stubEnumeration()
    const child = stubSpawn()
    child.pid = 4321
    const bm = new BackendManager()
    const p = bm.start()
    await new Promise((r) => setImmediate(r))
    expect(spawnMock).toHaveBeenCalledTimes(1) // 枚举回调还没放行
    enumStub.resolve()
    await p
    // taskkill 只许对旧孤儿 9999（不同端口 7778）下手；本次启动的后端进程——
    // 无论 pid 是否等于 child.pid——只要服务的就是本次端口，一律豁免。
    const killed = execFileMock.mock.calls
      .filter((c) => c[0] === 'taskkill' || String(c[0]).endsWith('taskkill.exe'))
      .map((c) => (c[1] as string[]).join(' '))
    expect(killed.some((a) => a.includes('/PID 9999'))).toBe(true)
    expect(killed.some((a) => a.includes('/PID 4321'))).toBe(false)
    // 回归断言：孙进程 5678（pid≠child.pid、同端口 7777）绝不能再被误杀
    expect(killed.some((a) => a.includes('/PID 5678'))).toBe(false)
  })
})
