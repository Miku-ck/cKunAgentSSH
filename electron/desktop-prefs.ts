/**
 * desktop-prefs.ts — 桌面偏好（开机自启 / 关窗隐藏到托盘 / 桌面通知）的持久化与生效动作。
 *
 * 字段契约与合法值判定来自 shared/desktop-prefs.ts（渲染进程用同一份），这里只管
 * "读盘 → 应用 → 落盘"。三条约束：
 * 1. 内存里始终留一份已生效值：主窗口 close 事件要同步判定是否隐藏，来不及 await 读盘。
 * 2. 写盘走串行队列：SSHStore 的教训是"读全量→改→写全量"并发会丢更新。
 * 3. 关窗隐藏必须伴随可用托盘：没有托盘图标还把窗口隐藏，应用既关不掉也唤不回。
 */
import { app, BrowserWindow, Menu, Notification, Tray, nativeImage } from 'electron'
import { existsSync } from 'fs'
import { mkdir, readFile, rename, writeFile } from 'fs/promises'
import { dirname, join } from 'path'
import {
  DEFAULT_DESKTOP_PREFS,
  normalizeDesktopPrefs,
  prefsPatchOrNull,
  type DesktopPrefs,
} from '../shared/desktop-prefs'

let prefs: DesktopPrefs = { ...DEFAULT_DESKTOP_PREFS }
let tray: Tray | null = null
let quitting = false
let resolveWindow: (() => BrowserWindow | null) | null = null

function storePath(): string {
  return join(app.getPath('userData'), 'desktop-prefs.json')
}

/** 托盘图标：dev 与打包后的 app 根目录都有 resources/icon.png（buildResources 未被 files 排除） */
function trayIconPath(): string {
  return join(app.getAppPath(), 'resources', 'icon.png')
}

function trayIconAvailable(): boolean {
  const p = trayIconPath()
  return existsSync(p) && !nativeImage.createFromPath(p).isEmpty()
}

/** 关窗时是否应该隐藏而不是退出（无托盘则绝不隐藏，否则窗口唤不回来） */
export function shouldHideOnClose(): boolean {
  return prefs.minimizeToTray && !quitting && tray !== null
}

/** main.ts 的 before-quit 调用：让收尾期间的 close 事件不再走隐藏分支 */
export function markQuitting(): void {
  quitting = true
  destroyTray()
}

export function getDesktopPrefs(): DesktopPrefs {
  return { ...prefs }
}

