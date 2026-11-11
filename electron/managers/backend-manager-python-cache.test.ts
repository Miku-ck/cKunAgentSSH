/**
 * A2 解释器乐观缓存 × BackendManager 联动契约：
 * 命中零探测、绝对路径缺失回退探测、健康就绪前失败作废、就绪后落盘。
 */
import { EventEmitter } from 'events'
import { mkdtempSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

// 可变桩位：userData 路径按用例切换；existsSync 可控（供"缓存 cmd 不在盘上"用例改写）
const h = vi.hoisted(() => ({ userDataDir: '', exists: (_p: string): boolean => true }))

vi.mock('electron', () => ({
  app: {
    getPath: (k: string) => (k === 'userData' ? h.userDataDir : ''),
    getAppPath: () => '/fake/app',
    isPackaged: false,
  },
}))
vi.mock('fs', async (orig) => {
  const actual = await orig<typeof import('fs')>()
  return { ...actual, existsSync: (p: string) => h.exists(p) }
})
vi.mock('child_process', async (orig) => {
  const actual = await orig<typeof import('child_process')>()
  return { ...actual, execFile: vi.fn(), spawn: vi.fn() }
})

import { execFile, spawn } from 'child_process'
import { BackendManager } from './backend-manager'
import { loadPythonCache, savePythonCache, type PythonLaunch } from './backend-python-cache'

const execFileMock = execFile as unknown as Mock
const spawnMock = spawn as unknown as Mock

const proto = BackendManager.prototype as unknown as Record<string, unknown>
const saved: Record<string, unknown> = {}
let ud = ''

beforeEach(() => {
  for (const k of ['findFreePort', 'ensureDbPath', 'getBackendDir', 'waitForReady', 'killCurrentProcess']) {
    saved[k] = proto[k]
  }
  proto.findFreePort = () => Promise.resolve(7777)
  proto.ensureDbPath = () => '/tmp/ckun-fake.db'
  proto.getBackendDir = () => '/fake/app/backend'
  proto.waitForReady = () => new Promise<void>((r) => { setTimeout(r, 0) })
  proto.killCurrentProcess = () => {}
  h.exists = () => true
  ud = mkdtempSync(join(tmpdir(), 'ckun-py-mgr-'))
  h.userDataDir = ud
})
afterEach(() => {
  Object.assign(proto, saved)
  execFileMock.mockReset()
  spawnMock.mockReset()
  rmSync(ud, { recursive: true, force: true })
})

/** execFile 统一应答桩：CIM 枚举→空结果；解释器探测→按 passSet；其余（taskkill）→成功 */
function stubProbe(passSet: Set<string>) {
  execFileMock.mockImplementation((cmd: string, args: string[], _o: unknown, cb: (e: Error | null, out?: string) => void) => {
    if (args.some((a) => String(a).includes('Get-CimInstance'))) {
      process.nextTick(() => cb(null, ''))
    } else if (args.some((a) => String(a).includes('import fastapi'))) {
      process.nextTick(() => (passSet.has(cmd) ? cb(null, '') : cb(new Error('probe fail'))))
    } else {
      process.nextTick(() => cb(null, ''))
    }
  })
}

function stubSpawn() {
  const child = new EventEmitter() as EventEmitter & { pid: number; stdout: null; stderr: null }
  child.stdout = null
  child.stderr = null
  child.pid = 4321
  spawnMock.mockReturnValue(child)
  return child
}

/** 异步写落盘等待：轮询直到能解析出合法值（最多 1s，必须让出事件循环） */
async function waitForCache(d: string): Promise<PythonLaunch | null> {
  for (let i = 0; i < 20; i++) {
    const v = loadPythonCache(d)
    if (v) return v
    await new Promise((r) => setTimeout(r, 50))
  }
  return null
}

function resolveCmd(bm: BackendManager): Promise<PythonLaunch> {
  return (bm as unknown as { resolvePythonCommand(): Promise<PythonLaunch> }).resolvePythonCommand()
}

describe('A2 解释器乐观缓存', () => {
  it('缓存命中：resolvePythonCommand 零探测直接返回（裸名走 PATH 解析，不做 existsSync 盘查）', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    savePythonCache(ud, { cmd: 'python', prefixArgs: [] })
    expect(await waitForCache(ud)).not.toBeNull()
    const r = await resolveCmd(new BackendManager())
    expect(r).toEqual({ cmd: 'python', prefixArgs: [] })
    expect(execFileMock).not.toHaveBeenCalled() // 关键路径零探测
  })
  it('缓存的绝对路径 cmd 不存在 → 回退全量并行探测', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    savePythonCache(ud, { cmd: 'D:/gone/python.exe', prefixArgs: [] })
    expect(await waitForCache(ud)).not.toBeNull()
    h.exists = (p: string) => p !== 'D:/gone/python.exe'
    stubProbe(new Set(['python'])) // 全量探测 python 命中
    const r = await resolveCmd(new BackendManager())
    expect(r).toEqual({ cmd: 'python', prefixArgs: [] })
    expect(execFileMock).toHaveBeenCalledTimes(3) // 确实走了一轮并行探测
  })
  it('健康就绪前失败 → 缓存作废，下一次 start 走全量探测', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    savePythonCache(ud, { cmd: 'python', prefixArgs: [] })
    expect(await waitForCache(ud)).not.toBeNull()
    stubProbe(new Set(['python']))
    stubSpawn()
    proto.waitForReady = () => Promise.reject(new Error('health timeout'))
    const bm = new BackendManager()
    await expect(bm.start()).rejects.toThrow(/health timeout/)
    expect(loadPythonCache(ud)).toBeNull() // 缓存已作废（clear 为同步删除，无需轮询）
    // 下一次 resolve 回到全量探测：execFile 新增 3 次探测调用
    const before = execFileMock.mock.calls.length
    await resolveCmd(new BackendManager())
    expect(execFileMock.mock.calls.length - before).toBe(3)
  })
  it('start 成功（health ready）→ 本次解释器写入缓存', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    stubProbe(new Set(['python']))
    stubSpawn()
    const bm = new BackendManager()
    await bm.start()
    expect(await waitForCache(ud)).toEqual({ cmd: 'python', prefixArgs: [] })
  })
})
