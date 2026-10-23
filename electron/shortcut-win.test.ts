/**
 * shortcut-win：tar 形态首启自建 .lnk/AUMID 快捷方式的契约。
 * 纯函数（quote/路径/计划）逐条断言返回值；ensureShellShortcuts 断言
 * 跳过条件、幂等零调用、失败仅 warn、成功后写桌面标记。
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

const ctl = vi.hoisted(() => ({
  packaged: true,
  appData: 'C:\\Users\\t\\AppData\\Roaming',
  userData: 'C:\\Users\\t\\AppData\\Roaming\\ckun-agent-ssh',
  exe: 'D:\\app\\cKunAgentSSH.exe',
}))

vi.mock('electron', () => ({
  app: {
    get isPackaged() { return ctl.packaged },
    getPath: (k: string) => (k === 'appData' ? ctl.appData : k === 'userData' ? ctl.userData : k === 'exe' ? ctl.exe : ''),
    setAppUserModelId: () => undefined,
  },
  BrowserWindow: class {},
  Menu: { buildFromTemplate: (t: unknown) => t },
  Notification: class { static isSupported() { return true } },
  Tray: class {},
  nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
}))

const existsMock = vi.hoisted(() => vi.fn((_p: string): boolean => true))
vi.mock('fs', async (orig) => {
  const actual = await orig<typeof import('fs')>()
  return { ...actual, existsSync: existsMock }
})
const writeMarkerMock = vi.hoisted(() => vi.fn(async (_p: string, _d: string, _e?: string): Promise<void> => {}))
vi.mock('fs/promises', async (orig) => {
  const actual = await orig<typeof import('fs/promises')>()
  return { ...actual, writeFile: writeMarkerMock }
})
vi.mock('child_process', async (orig) => {
  const actual = await orig<typeof import('child_process')>()
  return { ...actual, execFile: vi.fn() }
})

import { execFile } from 'child_process'
import {
  buildShortcutScript,
  ensureShellShortcuts,
  planShortcuts,
  shellQuotePs,
  startMenuLnkPath,
} from './shortcut-win'

const execFileMock = execFile as unknown as Mock

function setPlatform(p: string) {
  Object.defineProperty(process, 'platform', { value: p, configurable: true })
}

let warnSpy: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'log').mockImplementation(() => {})
})
afterEach(() => {
  vi.restoreAllMocks()
  execFileMock.mockReset()
  existsMock.mockReset()
  existsMock.mockReturnValue(true)
  writeMarkerMock.mockClear()
  ctl.packaged = true
})

describe('shortcut-win 纯函数', () => {
  it('shellQuotePs：PS 单引号转义（内部单引号翻倍，整体包裹）', () => {
    expect(shellQuotePs("it's")).toBe("'it''s'")
    expect(shellQuotePs('plain')).toBe("'plain'")
  })
  it('startMenuLnkPath：appData 拼 Start Menu\\Programs\\cKunAgentSSH.lnk', () => {
    const p = startMenuLnkPath(ctl.appData)
    expect(p).toContain('Microsoft')
    expect(p).toContain('Start Menu')
    expect(p.endsWith('cKunAgentSSH.lnk')).toBe(true)
  })
  it('planShortcuts：exists→不建 的取反真值表', () => {
    expect(planShortcuts({ startMenuExists: true, desktopMarkerExists: true }))
      .toEqual({ createStartMenu: false, createDesktop: false })
    expect(planShortcuts({ startMenuExists: false, desktopMarkerExists: true }))
      .toEqual({ createStartMenu: true, createDesktop: false })
    expect(planShortcuts({ startMenuExists: true, desktopMarkerExists: false }))
      .toEqual({ createStartMenu: false, createDesktop: true })
  })
})

describe('buildShortcutScript', () => {
  it('全量计划含 WScript.Shell、PKEY GUID、AUMID 字面量与 Desktop/StartMenu 解析', () => {
    const s = buildShortcutScript({ createStartMenu: true, createDesktop: true }, 'D:\\app\\cKunAgentSSH.exe', 'com.ckun.agent-ssh')
    expect(s).toContain('WScript.Shell')
    expect(s).toContain('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3') // PKEY_AppUserModel_ID fmtid
    expect(s).toContain('com.ckun.agent-ssh')
    expect(s).toContain("GetFolderPath('Desktop')")
    expect(s).toContain("GetFolderPath('StartMenu')")
    expect(s).toContain("'D:\\app\\cKunAgentSSH.exe'") // execPath 经单引号转义入脚本（反斜杠不改写）
    expect(s).toContain('SHGetPropertyStoreFromParsingName')
    expect(s).toContain('$ErrorActionPreference')
  })
  it('仅开始菜单计划：不含 Desktop 段（按 plan 拼段，不做多余动作）', () => {
    const s = buildShortcutScript({ createStartMenu: true, createDesktop: false }, 'D:\\a.exe', 'x.y')
    expect(s).toContain("GetFolderPath('StartMenu')")
    expect(s).not.toContain("GetFolderPath('Desktop')")
  })
})

describe('ensureShellShortcuts', () => {
  it('非 win32 跳过：execFile 不触发', async () => {
    setPlatform('linux')
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('非打包态跳过（dev 的 electron.exe 不配建快捷方式）', async () => {
    setPlatform('win32')
    ctl.packaged = false
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('开始菜单项已存在且桌面标记已写 → 零 PS 调用（幂等）', async () => {
    setPlatform('win32')
    existsMock.mockImplementation((p: string) => String(p).endsWith('.lnk') || String(p).endsWith('.desktop-shortcut-created'))
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('PS 失败仅 warn 不抛（快捷方式建不出来不影响应用本体）', async () => {
    setPlatform('win32')
    existsMock.mockReturnValue(false)
    execFileMock.mockImplementation((_c: string, _a: string[], _o: unknown, cb: (e: Error) => void) => {
      process.nextTick(() => cb(new Error('ConstrainedLanguage 下 Add-Type 失败')))
    })
    await expect(ensureShellShortcuts()).resolves.toBeUndefined()
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('快捷方式创建失败'), expect.anything())
    expect(writeMarkerMock).not.toHaveBeenCalled() // 失败不写标记 → 下次启动重试
  })
  it('首启成功：一次 PS 调用（含 execPath 的脚本）且写桌面标记', async () => {
    setPlatform('win32')
    existsMock.mockReturnValue(false)
    execFileMock.mockImplementation((_c: string, _a: string[], _o: unknown, cb: (e: Error | null) => void) => {
      process.nextTick(() => cb(null))
    })
    await ensureShellShortcuts()
    expect(execFileMock).toHaveBeenCalledTimes(1)
    const args = execFileMock.mock.calls[0][1] as string[]
    expect(args).toContain('-ExecutionPolicy')
    const script = args[args.length - 1]
    expect(script).toContain('cKunAgentSSH.exe')
    expect(script).toContain('com.ckun.agent-ssh')
    expect(writeMarkerMock).toHaveBeenCalledTimes(1)
    expect(String(writeMarkerMock.mock.calls[0][0]).endsWith('.desktop-shortcut-created')).toBe(true)
  })
})
