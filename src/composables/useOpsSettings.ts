/**
 * 运维设置（G4 出站代理 / G5 超时与重试 / G9 日志级别 / G10 诊断快照）。
 *
 * 与 memory/postProcess 那两组的根本区别：这三项的权威值只在后端，本地不写
 * localStorage，所以不需要 store 的 debounce watcher，也不需要额外的快照缓存 ——
 * 每项保存都是一次显式 PUT，失败即回滚到最近一次成功读/写的值（synced）。
 *
 * 代理字段有个绕不开的坑：GET 只回脱敏值（可能带口令，不能回明文）。若把"没填"
 * 当成"清除"，用户只想改超时就会顺手抹掉代理口令。所以 saveHttp 从不带 proxy_url，
 * 改代理只能走 saveProxy/clearProxy 这两个显式动作（后端把缺省字段视为"不改"）。
 */
import { computed, ref, watch } from 'vue'
import {
  api,
  toFriendlyMessage,
  type DiagnosticsSnapshot,
  type LogLevel,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'

/** 与 netconfig.TIMEOUT_MIN/MAX、RETRY_MAX 同口径：0 是"不覆盖"的哨兵值，不是最小值 */
export const TIMEOUT_MIN = 0
export const TIMEOUT_MAX = 600
export const RETRY_MAX = 3

export function useOpsSettings() {
  const toast = useToastStore()

  // ── 出站代理 ──
  const proxyDraft = ref('')
  const proxyMasked = ref('')
  const proxySet = ref(false)
  /** 'db' = 设置页存的，'env' = 进程环境变量兜底（此处保存会覆盖它） */
  const proxySource = ref<'' | 'db' | 'env'>('')
  const savingProxy = ref(false)

  // ── 超时 / 重试 ──
  const timeoutDraft = ref(0)
  const retryDraft = ref(1)
  const savingHttp = ref(false)
  /**
   * 输入框绑这个，不绑 timeoutDraft：`v-model.number` 在清空时给 ''，而 Number('') 是 0，
   * 于是"把框清空"会被当成"把超时设成 0 秒"并真的写进后端。清空要落回占位值，
   * 得由这里显式区分"空/非法 → NaN（判定为无效，禁止保存）"。
   */
  const timeoutText = ref(String(timeoutDraft.value))
  watch(timeoutText, (v) => {
    const n = Number(v)
    timeoutDraft.value = v.trim() === '' || !Number.isFinite(n) ? NaN : Math.floor(n)
  })

  // ── 日志级别 ──
  const logLevel = ref<LogLevel>('warning')
  const logLevels = ref<LogLevel[]>(['warning'])
  const savingLevel = ref(false)

  // ── 诊断快照 ──
  const diagnostics = ref<DiagnosticsSnapshot | null>(null)
  const diagLoading = ref(false)
  const diagError = ref('')

  const loading = ref(false)
  const loadError = ref('')

  // 后端最近一次一致状态（读回或写回），既做回滚基线也做"有无改动"判定
  const synced = ref({ timeout: 0, retry: 1 })

  const timeoutInvalid = computed(() => !Number.isInteger(timeoutDraft.value)
    || timeoutDraft.value < TIMEOUT_MIN || timeoutDraft.value > TIMEOUT_MAX)

  const httpDirty = computed(() => timeoutDraft.value !== synced.value.timeout
    || retryDraft.value !== synced.value.retry)

  function clampInt(value: unknown, min: number, max: number, fallback: number): number {
    const n = Number(value)
    if (!Number.isFinite(n)) return fallback
    return Math.min(max, Math.max(min, Math.floor(n)))
  }

  function applyHttp(data: {
    proxy_url_masked?: string
    proxy_url_set?: boolean
    proxy_source?: string
    timeout_seconds?: number
    retry_count?: number
  }) {
    proxyMasked.value = data.proxy_url_masked ?? ''
    proxySet.value = data.proxy_url_set === true
    proxySource.value = data.proxy_source === 'db' || data.proxy_source === 'env' ? data.proxy_source : ''
    timeoutDraft.value = clampInt(data.timeout_seconds, TIMEOUT_MIN, TIMEOUT_MAX, 0)
    timeoutText.value = String(timeoutDraft.value)
    retryDraft.value = clampInt(data.retry_count, 0, RETRY_MAX, 1)
    synced.value = { timeout: timeoutDraft.value, retry: retryDraft.value }
  }

  function applyLevel(data: { level?: string; levels?: string[] }) {
    const levels = Array.isArray(data.levels) && data.levels.length
      ? (data.levels as LogLevel[])
      : logLevels.value
    logLevels.value = levels
    // 只接受后端确实认识的级别，脏行不会把下拉框变成一个选不中的值
    logLevel.value = levels.includes(data.level as LogLevel) ? (data.level as LogLevel) : 'warning'
  }

  async function load() {
    loading.value = true
    loadError.value = ''
    try {
      // 并发拉取：日志级别读不到不该把网络区也拖成空态
      const [http, level] = await Promise.all([api.getHttpSettings(), api.getLogLevel()])
      applyHttp(http)
      applyLevel(level)
    } catch (e: unknown) {
      loadError.value = toFriendlyMessage(e, '运维设置加载失败')
    } finally {
      loading.value = false
    }
  }

  /** 显式写代理：payload 只有 proxy_url，超时/重试留在后端原值上 */
  async function saveProxy(): Promise<boolean> {
    const value = proxyDraft.value.trim()
    if (!value) {
      toast.warning('代理地址为空：留空请直接点"清除"，否则保存会保留原代理')
      return false
    }
    savingProxy.value = true
    try {
      const data = await api.updateHttpSettings({ proxy_url: value })
      applyHttp(data)
      proxyDraft.value = ''
      const rebuilt = (data as { clients_rebuilt?: number }).clients_rebuilt ?? 0
      toast.success(rebuilt ? `代理已生效，${rebuilt} 个模型连接已按新策略重建` : '代理已生效')
      return true
    } catch (e: unknown) {
      toast.error(`代理保存失败：${toFriendlyMessage(e, '请检查地址格式')}`, 5000)
      return false
    } finally {
      savingProxy.value = false
    }
  }

  async function clearProxy(): Promise<boolean> {
    savingProxy.value = true
    try {
      const data = await api.updateHttpSettings({ proxy_url: '' })
      applyHttp(data)
      proxyDraft.value = ''
      toast.success('已清除代理，出站请求恢复直连')
      return true
    } catch (e: unknown) {
      toast.error(`清除代理失败：${toFriendlyMessage(e)}`, 5000)
      return false
    } finally {
      savingProxy.value = false
    }
  }

  /** 超时/重试一起发；不带 proxy_url 是刻意的（见文件头） */
  async function saveHttp(): Promise<boolean> {
    const timeout = clampInt(timeoutDraft.value, TIMEOUT_MIN, TIMEOUT_MAX, synced.value.timeout)
    const retry = clampInt(retryDraft.value, 0, RETRY_MAX, synced.value.retry)
    timeoutDraft.value = timeout
    retryDraft.value = retry
    savingHttp.value = true
    try {
      const data = await api.updateHttpSettings({ timeout_seconds: timeout, retry_count: retry })
      applyHttp(data)
      toast.success(timeout ? `出站超时已设为 ${timeout}s` : '超时已恢复各端点默认')
      return true
    } catch (e: unknown) {
      timeoutDraft.value = synced.value.timeout
      timeoutText.value = String(synced.value.timeout)
      retryDraft.value = synced.value.retry
      toast.error(`超时/重试保存失败，已回滚：${toFriendlyMessage(e)}`, 5000)
      return false
    } finally {
      savingHttp.value = false
    }
  }

  async function setLogLevel(next: LogLevel): Promise<boolean> {
    if (next === logLevel.value) return true
    const prev = logLevel.value
    logLevel.value = next
    savingLevel.value = true
    try {
      const data = await api.updateLogLevel(next)
      applyLevel(data)
      toast.success(`后端日志级别：${logLevel.value}`)
      return true
    } catch (e: unknown) {
      logLevel.value = prev
      toast.error(`日志级别设置失败，已回滚：${toFriendlyMessage(e)}`, 5000)
      return false
    } finally {
      savingLevel.value = false
    }
  }

  async function refreshDiagnostics(): Promise<boolean> {
    diagLoading.value = true
    diagError.value = ''
    try {
      const d = await api.getDiagnostics()
      diagnostics.value = d
      // 诊断报的就是生效值，顺手回填两处控件（面板开着时别处改过也能看到）
      applyLevel(d.logging)
      applyHttp({
        proxy_url_masked: d.http.proxy,
        proxy_url_set: d.http.proxy_set,
        proxy_source: proxySource.value,
        timeout_seconds: d.http.timeout_seconds,
        retry_count: d.http.retry_count,
      })
      return true
    } catch (e: unknown) {
      diagError.value = toFriendlyMessage(e, '诊断信息获取失败')
      return false
    } finally {
      diagLoading.value = false
    }
  }

  return {
    proxyDraft,
    proxyMasked,
    proxySet,
    proxySource,
    savingProxy,
    timeoutDraft,
    timeoutText,
    retryDraft,
    savingHttp,
    timeoutInvalid,
    httpDirty,
    logLevel,
    logLevels,
    savingLevel,
    diagnostics,
    diagLoading,
    diagError,
    loading,
    loadError,
    load,
    saveProxy,
    clearProxy,
    saveHttp,
    setLogLevel,
    refreshDiagnostics,
  }
}
