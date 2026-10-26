/**
 * GPU 渲染开关的契约守卫。
 *
 * ⚠️ 这条断言的依据**尚未成立**，别把它读成「已证明这三个开关必须存在」：
 *   - 17:39 有人按 ADR 0001 删掉三个渲染开关，判据「改完必须起一次看到窗口」从未在本机执行。
 *   - 当晚恢复到提交 9ed8b12 的状态之后，应用**仍然起不来，死法一模一样** —— 所以
 *     「删开关导致启动失败」这个推断已被证伪（回填记录见 git history 的 ADR-0001）。
 *   - 本机真正的阻塞在更底层：Electron 浏览器进程在加载任何应用 JS 之前就静默终止，
 *     裸 electron.exe（零仓库代码）同样如此，与这三个开关无关。
 *
 * 所以这条守卫现在只表达一件事：**保持代码等于最后一次已知可用的提交态（9ed8b12）**，
 * 免得机器问题还没解决，又有人凭一次没起过窗口的实验改动它。等能在真实终端跑起应用，
 * 再据实决定三个开关的去留，并同步改写本文件与 ADR 0001。
 *
 * 手法与 electron/app-user-model-id.test.ts 同族：读源码文本做契约校验，不启动 Electron。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const mainSrc = readFileSync(new URL('./main.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

/** 抓出 main.ts 里所有 appendSwitch 的开关名，便于报错时直接给出实际清单 */
const appendedSwitches = [...mainSrc.matchAll(/appendSwitch\('([^']+)'\)/g)].map((m) => m[1])

describe('主进程必须无条件禁用 GPU 渲染（本机驱动与 Electron v35 不兼容）', () => {
  it('三个渲染开关必须在源码里，缺任何一个本机启动即崩', () => {
    const required = ['disable-gpu', 'disable-gpu-compositing', 'disable-software-rasterizer']
    const missing = required.filter((s) => !appendedSwitches.includes(s))
    expect(
      missing,
      `本机 GPU 驱动下不禁用渲染会在初始化阶段静默退出（窗口不弹、无崩溃记录）；恢复删除请先补 ADR 0001 的回填证据。实际开关: ${appendedSwitches.join(', ')}`,
    ).toEqual([])
  })

  it('disable-gpu-disk-cache 保留；disable-gpu-sandbox 已裁决移除且不得擅自加回', () => {
    // disable-gpu-disk-cache：有明确实测依据（GPU 进程写不进缓存目录的权限错误），保留。
    expect(appendedSwitches).toContain('disable-gpu-disk-cache')
    // disable-gpu-sandbox：2026-10-26 单独裁决后移除（main.ts GPU 开关区注释有完整证据链：
    // 9ed8b12 捆绑引入无自身依据、守卫头部自证崩溃推断已证伪）。反向钉住——除非补出
    // 具体崩溃/兼容证据并走单独裁决，不允许把它顺手加回来。
    expect(
      appendedSwitches.includes('disable-gpu-sandbox'),
      `disable-gpu-sandbox 已于 2026-10-26 裁决移除（GPU 子进程沙箱应启用），不要未经裁决加回。实际开关: ${appendedSwitches.join(', ')}`,
    ).toBe(false)
  })
})
