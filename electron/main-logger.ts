import { app } from 'electron'
import { appendFileSync, mkdirSync, renameSync, statSync, unlinkSync, writeFileSync } from 'fs'
import { join } from 'path'
import { inspect } from 'node:util'

/**
 * 主进程统一日志（打包态落盘）。
 *
 * 为什么存在：打包态（Windows GUI 子系统）主进程没有 stdout/stderr 可看，
 * 全仓约 70 处 console.*（含各 manager 模块，它们与本文件共享同一个 console
 * 对象）在打包产物里全部黑洞化，现场排障时一行都拿不到。installMainLogger()
 * 在 main.ts 最早处劫持 console.log/info/warn/error：
 *   1. 统一加 [HH:MM:SS.mmm] 时间戳（口径沿用原 main.ts 的 tsNow，已迁入本文件）
 *      与级别前缀；
 *   2. 原样转发到**劫持前**捕获的原始 console（dev 终端行为不变）；
 *   3. 追加写入 userData/main.log，超 5MB 轮转，保留 .1/.2 两份历史（共 3 份）。
 *
 * 防递归：writeLine 内部只允许调用 originalConsole 与 fs 同步 API，绝不触碰
 * 被劫持后的 console —— logger 自己的报错（落盘失败等）也走 originalConsole.error。
 *
 * 失败姿态：userData 不可写（降级模式，见 main.ts 单实例锁注释）或磁盘异常时，
 * 落盘静默停用、原始 console 照常 —— 日志通道绝不能成为新的崩溃源或阻塞源。
 * electron-bridge.log 的独立写入（electron-bridge.ts bridgeLog）不受本劫持影响：
 * 劫持只拦 console.*，不拦 fs。
 */

/** [HH:MM:SS.mmm] 时间戳：启动链路的时序问题（IPC 先于 manager 就位、补全卡壳、
 *  后端起停交错）只有带毫秒时间戳才排得动。自 main.ts 迁入，口径不变。 */
