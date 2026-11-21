/**
 * 渲染层统一日志 API（P2-1）。
 *
 * 目的：把散落在 src/ 下的裸 console.* 收敛到一处，统一加 [tag] 来源标签，
 * 并为 DEBUG 提供一个默认关闭的开关。关键约束：**不改变底层 console 方法**——
 * logInfo/logWarn/logError 一对一映射到 console.info/warn/error，因此
 * electron/core/main-logger.ts 的 console 劫持层（时间戳/级别前缀/落盘）行为
 * 完全不变，主进程看到的级别与格式与改动前一致。
 *
 * 用法（保留多参数透传，与原有 console.error('[tag] msg', err) 等价）：
 *   logError('streamAgentRun', 'onError 回调异常:', cbErr)
 *   logWarn('API', `Request ${path} failed`)
 *   logDebug('agent', '大体积状态', bigObject)   // 默认不输出
 *
 * DEBUG 开关：开发态（import.meta.env.DEV）恒开；生产态需 localStorage['ckun-debug']='1'。
 */
type Level = 'info' | 'warn' | 'error' | 'debug'

function debugEnabled(): boolean {
  if (typeof import.meta !== 'undefined' && import.meta.env?.DEV) return true
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('ckun-debug') === '1'
  } catch {
    return false
  }
}

function emit(level: Level, tag: string, args: unknown[]): void {
  if (level === 'debug' && !debugEnabled()) return
  const prefix = `[${tag}]`
  if (level === 'info') console.info(prefix, ...args)
  else if (level === 'warn') console.warn(prefix, ...args)
  else if (level === 'error') console.error(prefix, ...args)
  else console.debug(prefix, ...args)
}

export function logInfo(tag: string, ...args: unknown[]): void { emit('info', tag, args) }
export function logWarn(tag: string, ...args: unknown[]): void { emit('warn', tag, args) }
export function logError(tag: string, ...args: unknown[]): void { emit('error', tag, args) }
export function logDebug(tag: string, ...args: unknown[]): void { emit('debug', tag, args) }
