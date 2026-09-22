/**
 * desktop-prefs.ts — 桌面偏好的契约单一实现源（主进程与渲染进程共享）
 *
 * 约束：本文件必须保持平台无关 — 不得 import electron / node 内置模块。
 * 生效动作（登录项、托盘、通知）在 electron/desktop-prefs.ts；这里只定义
 * "有哪些字段、默认值、怎样判定合法值"，避免两端各抄一份而漂开。
 */

export interface DesktopPrefs {
  /** 开机自启（Windows/macOS 支持；Linux 主进程会拒绝开启） */
  autoLaunch: boolean
  /** 点关闭按钮隐藏到托盘而不是退出。开启时必须有可用托盘图标，否则窗口关不掉也回不来 */
  minimizeToTray: boolean
  /** 桌面通知总开关（定时任务结束/等待审批等后台事件） */
  desktopNotifications: boolean
}

/**
 * 默认值 = 引入本设置之前的实际行为：不自启、关窗即退出。
 * 通知默认开：它只在窗口未聚焦时出现，且各操作系统仍可在全局设置里单独关掉。
 */
export const DEFAULT_DESKTOP_PREFS: DesktopPrefs = {
  autoLaunch: false,
  minimizeToTray: false,
  desktopNotifications: true,
}

export const DESKTOP_PREF_KEYS = ['autoLaunch', 'minimizeToTray', 'desktopNotifications'] as const

/**
 * 磁盘/IPC 传入值 → 完整偏好。
 * 只认真布尔：手改过的 json 里塞 'no'/1 时，`??` 会原样放行，
 * 于是 truthy 的 'no' 会让用户以为通知关了、实际还开着。
 */
export function normalizeDesktopPrefs(raw: unknown): DesktopPrefs {
  const src = (raw && typeof raw === 'object') ? raw as Record<string, unknown> : {}
  const out: DesktopPrefs = { ...DEFAULT_DESKTOP_PREFS }
  for (const key of DESKTOP_PREF_KEYS) {
    if (typeof src[key] === 'boolean') out[key] = src[key] as boolean
  }
  return out
}

/** 渲染进程的 patch：丢掉未知键与非布尔值；空 patch 返回 null（调用方据此拒绝写入） */
export function prefsPatchOrNull(raw: unknown): Partial<DesktopPrefs> | null {
  const src = (raw && typeof raw === 'object') ? raw as Record<string, unknown> : null
  if (!src) return null
  const patch: Partial<DesktopPrefs> = {}
  for (const key of DESKTOP_PREF_KEYS) {
    if (typeof src[key] === 'boolean') patch[key] = src[key] as boolean
  }
  return Object.keys(patch).length ? patch : null
}
