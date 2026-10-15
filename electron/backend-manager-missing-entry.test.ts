/**
 * 后端入口缺失必须报"入口不存在"，不许转成"没装 Python"。
 *
 * 原实现把 spawn 的 ENOENT 一律翻译成「未检测到 Python 运行环境」，而 Node 对
 * 「cwd 不存在」报的同样是 ENOENT 且 err.path 就是解释器名（实测：
 * `spawn('node',['-v'],{cwd:'<不存在的目录>'})` → code=ENOENT / syscall=spawn node /
 * path=node）。于是 backend 目录缺失时日志打出"Using Python interpreter: python"
 * 紧跟"spawn python ENOENT"，排查方向被整个带偏 —— 这条链路为此烧过两次提交。
 *
 * 现在 spawn 前先验 main.py 存在；顺带把"预 spawn 失败也要唤醒 whenReady 等待者"
 * 补上，否则渲染层的 backend:whenReady 只能干等 15s 超时拿到一句无信息量的"启动超时"。
 */
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

let appDir = ''

vi.mock('electron', () => ({
  app: {
    getPath: () => appDir,
    getAppPath: () => appDir,
    isPackaged: false,
  },
}))

vi.mock('child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('child_process')>()
  return { ...actual, spawn: vi.fn(), execFile: vi.fn() }
})

import { spawn, execFile } from 'child_process'
import { BackendManager } from './backend-manager'

const spawnMock = spawn as unknown as Mock
const execFileMock = execFile as unknown as Mock

let backendDir = ''

/** 解释器探测桩：全部失败（等价于机器上没有可用 python），用于验证"过了入口校验才轮到探测" */
const stubProbesFailing = () => {
  execFileMock.mockImplementation(
    (_cmd: string, _args: unknown[], _opts: unknown, cb: (e: Error | null) => void) => {
      process.nextTick(() => cb(new Error('no python')))
    },
  )
}

beforeEach(() => {
  appDir = mkdtempSync(join(tmpdir(), 'ckun-bm-entry-'))
  backendDir = join(appDir, 'backend')
  mkdirSync(backendDir, { recursive: true })
  spawnMock.mockReset()
  execFileMock.mockReset()
  stubProbesFailing()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  rmSync(appDir, { recursive: true, force: true })
  vi.restoreAllMocks()
})

describe('后端入口校验先于解释器探测与 spawn', () => {
  it('缺 main.py：报入口不存在，且绝不 spawn', async () => {
    const bm = new BackendManager()

    await expect(bm.start()).rejects.toThrow(/后端入口文件不存在/)
    expect(spawnMock, '入口都不存在，不该再拉起解释器').not.toHaveBeenCalled()
  })

  it('缺 main.py：whenReady 的等待者立刻拿到真实原因，而不是 15s 超时', async () => {
    const bm = new BackendManager()
    const waiting = bm.whenReady(8000)
    const starting = bm.start()

    await expect(starting).rejects.toThrow(/后端入口文件不存在/)
    await expect(waiting).rejects.toThrow(/后端入口文件不存在/)
  })

  it('main.py 在位：放行到解释器解析，报的是 Python 环境缺失而非入口缺失', async () => {
    writeFileSync(join(backendDir, 'main.py'), '# stub entry', 'utf-8')
    const bm = new BackendManager()

    await expect(bm.start()).rejects.toThrow(/未找到可运行后端的 Python 环境/)
    expect(spawnMock).not.toHaveBeenCalled()
  })

  it('校验消息带上实际路径与启动形态，便于一次定位', async () => {
    const bm = new BackendManager()
    const err = await bm.start().catch((e: Error) => e)
    const message = (err as Error).message

    expect(message).toContain(join(backendDir, 'main.py'))
    expect(message).toContain('isPackaged=false')
  })
})
