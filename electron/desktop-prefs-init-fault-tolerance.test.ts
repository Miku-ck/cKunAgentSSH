/**
 * 启动回填的容错性 —— initDesktopPrefs 的 docstring 承诺"任何一步失败都不该让应用起不来"。
 *
 * 实测两处兑现不了，而且都是"设置说开了、系统里什么都没发生"的形状：
 * - `ensureTray()` 是整段里唯一没有 try 的一步。图标文件在位但 `new Tray()` 抛错是
 *   真实场景（Linux 没有 appindicator 宿主时 Electron 直接抛），应用会在注册 IPC 之前
 *   崩掉 —— 与 docstring 正相反。
 * - Linux 的 `applyAutoLaunch` 按设计必然抛错，所以回填跳过它；但跳过之后落盘的
 *   `autoLaunch: true` 原样留着（换机器/同步过来的库文件都会带这种陈旧值），
 *   设置页显示"开机自启 已开"，系统里没有任何启动项。
 * 同文件下面那条"托盘图标缺失"分支早就给出了正确形态：不一致就关掉并写回。
 * 这次是把同一处理也用到上面两步，并抽成 disablePref 一处实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const env = vi.hoisted(() => ({
  platform: 'linux',
  prefsFile: '',
  tmpData: '',
  iconPresent: true,
  trayThrows: false,
  loginItemCalls: [] as Array<{ openAtLogin?: boolean }>,
}))

vi.mock('electron', () => ({
  app: {
    getPath: () => '/fake/userData',
    getAppPath: () => '/fake/app',
    setAppUserModelId: () => undefined,
    setLoginItemSettings: (o: { openAtLogin?: boolean }) => env.loginItemCalls.push(o),
  },
  BrowserWindow: class {},
  Menu: { buildFromTemplate: (t: unknown) => t },
  Notification: class { static isSupported() { return true } },
  Tray: class {
    constructor() {
      if (env.trayThrows) throw new Error('libappindicator is not installed')
    }
    setToolTip(): void {}
    setContextMenu(): void {}
    on(): void {}
    destroy(): void {}
  },
  nativeImage: {
    createFromPath: () => ({ isEmpty: () => !env.iconPresent }),
  },
}))

vi.mock('fs', () => ({
  existsSync: (p: string) =>
    String(p).endsWith('desktop-prefs.json') ? env.prefsFile !== '' : env.iconPresent,
}))

vi.mock('fs/promises', () => ({
  mkdir: async () => undefined,
  readFile: async () => env.prefsFile,
  writeFile: async (_p: string, data: string) => { env.tmpData = data },
  rename: async () => { env.prefsFile = env.tmpData },
}))

const STORED = JSON.stringify({
  autoLaunch: true,
  minimizeToTray: true,
  notifications: true,
})

/** 模块级 prefs/tray 是进程内单例状态，每个用例都要重新装载模块 */
async function load() {
  vi.resetModules()
  Object.defineProperty(process, 'platform', { value: env.platform, configurable: true })
  const mod = await import('./desktop-prefs')
  return mod
}

beforeEach(() => {
  env.platform = 'linux'
  env.prefsFile = STORED
  env.tmpData = ''
  env.iconPresent = true
  env.trayThrows = false
  env.loginItemCalls = []
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('initDesktopPrefs 的容错与陈旧值归一', () => {
  it('Tray 构造抛错时不崩启动，并把该项关掉写回', async () => {
    env.platform = 'linux'
    env.trayThrows = true
    const mod = await load()
    const out = await mod.initDesktopPrefs(() => null)
    expect(out.minimizeToTray).toBe(false)
    expect(JSON.parse(env.prefsFile).minimizeToTray).toBe(false)  // 关闭后必须写回磁盘
  })

  it('Linux 上陈旧的 autoLaunch 被归零，而不是显示"已开"却什么都没做', async () => {
    env.platform = 'linux'
    const mod = await load()
    const out = await mod.initDesktopPrefs(() => null)
    expect(out.autoLaunch).toBe(false)
    expect(JSON.parse(env.prefsFile).autoLaunch).toBe(false)
    expect(env.loginItemCalls).toEqual([])  // Linux 上不该调用 setLoginItemSettings
  })

  it('反向锁：Windows 上 autoLaunch 照旧回填为开机项，值保持开', async () => {
    env.platform = 'win32'
    const mod = await load()
    const out = await mod.initDesktopPrefs(() => null)
    expect(out.autoLaunch).toBe(true)
    expect(env.loginItemCalls).toEqual([{ openAtLogin: true }])
    expect(JSON.parse(env.prefsFile).autoLaunch).toBe(true)
  })

  it('反向锁：托盘建得起来时 minimizeToTray 不被误关', async () => {
    env.platform = 'win32'
    const mod = await load()
    const out = await mod.initDesktopPrefs(() => null)
    expect(out.minimizeToTray).toBe(true)
  })

  it('反向锁：图标缺失仍按原口径关闭（这次改动没把那条分支换坏）', async () => {
    env.platform = 'win32'
    env.iconPresent = false
    const mod = await load()
    const out = await mod.initDesktopPrefs(() => null)
    expect(out.minimizeToTray).toBe(false)
  })
})
