/**
 * useDesktopPrefs — 桌面偏好（G8）的渲染侧契约。
 *
 * 面板上的开关看起来"点了就生效"，实际要过主进程一关（托盘图标缺失、Linux 不
 * 支持登录项都会拒绝）。所以这里最要紧的是两条：失败必须回滚并出声，浏览器模式
 * 下不能假装写成功。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const g = globalThis as unknown as Record<string, unknown>

const electronMock = {
  getDesktopPrefs: vi.fn(),
  setDesktopPrefs: vi.fn(),
}
const mockToast = { error: vi.fn(), warning: vi.fn(), success: vi.fn() }

vi.mock('@/stores/toast', () => ({ useToastStore: () => mockToast }))

const { useDesktopPrefs } = await import('@/composables/useDesktopPrefs')

const allOn = { autoLaunch: true, minimizeToTray: true, desktopNotifications: true }

describe('useDesktopPrefs', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    for (const fn of Object.values(electronMock)) fn.mockReset()
    for (const fn of Object.values(mockToast)) fn.mockReset()
    g.window = { electronAPI: electronMock }
    electronMock.getDesktopPrefs.mockResolvedValue({ ...allOn })
    electronMock.setDesktopPrefs.mockImplementation(async (patch: Record<string, unknown>) => ({
      ...allOn,
      ...patch,
    }))
  })

  it('load 读回主进程的权威值', async () => {
    const s = useDesktopPrefs()
    await s.load()
    expect(electronMock.getDesktopPrefs).toHaveBeenCalledTimes(1)
    expect(s.prefs.value).toEqual(allOn)
    expect(s.error.value).toBe('')
  })

  it('非布尔的脏值不采信，回落到默认而不是"看起来开着"', async () => {
    electronMock.getDesktopPrefs.mockResolvedValue({
      autoLaunch: 'yes', minimizeToTray: 0, desktopNotifications: true,
    })
    const s = useDesktopPrefs()
    await s.load()
    expect(s.prefs.value.autoLaunch).toBe(false)
    expect(s.prefs.value.minimizeToTray).toBe(false)
    expect(s.prefs.value.desktopNotifications).toBe(true)
  })

  it('toggle 只提交单键 patch，并用主进程回包覆盖本地', async () => {
    const s = useDesktopPrefs()
    await s.load()
    await s.toggle('autoLaunch')
    expect(electronMock.setDesktopPrefs).toHaveBeenCalledWith({ autoLaunch: false })
    expect(s.prefs.value.autoLaunch).toBe(false)
  })

  it('主进程拒绝时回滚开关并出声', async () => {
    electronMock.setDesktopPrefs.mockRejectedValueOnce(
      new Error('托盘图标资源缺失（resources/icon.png），无法启用关窗最小化'),
    )
    const s = useDesktopPrefs()
    await s.load()
    await s.toggle('minimizeToTray')
    expect(s.prefs.value.minimizeToTray).toBe(true)
    expect(s.error.value).toContain('托盘图标资源缺失')
    expect(mockToast.error).toHaveBeenCalledTimes(1)
  })

  it('读取失败保留当前显示值，不把开关重置成默认', async () => {
    const s = useDesktopPrefs()
    await s.load()
    electronMock.getDesktopPrefs.mockRejectedValueOnce(new Error('ipc 未就绪'))
    await s.load()
    expect(s.prefs.value).toEqual(allOn)
    expect(s.error.value).toBe('ipc 未就绪')
  })

  it('浏览器模式：不发 IPC，提示仅在桌面应用内可改', async () => {
    g.window = {}
    const s = useDesktopPrefs()
    await s.load()
    await s.toggle('autoLaunch')
    expect(electronMock.setDesktopPrefs).not.toHaveBeenCalled()
    expect(s.prefs.value.autoLaunch).toBe(false)
    expect(mockToast.warning).toHaveBeenCalledTimes(1)
  })

  it('写入未回话时忽略第二次点击，不排队互相覆盖', async () => {
    let release: (v: unknown) => void = () => {}
    electronMock.setDesktopPrefs.mockImplementationOnce(
      () => new Promise((res) => { release = res }),
    )
    const s = useDesktopPrefs()
    await s.load()
    const first = s.toggle('autoLaunch')
    expect(s.pending.value).toBe('autoLaunch')
    await s.toggle('desktopNotifications')
    expect(electronMock.setDesktopPrefs).toHaveBeenCalledTimes(1)
    release({ ...allOn, autoLaunch: false })
    await first
    expect(s.pending.value).toBeNull()
    expect(s.prefs.value.desktopNotifications).toBe(true)
  })
})
