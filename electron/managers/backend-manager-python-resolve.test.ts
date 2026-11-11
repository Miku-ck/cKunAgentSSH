/**
 * 回归：Windows 上裸名 'python' 可能解析到 Microsoft Store 的 App Execution
 * Alias 存根 —— spawn 成功、立即 exit 9009、无有效输出，后端健康检查只能等
 * 15s 超时且看不出原因（实测复现）。
 *
 * resolvePythonCommand 必须逐个探测候选（python / python3 / py -3），探测手段
 * 是 `import fastapi, uvicorn`（顺带把"解释器在但没装依赖"也挡在 spawn 前）；
 * 全部不可用时抛出带尝试明细的错误，而不是带着一个必死的解释器继续 spawn。
 */
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

vi.mock('electron', () => ({
  app: { getPath: () => '', isPackaged: false },
}))

vi.mock('child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('child_process')>()
  return { ...actual, execFile: vi.fn() }
})

import { execFile } from 'child_process'
import { BackendManager } from './backend-manager'

const execFileMock = execFile as unknown as Mock

const bm = new BackendManager()
// resolvePythonCommand 是编译期 private，运行时可直接取（只为测语义，不是后门）
const resolve = () =>
  (bm as unknown as { resolvePythonCommand: () => Promise<{ cmd: string; prefixArgs: string[] }> })
    .resolvePythonCommand()

const setPlatform = (p: string) =>
  Object.defineProperty(process, 'platform', { value: p, configurable: true })

/** execFile 桩：cmd 出现在 passSet 里则成功回调，否则 9009（Store 存根行为） */
const stubProbe = (passSet: Set<string>) => {
  execFileMock.mockImplementation(
    (cmd: string, _args: string[], _opts: unknown, cb: (e: Error | null) => void) => {
      const err = passSet.has(cmd) ? null : Object.assign(new Error('Python was not found'), { code: 9009 })
      process.nextTick(() => cb(err))
    },
  )
}

let savedPlatform: PropertyDescriptor
let savedCkunPython: string | undefined
let tmpDir = ''

beforeEach(() => {
  savedPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!
  savedCkunPython = process.env.CKUN_PYTHON
  delete process.env.CKUN_PYTHON
  execFileMock.mockReset()
})

afterEach(() => {
  Object.defineProperty(process, 'platform', savedPlatform)
  if (savedCkunPython === undefined) delete process.env.CKUN_PYTHON
  else process.env.CKUN_PYTHON = savedCkunPython
  vi.restoreAllMocks()
  if (tmpDir) {
    try { rmSync(tmpDir, { recursive: true }) } catch { /* 测试目录清理失败不阻塞 */ }
    tmpDir = ''
  }
})

describe('resolvePythonCommand —— 候选探测与回退', () => {
  it('win32：第一个候选可用时直接返回（并行探测全部候选，按优先级选首个成功）', async () => {
    setPlatform('win32')
    stubProbe(new Set(['python']))

    const r = await resolve()
    expect(r).toEqual({ cmd: 'python', prefixArgs: [] })
    // 并行探测：Promise.all 一次性派发全部 3 个候选，不会因首命中而短路 ——
    // 上限固定为一轮并行 spawn，性能不再随候选数量线性下降
    expect(execFileMock).toHaveBeenCalledTimes(3)
    // 探测命令必须验证依赖可导入，而不是只看解释器能否执行
    expect(execFileMock.mock.calls[0][1]).toEqual(['-c', 'import fastapi, uvicorn'])
  })

  it('win32：python/python3 都不可用（如 Store 存根）时回退到 py -3', async () => {
    setPlatform('win32')
    stubProbe(new Set(['py']))

    const r = await resolve()
    expect(r).toEqual({ cmd: 'py', prefixArgs: ['-3'] })
    expect(execFileMock).toHaveBeenCalledTimes(3)
    expect(execFileMock.mock.calls[2][1]).toEqual(['-3', '-c', 'import fastapi, uvicorn'])
  })

  it('win32：全部候选不可用时抛错且错误里带尝试明细', async () => {
    setPlatform('win32')
    stubProbe(new Set())

    await expect(resolve()).rejects.toThrow(/未找到可运行后端的 Python 环境/)
    await expect(resolve()).rejects.toThrow(/python、python3、py -3/)
    await expect(resolve()).rejects.toThrow(/CKUN_PYTHON/)
  })

  it('posix：优先 python3，回退 python（并行探测 2 候选后按优先级选 py3）', async () => {
    setPlatform('linux')
    stubProbe(new Set(['python3']))

    const r = await resolve()
    expect(r).toEqual({ cmd: 'python3', prefixArgs: [] })
    // 并行探测：2 个候选一次性派发，结果数组按优先级排序后取首命中
    expect(execFileMock).toHaveBeenCalledTimes(2)
  })

  it('CKUN_PYTHON 显式指定且存在时不做探测直接信任', async () => {
    setPlatform('win32')
    tmpDir = mkdtempSync(join(tmpdir(), 'ckun-py-resolve-'))
    const fake = join(tmpDir, 'python.exe')
    writeFileSync(fake, '')

    process.env.CKUN_PYTHON = fake
    const r = await resolve()
    expect(r).toEqual({ cmd: fake, prefixArgs: [] })
    expect(execFileMock).not.toHaveBeenCalled()
  })
})
