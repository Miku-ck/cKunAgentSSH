/**
 * 连接阶段超时：只覆盖 fetch 返回 headers 之前，不限制响应体/整条 SSE 流时长。
 *
 * 使用自建 timer，而非 AbortSignal.timeout()：后者内部定时器不受单测 fake timers
 * 控制，且会把超时作用域延伸到整条请求。调用方应在拿到响应后立即 clear()。
 */
export const CONNECT_TIMEOUT_MS = 28_000

export interface ConnectTimeout {
  readonly controller: AbortController
  readonly clear: () => void
}

export function createConnectTimeout(timeoutMs = CONNECT_TIMEOUT_MS): ConnectTimeout {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | null = setTimeout(() => {
    timer = null
    const error = new Error('连接超时')
    error.name = 'TimeoutError'
    controller.abort(error)
  }, timeoutMs)

  return {
    controller,
    clear: () => {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    },
  }
}
