/**
 * shortcut-win：tar 形态自建 .lnk/AUMID 快捷方式的契约。
 * 纯函数（quote/路径/计划/脚本拼装）逐条断言返回值；ensureShellShortcuts 断言
 * 跳过条件、健康快路径零调用、指纹失配（解包目录被移动）唤起 PS、失败仅 warn、
 * 成功后回写桌面标记与 exe 指纹。
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

const ctl = vi.hoisted(() => ({
  packaged: true,
  appData: 'C:\\Users\\t\\AppData\\Roaming',
  userData: 'C:\\Users\\t\\AppData\\Roaming\\ckun-agent-ssh',
  exe: 'D:\\app\\cKunAgentSSH.exe',
  cachedExe: 'D:\\app\\cKunAgentSSH.exe',
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
const writeMock = vi.hoisted(() => vi.fn(async (_p: string, _d: string, _e?: string): Promise<void> => {}))
const readFileMock = vi.hoisted(() => vi.fn(async (_p: string, _e?: string): Promise<string> => ctl.cachedExe))
vi.mock('fs/promises', async (orig) => {
  const actual = await orig<typeof import('fs/promises')>()
  return { ...actual, writeFile: writeMock, readFile: readFileMock }
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

/** existsSync 侧的健康/缺失口径：三个 userData 产物 + 开始菜单 .lnk */
function setExists(opts: { lnk: boolean; marker: boolean; cache: boolean }) {
  existsMock.mockImplementation((p: string) => {
    const s = String(p)
    if (s.endsWith('.lnk')) return opts.lnk
    if (s.endsWith('.desktop-shortcut-created')) return opts.marker
    if (s.endsWith('.shortcuts-built-for')) return opts.cache
    return true
  })
}

let warnSpy: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'log').mockImplementation(() => {})
  ctl.cachedExe = ctl.exe
})
afterEach(() => {
  vi.restoreAllMocks()
  execFileMock.mockReset()
  existsMock.mockReset()
  existsMock.mockReturnValue(true)
  writeMock.mockClear()
  readFileMock.mockClear()
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
  it('planShortcuts：指纹匹配且 .lnk 在位 → 零 PS；任一失配 → PS 校验', () => {
    expect(planShortcuts({ startMenuExists: true, desktopMarkerExists: true, exeCacheMatches: true }))
      .toEqual({ needsPs: false, desktopCreateIfMissing: false })
    // 解包目录被移动：exe 路径变化 → 指纹失配，必须唤起 PS
    expect(planShortcuts({ startMenuExists: true, desktopMarkerExists: true, exeCacheMatches: false }))
      .toEqual({ needsPs: true, desktopCreateIfMissing: false })
    // 开始菜单项被用户删掉：每次启动补建（原语义保留）
    expect(planShortcuts({ startMenuExists: false, desktopMarkerExists: true, exeCacheMatches: true }))
      .toEqual({ needsPs: true, desktopCreateIfMissing: false })
    // 无 marker（真首启/标记写入曾失败）：桌面缺失要新建
    expect(planShortcuts({ startMenuExists: true, desktopMarkerExists: false, exeCacheMatches: false }))
      .toEqual({ needsPs: true, desktopCreateIfMissing: true })
  })
})

