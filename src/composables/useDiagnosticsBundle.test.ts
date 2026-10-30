/**
 * useDiagnosticsBundle — 诊断包导出的渲染侧契约。
 *
 * 最要紧的两条：浏览器模式下不能假装导出成功；主进程回了 ok=false 时，用户必须
 * 看到那句话（失败信息里带着保留下来的临时目录，那是唯一还能救回现场的线索）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const g = globalThis as unknown as Record<string, unknown>

const electronMock = {
  diagExport: vi.fn(),
  diagReveal: vi.fn(),
}
const mockToast = { error: vi.fn(), warning: vi.fn(), success: vi.fn() }

vi.mock('@/stores/toast', () => ({ useToastStore: () => mockToast }))

const { useDiagnosticsBundle } = await import('@/composables/useDiagnosticsBundle')

const okResult = {
  ok: true,
  path: 'C:\\Users\\u\\AppData\\Roaming\\cKunAgentSSH\\diagnostics\\ckunagentssh-diag-0.1.0-20261030-120509.tar.gz',
  bytes: 2048,
  logs: [{ name: 'main.log', bytes: 1024 }],
  crashDumps: 2,
  error: null,
}

describe('useDiagnosticsBundle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    for (const fn of Object.values(electronMock)) fn.mockReset()
    for (const fn of Object.values(mockToast)) fn.mockReset()
    g.window = { electronAPI: electronMock }
    electronMock.diagExport.mockResolvedValue({ ...okResult })
    electronMock.diagReveal.mockResolvedValue(undefined)
  })

  it('导出成功：结果落进 result，提示一次', async () => {
    const s = useDiagnosticsBundle()
    expect(s.supported.value).toBe(true)
    await s.exportBundle()
    expect(electronMock.diagExport).toHaveBeenCalledTimes(1)
    expect(s.result.value?.path).toContain('ckunagentssh-diag-')
    expect(s.error.value).toBe('')
    expect(mockToast.success).toHaveBeenCalledTimes(1)
  })

  it('主进程回 ok=false：展示它给的错误（含保留的临时目录），不当成功', async () => {
    electronMock.diagExport.mockResolvedValueOnce({
      ok: false,
      path: null,
      bytes: 0,
      logs: [],
      crashDumps: 0,
      error: 'tar 退出码 1（未完成的内容留在 …\\.staging-1-2）',
    })
    const s = useDiagnosticsBundle()
    await s.exportBundle()
    expect(s.error.value).toContain('tar 退出码 1')
    expect(s.error.value).toContain('.staging-')
    expect(mockToast.success).not.toHaveBeenCalled()
    expect(mockToast.error).toHaveBeenCalledTimes(1)
  })

  it('IPC 抛错（主进程归一化的 message）同样落地并出声', async () => {
    electronMock.diagExport.mockRejectedValueOnce(new Error('userData 不可写'))
    const s = useDiagnosticsBundle()
    await s.exportBundle()
    expect(s.error.value).toBe('userData 不可写')
    expect(mockToast.error).toHaveBeenCalledTimes(1)
  })

  it('导出进行中忽略第二次点击（两份包说不清该用哪份）', async () => {
    let release: (v: unknown) => void = () => {}
    electronMock.diagExport.mockImplementationOnce(() => new Promise((res) => { release = res }))
    const s = useDiagnosticsBundle()
    const first = s.exportBundle()
    expect(s.exporting.value).toBe(true)
    await s.exportBundle()
    expect(electronMock.diagExport).toHaveBeenCalledTimes(1)
    release({ ...okResult })
    await first
    expect(s.exporting.value).toBe(false)
  })

  it('浏览器模式：不发 IPC，置灰并提示', async () => {
    g.window = {}
    const s = useDiagnosticsBundle()
    expect(s.supported.value).toBe(false)
    await s.exportBundle()
    await s.reveal()
    expect(electronMock.diagExport).not.toHaveBeenCalled()
    expect(electronMock.diagReveal).not.toHaveBeenCalled()
    expect(mockToast.warning).toHaveBeenCalledTimes(1)
  })

  it('reveal 被主进程拒绝时出声（不静默吞掉）', async () => {
    electronMock.diagReveal.mockRejectedValueOnce(new Error('还没有导出过诊断包'))
    const s = useDiagnosticsBundle()
    await s.reveal()
    expect(mockToast.error).toHaveBeenCalledTimes(1)
  })
})
