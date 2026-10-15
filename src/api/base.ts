/**
 * HTTP 客户端封装 — 基于 backendPort 构建 API URL
 *
 * 三种访问场景：
 * 1. Electron 渲染进程：通过 IPC 拿主进程分配的 backendPort + 127.0.0.1（loopback，最稳）
 * 2. 浏览器本机访问（http://localhost:5173）：用当前页面 host + 8010（与后端 dev 端口一致）
 * 3. 浏览器外部访问（http://[本机IP]:5173）：必须用 window.location.hostname 替代 127.0.0.1，
 *    否则请求会指向用户本机的 8010（不存在）
 */
import { useAppStore } from '@/stores/app'
import { extractSSHError, toFriendlySshMessage } from '@/ssh-error-types'

const FALLBACK_PORT = 8010  // 后端 dev 默认端口（与 backend/main.py --port default 一致）

// ── 后端鉴权 token 头 ──
// 跨层契约：Electron 层通过 IPC getBackendToken() 提供后端鉴权 token，
// 渲染进程所有发往后端的请求（含 SSE）都带 X-API-Token 头。
// 声明由 Electron 层在 src/env.d.ts 补充；此处用防御式访问，浏览器环境 / IPC 未实现时保持 null。
let apiToken: string | null = null
let apiTokenPromise: Promise<void> | null = null

/** 惰性获取后端 token：成功后缓存；失败 / 无 IPC 时保持 null（请求不带该头） */
export async function ensureApiToken(): Promise<void> {
  if (apiToken !== null) return
  if (apiTokenPromise) return apiTokenPromise
  apiTokenPromise = (async () => {
    try {
      const bridge = typeof window !== 'undefined'
        ? (window.electronAPI as unknown as { getBackendToken?: () => Promise<unknown> } | undefined)
        : undefined
      if (bridge && typeof bridge.getBackendToken === 'function') {
        const token = await bridge.getBackendToken()
        if (typeof token === 'string' && token.length > 0) {
          apiToken = token
        }
      }
    } catch {
      apiToken = null
    } finally {
      apiTokenPromise = null
    }
  })()
  return apiTokenPromise
}

/** 往请求头中追加 X-API-Token（token 为空时不加该头） */
export function withApiToken(headers: Record<string, string>): Record<string, string> {
  if (apiToken) headers['X-API-Token'] = apiToken
  return headers
}

/**
 * 统一错误对象：携带用户友好的 message，同时保留原始错误用于调试。
 */
export class FriendlyError extends Error {
  constructor(
    message: string,
    public original: unknown,
    public code?: string
  ) {
    super(message)
    this.name = 'FriendlyError'
  }
}

const TECHNICAL_ERROR_PATTERNS = [
  /SQL|database|sqlite|psycopg|mysql|postgres/i,
  /Traceback|Exception|Error:\s/i,
  /Internal Server Error/i,
  /Failed to fetch|NetworkError|fetch failed|net::/i,
  /Backend not ready/i,
  /ENOTFOUND|ECONNREFUSED|ETIMEDOUT|ECONNRESET/i,
]

function isTechnicalErrorMessage(msg: string): boolean {
  return TECHNICAL_ERROR_PATTERNS.some(p => p.test(msg))
}

/**
 * 剥掉 Electron IPC 的投递信封。
 *
 * S0-13：主进程 reject 到达渲染端时恒为
 * `Error invoking remote method '<channel>': Error: <原始 message>`，
 * 而 TECHNICAL_ERROR_PATTERNS 里有一条 `/Error:\s/i` —— 信封本身就带 `Error: `，
 * 于是**任何**非 SSHError 的主进程抛错都被改写成「服务暂时不可用，请稍后重试」，
 * 具体根因（会话不存在 / 参数校验失败 / 目标目录不合法…）永远看不到。
 * 同仓既有口径：src/stores/server-monitor-core.ts 的 describeMonitorError
 * 也是先剥信封再判文案。
 */