async function readFromDisk(): Promise<DesktopPrefs> {
  const path = storePath()
  if (!existsSync(path)) return { ...DEFAULT_DESKTOP_PREFS }
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // 读失败不能按"空偏好"继续：随后的写入会把用户三项设置抹平（与 SSHStore E-2 同一形状）
    throw new Error(`读取桌面偏好失败（文件可能被占用）：${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    return normalizeDesktopPrefs(JSON.parse(raw))
  } catch (e) {
    // 损坏才降级：坏文件本就读不出可用值，保留现场交给用户判断
    console.error('[DesktopPrefs] 偏好文件损坏，按默认值处理:', e)
    return { ...DEFAULT_DESKTOP_PREFS }
  }
}

async function writeToDisk(next: DesktopPrefs): Promise<void> {
  const path = storePath()
  await mkdir(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  await writeFile(tmp, JSON.stringify(next, null, 2), 'utf-8')
  try {
    await rename(tmp, path)
  } catch (e) {
    // Windows 上杀软/索引短暂占用目标文件时 rename 会 EPERM：等一次再试，仍失败退回直写
    const code = (e as { code?: string })?.code
    if (code !== 'EPERM' && code !== 'EACCES' && code !== 'EBUSY') throw e
    await new Promise(r => setTimeout(r, 200))
    try {
      await rename(tmp, path)
    } catch {
      await writeFile(path, JSON.stringify(next, null, 2), 'utf-8')
    }
  }
}

let writeQueue: Promise<unknown> = Promise.resolve()
function enqueue<T>(op: () => Promise<T>): Promise<T> {
  const run = writeQueue.then(op, op)
  writeQueue = run.then(() => undefined, () => undefined)
  return run
}

/** 登录项：Linux 上 setLoginItemSettings 是空操作，开着等于骗人，所以直接拒绝 */
function applyAutoLaunch(enabled: boolean): void {
  if (process.platform === 'linux') {
    throw new Error('当前平台不支持开机自启，请在系统设置里添加启动项')
  }
  app.setLoginItemSettings({ openAtLogin: enabled })
}

function showMainWindow(): void {
  const win = resolveWindow?.() ?? null
  if (!win || win.isDestroyed()) return
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

function ensureTray(): void {
  if (tray) return
  const icon = nativeImage.createFromPath(trayIconPath())
  tray = new Tray(icon)
  tray.setToolTip('cKunAgentSSH')
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示主窗口', click: showMainWindow },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit() } },
  ]))
  // Windows 上双击唤起、macOS 上单击唤起，两边都挂才不留"点了没反应"的死角
  tray.on('click', showMainWindow)
  tray.on('double-click', showMainWindow)
}

function destroyTray(): void {
  if (!tray) return
  tray.destroy()
  tray = null
}

/**
 * 启动时回填：读盘 + 应用自启状态 + 需要时建托盘。
 * 任何一步失败都不该让应用起不来，所以整段容错；托盘缺图标时关掉该项而不是静默半生效。
 */
export async function initDesktopPrefs(
  getWindow: () => BrowserWindow | null,
): Promise<DesktopPrefs> {
  resolveWindow = getWindow
  // Windows 上通知按 AppUserModelID 归属；打包版由 electron-builder 从 appId 写进快捷方式，
  // dev（electron-vite dev 直跑 electron.exe）没有这一步，不设则通知被系统静默丢弃。
  if (process.platform === 'win32') {
    try {
      app.setAppUserModelId('com.ckun.agent-ssh')  // 与 electron-builder.yml 的 appId 同值
    } catch (e) {
      console.warn('[DesktopPrefs] setAppUserModelId 失败:', e)
    }
  }
  try {
    prefs = await readFromDisk()
  } catch (e) {
    console.error('[DesktopPrefs] 加载失败，使用默认值:', e)
    prefs = { ...DEFAULT_DESKTOP_PREFS }
  }
  if (prefs.autoLaunch && process.platform !== 'linux') {
    try {
      applyAutoLaunch(true)
    } catch (e) {
      console.error('[DesktopPrefs] 开机自启回填失败:', e)
    }
  }
  if (prefs.minimizeToTray) {
    if (trayIconAvailable()) {
      ensureTray()
    } else {
      // 落盘值与内存值不一致会永久迷惑：这里把该项关掉并写回，UI 也就显示关
      console.warn('[DesktopPrefs] 托盘图标缺失，已关闭"关窗隐藏到托盘"')
      prefs = { ...prefs, minimizeToTray: false }
      await writeToDisk(prefs).catch(() => undefined)
    }
  }
  return getDesktopPrefs()
}

/** 渲染进程 patch → 生效动作 → 落盘。动作失败抛错，由 IPC reject 提示用户（值不落盘） */
export async function setDesktopPrefs(raw: unknown): Promise<DesktopPrefs> {
  const patch = prefsPatchOrNull(raw)
  if (!patch) throw new Error('没有可更新的桌面偏好字段')
  const next = { ...prefs, ...patch }
  if (next.minimizeToTray && !trayIconAvailable()) {
    throw new Error('托盘图标资源缺失（resources/icon.png），无法开启"关窗隐藏到托盘"')
  }
  if ('autoLaunch' in patch && patch.autoLaunch !== prefs.autoLaunch) applyAutoLaunch(next.autoLaunch)
  if ('minimizeToTray' in patch && patch.minimizeToTray !== prefs.minimizeToTray) {
    if (next.minimizeToTray) ensureTray()
    else destroyTray()
  }
  prefs = next
  await enqueue(() => writeToDisk(prefs))
  return getDesktopPrefs()
}

/**
 * 桌面通知：三重闸门 —— 用户开关、窗口是否正在被看、系统是否支持。
 * 聚焦时不弹是给"通知疲劳"留的余地：用户在窗口里，toast 已经看得见，再来一条系统横幅就是噪音。
 */
export function notifyDesktop(title: string, body: string): boolean {
  if (!prefs.desktopNotifications) return false
  const win = resolveWindow?.() ?? null
  if (win && !win.isDestroyed() && win.isFocused() && win.isVisible()) return false
  if (!Notification.isSupported()) return false
  const n = new Notification({
    title: String(title).slice(0, 120),
    body: String(body).slice(0, 240),
  })
  n.on('click', showMainWindow)
  n.show()
  return true
}
