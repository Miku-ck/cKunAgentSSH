/**
 * 崩溃上报（本地 Crashpad，不联网）。
 *
 * 为什么要有：打包态主进程/渲染进程崩溃时，用户能拿到的只有"窗口没了"。
 * 主进程的 JS 异常有 uncaughtException 兜底（main.ts）与 main.log 落盘，但
 * **原生崩溃**（GPU/渲染进程/后端子进程）根本不走 JS 层，进程直接消失 ——
 * 那种现场只留在 Crashpad 的 .dmp 里。这里把 crashReporter 打开、把转储目录
 * 固定到 userData 下（诊断包要从那里取），并把 render/child-process-gone 落进
 * 主日志（转储说明"崩了"，日志说明"崩的是谁、什么原因"）。
 *
 * 三条约束：
 *   1. uploadToServer: false —— 本项目没有崩溃收集服务，任何"顺手联网"都不可接受；
 *   2. 必须在 app ready 之前调用，否则早崩的渲染进程抓不到；
 *   3. 绝不抛错：崩溃上报是附加能力，启不来只是少一个现场，不能让应用起不来。
 */
import { app, crashReporter } from 'electron'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

/**
 * 转储目录。固定放 userData 而不是 Electron 默认位置：
 * 默认位置随平台/版本变（Windows 常在 %TEMP% 下），诊断包要取时无从下手。
 */
export function crashDumpsDir(userDataDir: string): string {
  return join(userDataDir, 'Crashpad')
}

/** 只暴露本项目实际用到的字段，避免把整个 Electron 类型带进单测桩 */
export interface CrashReporterStartOptionsLike {
  uploadToServer: boolean
  compress: boolean
  productName?: string
  globalExtra?: Record<string, string>
  ignoreSystemCrashHandler?: boolean
}

export interface CrashReporterOptions {
  userDataDir: string
  productName?: string
  log?: (level: 'info' | 'warn' | 'error', message: string) => void
  /** 注入点：单测替换真实启动（真调需要 Electron 运行时） */
  startCrashReporter?: (options: CrashReporterStartOptionsLike) => void
}

let started = false

/**
 * 注册崩溃上报 + 进程级崩溃日志。返回转储目录（诊断包用同一个值）。
 * 幂等：重复调用只返回目录，不会二次 start（Electron 会警告并忽略）。
 */
export function setupCrashReporter(opts: CrashReporterOptions): string {
  const dir = crashDumpsDir(opts.userDataDir)
  if (started) return dir
  started = true

  const log = opts.log ?? ((level: 'info' | 'warn' | 'error', message: string) => {
    // 显式落到全局 console：本函数可能早于 main.ts 的 logWarn/logError 定义被调用，
    // 但 installMainLogger 已经劫持了 console，这里照样进 main.log。
    if (level === 'error') console.error(`[crash] ${message}`)
    else if (level === 'warn') console.warn(`[crash] ${message}`)
    else console.log(`[crash] ${message}`)
  })

  try {
    mkdirSync(dir, { recursive: true })
    app.setPath('crashDumps', dir)
  } catch (e) {
    // 目录建不出来时退回 Electron 默认位置：转储照样产生，只是诊断包可能取不到
    log('warn', `崩溃转储目录不可用（用默认位置）：${e instanceof Error ? e.message : String(e)}`)
  }

  try {
    const start = opts.startCrashReporter ?? ((o: CrashReporterStartOptionsLike) => crashReporter.start(o))
    start({
      // 只收集不上传：崩溃数据里可能带命令、路径、主机名，不经过用户手就出本机是不可接受的
      uploadToServer: false,
      compress: false,
      productName: opts.productName ?? app.getName(),
      ignoreSystemCrashHandler: false,
      // 转储里带上版本：只拿到一份 .dmp 时，第一件要确认的就是它来自哪个版本
      globalExtra: { 'app.version': app.getVersion() },
    })
    log('info', `崩溃上报已启用（本地收集，目录 ${dir}）`)
  } catch (e) {
    log('warn', `崩溃上报未启用（不影响使用）：${e instanceof Error ? e.message : String(e)}`)
  }

  // 进程级崩溃：crashpad 只留一份二进制转储，能读懂"怎么了"的只有这行日志
  app.on('render-process-gone', (_event, _webContents, details) => {
    log('error', `渲染进程崩溃：reason=${details.reason} exitCode=${details.exitCode}（转储见 ${dir}）`)
  })
  app.on('child-process-gone', (_event, details) => {
    const name = 'name' in details && details.name ? ` name=${details.name}` : ''
    log('error', `子进程崩溃：type=${details.type}${name} reason=${details.reason} exitCode=${details.exitCode}`)
  })

  return dir
}
