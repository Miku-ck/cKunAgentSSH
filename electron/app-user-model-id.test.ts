/**
 * AppUserModelID 与 electron-builder.yml 的 appId 必须同值，否则漂移是静默的：
 * dev 走 desktop-prefs 里的常量，打包版走 electron-builder 写进快捷方式的 appId。
 * 两边一旦分叉，Windows 把通知归到另一个身份 → Toast 不出现且没有任何报错，
 * 表现为"定时任务跑完了没提醒"，没人会往安装包配置上去查。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { APP_USER_MODEL_ID } from './desktop-prefs'

vi.mock('electron', () => ({
  app: {
    getPath: () => '/fake/userData',
    getAppPath: () => '/fake/app',
    setAppUserModelId: () => undefined,
  },
  BrowserWindow: class {},
  Menu: { buildFromTemplate: (t: unknown) => t },
  Notification: class { static isSupported() { return true } },
  Tray: class {},
  nativeImage: { createFromPath: () => ({ isEmpty: () => true }) },
}))

const builderYml = readFileSync(
  new URL('../electron-builder.yml', import.meta.url),
  'utf-8',
).replace(/\r\n/g, '\n')

describe('桌面通知身份与打包 appId 同源', () => {
  it('electron-builder.yml 的 appId 等于 APP_USER_MODEL_ID', () => {
    const declared = /^appId:\s*(\S+)\s*$/m.exec(builderYml)
    expect(declared, 'electron-builder.yml 里读不到 appId 行').not.toBeNull()
    expect(declared![1]).toBe(APP_USER_MODEL_ID)
  })
})