export function tsNow(): string {
  const d = new Date()
  const p = (n: number, w = 2): string => String(n).padStart(w, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`
}

type LogLevel = 'log' | 'info' | 'warn' | 'error'

/** 文件里的级别前缀；console 转发用原始方法本身区分级别，不再重复前缀 */
const FILE_TAG: Record<LogLevel, string> = {
  log: '[INFO]',
  info: '[INFO]',
  warn: '[WARN]',
  error: '[ERROR]',
}

/** 渲染层 console-message 的 level（'info'|'warning'|'error'|'debug'）→ 主进程级别映射 */
const RENDERER_LEVEL: Record<string, { tag: string; level: LogLevel }> = {
  info: { tag: '[INFO]', level: 'info' },
  warning: { tag: '[WARN]', level: 'warn' },
  error: { tag: '[ERROR]', level: 'error' },
  debug: { tag: '[DEBUG]', level: 'log' },
}

// 劫持前先捕获原始方法并绑定 console（Node 的 console 方法依赖 this，裸解引用不保险）。
const originalConsole: Record<LogLevel, (line: string) => void> = {
  log: console.log.bind(console),
  info: console.info.bind(console),
  warn: console.warn.bind(console),
  error: console.error.bind(console),
}

/** 单文件上限与保留份数：main.log + main.log.1 + main.log.2，至多 15MB */
const MAX_LOG_BYTES = 5 * 1024 * 1024

const LOG_FILE_NAME = 'main.log'

let logFilePath: string | null = null
let logFileBytes = 0
/** 落盘失败只报一次，别让诊断通道自己刷屏 */
let diskWriteBroken = false

/** 首条日志时惰性初始化：userData 路径在 app ready 前后都可取；取不到就放弃落盘，
 *  且后续日志会再试（userData 恢复可写后自动续上，探针失败只是 mkdir/stat 的系统调用代价） */
function ensureLogFile(): void {
  if (logFilePath !== null) return
  try {
    const dir = app.getPath('userData')
    mkdirSync(dir, { recursive: true })
    const file = join(dir, LOG_FILE_NAME)
    try {
      logFileBytes = statSync(file).size
    } catch {
      logFileBytes = 0  // 首次尚无日志文件
    }
    logFilePath = file
  } catch {
    logFilePath = null  // userData 不可用：仅保留原始 console
  }
}

/**
 * 超限轮转：.2 删除 → .1 挪到 .2 → 当前挪到 .1，主文件从头写。
 * 保尾策略与 electron-bridge.log 一致：最近的日志永远在主文件可见，历史在 .1/.2 可回看。
 * rename 失败（Windows 上文件被外部句柄占用等）退化为清空主文件 —— 至多丢历史，不丢本轮。
 */
function rotateIfNeeded(nextLineBytes: number): void {
  if (logFilePath === null) return
  if (logFileBytes + nextLineBytes <= MAX_LOG_BYTES) return
  try { unlinkSync(`${logFilePath}.2`) } catch { /* .2 不存在属首次轮转，忽略 */ }
  try { renameSync(`${logFilePath}.1`, `${logFilePath}.2`) } catch { /* 同上 */ }
  try {
    renameSync(logFilePath, `${logFilePath}.1`)
    logFileBytes = 0
  } catch {
    try {
      writeFileSync(logFilePath, '', 'utf-8')
      logFileBytes = 0
    } catch { /* 交给 appendFileSync 的 catch 统一上报 */ }
  }
}

/** 非 string 参数用 inspect 单行展开（Error 带栈、循环引用不炸）；string 原样透传 */
function formatArg(v: unknown): string {
  if (typeof v === 'string') return v
  try {
    return inspect(v, { depth: 3, breakLength: Infinity, maxArrayLength: 100 })
  } catch {
    return String(v)
  }
}

/** 单条日志的唯一出口：加时间戳与级别前缀 → 原始 console → 追加落盘 */
function writeLine(fileTag: string, out: (line: string) => void, text: string): void {
  const line = `[${tsNow()}] ${fileTag} ${text}`
  // 先走原始通道：dev 终端行为与劫持前一致；打包态无 stdout 时此调用无害
  out(line)
  ensureLogFile()
  if (logFilePath === null) return
  const lineBytes = Buffer.byteLength(line, 'utf-8') + 1  // +1 = 换行
  try {
    rotateIfNeeded(lineBytes)
    appendFileSync(logFilePath, `${line}\n`, 'utf-8')
    logFileBytes += lineBytes
  } catch (e) {
    if (!diskWriteBroken) {
      diskWriteBroken = true
      try {
        originalConsole.error(`[main-logger] 日志落盘失败（本进程内不再重复报告）: ${e instanceof Error ? e.message : String(e)}`)
      } catch { /* 连原始 console 都不可用时放弃 */ }
    }
  }
}

/** main.ts 最早处调用一次：此后全主进程（含各 manager 模块）的 console.* 都进本链路 */
export function installMainLogger(): void {
  console.log = (...args: unknown[]) => writeLine(FILE_TAG.log, originalConsole.log, args.map(formatArg).join(' '))
  console.info = (...args: unknown[]) => writeLine(FILE_TAG.info, originalConsole.info, args.map(formatArg).join(' '))
  console.warn = (...args: unknown[]) => writeLine(FILE_TAG.warn, originalConsole.warn, args.map(formatArg).join(' '))
  console.error = (...args: unknown[]) => writeLine(FILE_TAG.error, originalConsole.error, args.map(formatArg).join(' '))
}

/**
 * 渲染层 console 转发（main.ts 经 webContents.on('console-message') 调入）。
 * 打包态渲染层的 console 同样无处可见 —— devtools 没开时连 dev 态都看不见。
 * 带 [renderer] 来源标签与源码位置，进同一条落盘链路（防递归口径与主进程一致）。
 */
export function logRendererConsole(level: string, message: string, sourceId: string, lineNumber: number): void {
  const mapped = RENDERER_LEVEL[level] ?? RENDERER_LEVEL.info
  const src = sourceId ? ` (${sourceId}:${lineNumber})` : ''
  writeLine(mapped.tag, originalConsole[mapped.level], `[renderer] ${message}${src}`)
}