export function unwrapIpcErrorEnvelope(msg: string): string {
  if (!msg) return msg
  const root = msg
    .replace(/^Error invoking remote method '[^']+'\s*:\s*/, '')
    .replace(/^Error:\s*/i, '')
  return root || msg
}

/**
 * 把「一段消息」映射成用户可读文案。Error 与裸 string 两条腿共用它 ——
 * 规则抄两份的话，下一次加一条模式只会加在其中一份上。
 */
function friendlyFromMessage(raw: string, fallback: string): string {
  const msg = unwrapIpcErrorEnvelope(raw)
  if (!msg) return fallback
  if (/Backend not ready|后端未就绪/i.test(msg)) return '后端服务未就绪，请稍后重试'
  if (/fetch failed|NetworkError|Failed to fetch|网络连接失败/i.test(msg)) return '网络连接失败，请检查服务是否运行'
  if (/未登录|登录已过期|Unauthorized|401/i.test(msg)) return '未登录或登录已过期，请重新登录'
  if (isTechnicalErrorMessage(msg)) return '服务暂时不可用，请稍后重试'
  return msg
}

/**
 * 把底层/代码级错误转换为用户可读的友好提示。
 * v2 改进：优先识别 SSHErrorTransport（结构化错误，精准文案），再回退原有模式匹配。
 */
export function toFriendlyMessage(e: unknown, fallback = '操作失败，请稍后重试'): string {
  // v2：优先提取 SSH 结构化错误（精确文案）
  const sshTransport = extractSSHError(e)
  if (sshTransport) {
    // S0-14：transport.message 是主进程用共享文案表渲染好的成品，直接采信；
    // 原实现拿 (code, detail=undefined) 重渲染 ⇒ 把「30秒」「会话 x 不存在」
    // 这类只有生产端知道的 detail 抹掉了。detail 仅在 message 缺失时兜底。
    // 同仓口径见 describeMonitorError（用 t.message）。
    return sshTransport.message || toFriendlySshMessage(sshTransport.code, sshTransport.detail)
  }

  if (e instanceof FriendlyError) return e.message
  if (e instanceof Error) return friendlyFromMessage(e.message, fallback)
  // S5-14：裸字符串是这条缝上完全合法的错误形状 —— store 的 `localError` /
  // `remoteError` 就是 `ref('')`，而调用点（SftpPane 地址栏提交）把它原样交进来。
  // 缺这一支时恒落 fallback：用户输 `C:\Users\<u>\.ssh` 得到的不是「拒绝访问敏感
  // 路径」，而是「路径不存在或无法访问」—— 把一次安全拦截说成文件系统问题。
  if (typeof e === 'string') return friendlyFromMessage(e, fallback)
  return fallback
}

function isElectron(): boolean {
  return typeof window !== 'undefined' && !!window.electronAPI?.getBackendPort
}

export function getBaseUrl(): string {
  if (isElectron()) {
    // Electron：走 IPC 拿主进程真实端口
    const appStore = useAppStore()
    const port = appStore.backendPort
    if (!port) throw new Error('Backend not ready')
    return `http://127.0.0.1:${port}/api`
  }
  // 浏览器：用当前页面 host（dev 端口 5173 → 后端 8010）
  let host = typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1'
  // H13 修复：IPv6 [::1] → IPv4 127.0.0.1
  // 原因：vite dev server 可能绑定到 [::1]:5173（IPv6 only），导致 window.location.hostname 返回 [::1]；
  // 但后端监听 0.0.0.0:8010（IPv4 only），用 [::1]:8010 发请求会被拒绝（Failed to fetch）。
  // 统一映射到 127.0.0.1，确保 IPv4 可达。
  if (host === '[::1]' || host === '::1' || host === 'localhost') {
    host = '127.0.0.1'
  }
  return `http://${host}:${FALLBACK_PORT}/api`
}

/**
 * 轮询后端健康检查，等待后端恢复就绪
 * 通过 IPC 获取最新端口（后端重启可能更换端口）
 * @param timeoutMs 最大等待时间，超时后抛出错误
 */
let _healthCheckPromise: Promise<void> | null = null

