/**
 * dev 冷启动「先亮窗、后台探活」契约守卫。
 *
 * 背景：改动前 createWindow 里 `await waitForDevUrl()` 把冷盘 15s 探活全挡在
 * 窗口显示之前，ready-to-show 又要等 loadURL，用户点开图标后十几秒空窗。
 * 现在改为：先 loadURL(data:loading 页) 亮窗 → 后台并行探活 → 完成后 loadURL 真界面。
 *
 * 本文件用源码文本断言钉住顺序（与 main-gpu-switches.test.ts 同族，不启动 Electron）：
 * 阻止未来有人把探活挪回阻塞位置、或删掉 loading 页导致空窗回归。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const mainSrc = readFileSync(new URL('./main.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

describe('dev 启动先亮 loading 窗、探活后台并行', () => {
  it('导出 devLoadingUrl 且用 data: URL（零磁盘/网络 IO）', () => {
    expect(mainSrc).toMatch(/export function devLoadingUrl\(\): string/)
    const fnBody = mainSrc.slice(
      mainSrc.indexOf('export function devLoadingUrl'),
      mainSrc.indexOf('\n}', mainSrc.indexOf('export function devLoadingUrl')),
    )
    expect(fnBody).toContain('data:text/html')
    expect(fnBody).toContain('正在启动')
  })

  it('loading 页 loadURL 出现在探活 await 之前（顺序即体验）', () => {
    const iLoading = mainSrc.indexOf('loadURL(devLoadingUrl())')
    const iProbe = mainSrc.indexOf('await waitForDevUrl(devUrl)')
    expect(iLoading, '必须有 loading 页加载').toBeGreaterThan(-1)
    expect(iProbe, '必须有探活').toBeGreaterThan(-1)
    expect(iLoading, 'loading 页必须先于探活调用，否则空窗回归').toBeLessThan(iProbe)
  })

  it('探活在 void 异步块里（不阻塞 whenReady 主链）', () => {
    const iProbe = mainSrc.indexOf('await waitForDevUrl(devUrl)')
    const window = mainSrc.slice(iProbe - 120, iProbe)
    expect(window).toContain('void (async () => {'), '探活必须在 fire-and-forget 的异步块中'
  })

  it('探活完成后仍 loadURL 真界面，保留防抢跑语义', () => {
    const iProbe = mainSrc.indexOf('await waitForDevUrl(devUrl)')
    const after = mainSrc.slice(iProbe, iProbe + 500)
    expect(after).toContain('loadURL(devUrl)')
    expect(after).toContain('perfMark(\'dev server probe done\')')
  })

  it('生产分支仍走 loadFile，不受 dev 改动影响', () => {
    expect(mainSrc).toMatch(/mainWindow\.loadFile\(join\(__dirname, '\.\.\/renderer\/index\.html'\)\)/)
  })
})

describe('perf 日志：默认一行汇总、明细需 CKUN_PERF=1（对齐后端）', () => {
  const perfSrc = readFileSync(new URL('./utils/perf-mark.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

  it('perfMark 保留签名，明细受 CKUN_PERF 开关控制', () => {
    expect(perfSrc).toMatch(/export function perfMark\(name: string\): void/)
    expect(perfSrc).toContain("process.env.CKUN_PERF === '1'")
    expect(perfSrc).toContain('if (VERBOSE) console.log')
  })

  it('导出 logPerfSummary 且 main.ts 在启动编排结束后调用一次', () => {
    expect(perfSrc).toMatch(/export function logPerfSummary\(\): void/)
    expect(mainSrc).toContain('logPerfSummary()')
    expect(mainSrc).toContain('import { perfMark, logPerfSummary }')
  })
})
