/**
 * 关键故障告知守卫：钉住「立即明确告知 + 不成为新崩溃源 + 一次性去重」三条契约。
 *
 * 沿用 electron/*.test.ts 同族的源码文本断言（不启动 Electron）验接入点；
 * 去重与 dialog 调用形态用 vi.mock('electron') 做行为断言。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const mainSrc = readFileSync(new URL('../main.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

// dialog 桩：showMessageBox 返回可 await 的 Promise，便于断言调用参数。
// 显式声明单参数签名，避免 vi.fn 推断成零参导致 spread / 下标访问类型报错。
const showMessageBox = vi.fn((_opts: Record<string, unknown>) => Promise.resolve({ response: 0 }))
vi.mock('electron', () => ({
  dialog: { showMessageBox: (opts: Record<string, unknown>) => showMessageBox(opts) },
}))

describe('关键故障告知接入点（源码守卫）', () => {
  it('main.ts 三处告知接入齐全：uncaught / onStatusChange error / start catch', () => {
    expect(mainSrc).toContain("announceCriticalFault('uncaught'")
    expect(mainSrc).toContain("announceCriticalFault('backend-fatal'")
    // onStatusChange 里 ready 复位，保证下一周期可重新弹
    expect(mainSrc).toContain("resetCriticalFaultEpisode()")
  })

  it('致命退出分支不弹窗（避免退出时挂模态框拖死进程）', () => {
    const i = mainSrc.indexOf('uncaughtTimes.length >= 3')
    const branch = mainSrc.slice(i, i + 200)
    expect(branch).not.toContain('announceCriticalFault')
    expect(branch).toContain('app.quit()')
  })
})

describe('announceCriticalFault 行为', () => {
  beforeEach(async () => {
    showMessageBox.mockClear()
    const { resetCriticalFaultEpisode } = await import('./critical-fault')
    resetCriticalFaultEpisode()
  })

  it('backend-fatal 同周期只弹一次，ready 复位后可再弹', async () => {
    const { announceCriticalFault, resetCriticalFaultEpisode } = await import('./critical-fault')
    expect(announceCriticalFault('backend-fatal', '后端服务不可用', 'x')).toBe(true)
    expect(announceCriticalFault('backend-fatal', '后端服务不可用', 'y')).toBe(false)
    expect(showMessageBox).toHaveBeenCalledTimes(1)
    resetCriticalFaultEpisode()
    expect(announceCriticalFault('backend-fatal', '后端服务不可用', 'z')).toBe(true)
    expect(showMessageBox).toHaveBeenCalledTimes(2)
  })

  it('对话框不传 parentWindow（故障时刻窗口可能无响应）', async () => {
    const { announceCriticalFault } = await import('./critical-fault')
    announceCriticalFault('backend-fatal', '标题', '详情')
    const opts = showMessageBox.mock.calls[0][0] as Record<string, unknown>
    expect(opts.type).toBe('error')
    expect(opts).not.toHaveProperty('parent')
    expect(opts).not.toHaveProperty('browserWindow')
  })

  it('uncaught 60s 内 cooldown，不重复弹', async () => {
    const { announceCriticalFault } = await import('./critical-fault')
    expect(announceCriticalFault('uncaught', '异常', 'a')).toBe(true)
    expect(announceCriticalFault('uncaught', '异常', 'b')).toBe(false)
    expect(showMessageBox).toHaveBeenCalledTimes(1)
  })
})