export async function waitForBackendReady(timeoutMs = 8000): Promise<void> {
  // 并发去重：多个请求同时遇到网络错误时，复用同一个健康检查 Promise，避免 hammer /api/health
  if (_healthCheckPromise) {
    return _healthCheckPromise
  }
  _healthCheckPromise = (async () => {
    try {
      const start = Date.now()
      while (Date.now() - start < timeoutMs) {
        // 解析后端 host:port（Electron 走 IPC，浏览器走 window.location）
        const baseUrl = getBaseUrl()
        try {
          const headers: Record<string, string> = {}
          await ensureApiToken()
          const res = await fetch(`${baseUrl}/health`, {
            signal: AbortSignal.timeout(2000),
            headers: withApiToken(headers),
          })
          if (res.ok) return
        } catch {
          // 后端未就绪，继续等待
        }
        await new Promise(r => setTimeout(r, 800))
      }
      throw new Error('Backend not ready after waiting')
    } finally {
      _healthCheckPromise = null
    }
  })()
  return _healthCheckPromise
}

/**
 * 后端错误体 → 可读文案。
 * 422（请求体校验失败）的 detail 是 [{loc, msg, type}] 数组，
 * 直接交给 Error/toast 会得到 "[object Object]"。
 */
export function backendErrorDetail(err: unknown): string {
  const detail = (err as { detail?: unknown } | null)?.detail
  if (Array.isArray(detail)) {
    return detail.map(d => (d as { msg?: string })?.msg || String(d)).join('; ')
  }
  return typeof detail === 'string' ? detail : ''
}

export async function request<T>(path: string, options?: RequestInit, _retrying = false): Promise<T> {
  const { headers: customHeaders, ...restOptions } = options || {}
  // FormData 请求由浏览器自动设置 Content-Type（含 multipart boundary），不要手动覆盖
  const isFormData = restOptions.body instanceof FormData
  // 惰性获取 token（缓存结果），确保 X-API-Token 头带上；重试路径复用缓存的 token
  await ensureApiToken()
  try {
    // B36 修复：getBaseUrl 可能抛原始 Error('Backend not ready') —
    // 移入 try 内，统一走下方 FriendlyError 包装（友好文案"后端服务未就绪"）
    const url = `${getBaseUrl()}${path}`
    const res = await fetch(url, {
      ...restOptions,
      headers: withApiToken({
        ...(restOptions.method && restOptions.method !== 'GET' && !isFormData ? { 'Content-Type': 'application/json' } : {}),
        ...(customHeaders as Record<string, string>),
      }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(backendErrorDetail(err) || res.statusText)
    }
    if (res.status === 204) return {} as T
    return res.json()
  } catch (e: unknown) {
    // 网络层错误（后端崩溃/重启）：等待后端恢复后重试一次
    const isNetworkError = e instanceof TypeError && /fetch/i.test(e.message)
    // B14 修复：自动重试仅限幂等的 GET/DELETE —
    // POST/PUT/PATCH 重放会双发（重复建会话/授权），失败直接抛错
    const method = ((restOptions.method as string) || 'GET').toUpperCase()
    const isIdempotent = method === 'GET' || method === 'DELETE'
    if (isNetworkError && !_retrying && isIdempotent) {
      console.warn(`[API] Request ${path} failed (network), waiting for backend to recover...`)
      try {
        await waitForBackendReady()
        // 后端已恢复，继续重试请求
        return request<T>(path, options, true)
      } catch {
        // 后端未恢复，抛出统一友好错误
        throw new FriendlyError('网络连接失败，请检查服务是否运行', e)
      }
    }
    // 将技术/代码级错误转换为用户友好提示；业务错误（如用户名或密码错误）保持不变
    const friendly = toFriendlyMessage(e, '请求失败，请稍后重试')
    // 已经是 FriendlyError 时，避免二次包装导致信息丢失
    if (e instanceof FriendlyError) throw e
    if (e instanceof Error && friendly === e.message) throw e
    throw new FriendlyError(friendly, e)
  }
}