describe('buildShortcutScript', () => {
  it('含 WScript.Shell、PKEY GUID、AUMID 字面量、Desktop/StartMenu 解析与 exe 转义', () => {
    const s = buildShortcutScript({ needsPs: true, desktopCreateIfMissing: true }, 'D:\\app\\cKunAgentSSH.exe', 'com.ckun.agent-ssh')
    expect(s).toContain('WScript.Shell')
    expect(s).toContain('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3') // PKEY_AppUserModel_ID fmtid
    expect(s).toContain('com.ckun.agent-ssh')
    expect(s).toContain("GetFolderPath('Desktop')")
    expect(s).toContain("GetFolderPath('StartMenu')")
    expect(s).toContain("'D:\\app\\cKunAgentSSH.exe'") // execPath 经单引号转义入脚本（反斜杠不改写）
    expect(s).toContain('SHGetPropertyStoreFromParsingName')
    expect(s).toContain('$ErrorActionPreference')
  })
  it('漂移判定在前、Add-Type 只在 pending 非空时执行（健康路径不付 C# 编译开销）', () => {
    const s = buildShortcutScript({ needsPs: true, desktopCreateIfMissing: true }, 'D:\\a.exe', 'x.y')
    expect(s).toContain('-ne $exe') // TargetPath 与当前 exe 比对
    expect(s).toContain('if ($pending.Count -gt 0)')
    expect(s.indexOf('$pending = @()')).toBeLessThan(s.indexOf('Add-Type'))
  })
  it('repair-only 桌面段：缺失不新建（尊重用户删除），只修漂移', () => {
    const s = buildShortcutScript({ needsPs: true, desktopCreateIfMissing: false }, 'D:\\a.exe', 'x.y')
    expect(s).toContain('$dtCreateIfMissing = $false')
    expect(s).toContain('elseif ($dtCreateIfMissing)')
  })
  it('首建段：桌面缺失要新建', () => {
    const s = buildShortcutScript({ needsPs: true, desktopCreateIfMissing: true }, 'D:\\a.exe', 'x.y')
    expect(s).toContain('$dtCreateIfMissing = $true')
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
  it('健康快路径：.lnk 在位 + 标记在位 + 指纹匹配 → 零 PS 调用（幂等）', async () => {
    setPlatform('win32')
    setExists({ lnk: true, marker: true, cache: true })
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
    expect(writeMock).not.toHaveBeenCalled()
  })
  it('exe 指纹失配（解包目录被移动）→ 唤起 PS 漂移校验，成功后回写指纹、不重写桌面标记', async () => {
    setPlatform('win32')
    setExists({ lnk: true, marker: true, cache: true })
    ctl.cachedExe = 'D:\\old-place\\cKunAgentSSH.exe'
    execFileMock.mockImplementation((_c: string, _a: string[], _o: unknown, cb: (e: Error | null) => void) => {
      process.nextTick(() => cb(null))
    })
    await ensureShellShortcuts()
    expect(execFileMock).toHaveBeenCalledTimes(1)
    const script = (execFileMock.mock.calls[0][1] as string[]).at(-1) as string
    expect(script).toContain('-ne $exe')
    expect(writeMock).toHaveBeenCalledTimes(1)
    expect(String(writeMock.mock.calls[0][0]).endsWith('.shortcuts-built-for')).toBe(true)
    expect(writeMock.mock.calls[0][1]).toBe(ctl.exe)
  })
  it('PS 失败仅 warn 不抛（快捷方式建不出来不影响应用本体），标记与指纹都不写 → 下次启动重试', async () => {
    setPlatform('win32')
    setExists({ lnk: false, marker: false, cache: false })
    execFileMock.mockImplementation((_c: string, _a: string[], _o: unknown, cb: (e: Error) => void) => {
      process.nextTick(() => cb(new Error('ConstrainedLanguage 下 Add-Type 失败')))
    })
    await expect(ensureShellShortcuts()).resolves.toBeUndefined()
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('快捷方式创建失败'), expect.anything())
    expect(writeMock).not.toHaveBeenCalled()
  })
  it('首启成功：一次 PS 调用（含 execPath 的脚本）且回写桌面标记与 exe 指纹', async () => {
    setPlatform('win32')
    setExists({ lnk: false, marker: false, cache: false })
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
    expect(writeMock).toHaveBeenCalledTimes(2)
    const writtenPaths = writeMock.mock.calls.map((c) => String(c[0]))
    expect(writtenPaths.some((p) => p.endsWith('.desktop-shortcut-created'))).toBe(true)
    expect(writtenPaths.some((p) => p.endsWith('.shortcuts-built-for'))).toBe(true)
    expect(writeMock.mock.calls.find((c) => String(c[0]).endsWith('.shortcuts-built-for'))![1]).toBe(ctl.exe)
  })
})
