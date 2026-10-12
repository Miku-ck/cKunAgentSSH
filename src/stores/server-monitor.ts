/**
 * 服务器实时监控 — 远端指标轮询采集（B 阶段）
 *
 * 复用既有 ssh:exec IPC（exec channel，独立于终端 shell，不污染交互终端）。
 * 一条聚合命令取 CPU/内存/负载/磁盘/监听端口（5s 快轮询），进程 TOP 单独一条
 * 命令 60s 慢轮询，解析失败或会话断开时静默失败（连续失败 3 次停止轮询）。
 *
 * 状态放 Pinia store：Dashboard 视图切换/卸载不中断轮询；
 * 会话全部关闭时由 useSshNavigation 的 watch 停止。
 * 纯解析/差分逻辑在 server-monitor-core.ts（无 vue 依赖，独立单测）。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { useSSHStore } from './ssh'
import {
  METRICS_CMD,
  PROCS_CMD,
  parseMetricsRaw,
  parseProcsRaw,
  computeCpuDiff,
  describeMonitorError,
  type ServerMetrics,
  type CpuSample,
  type ProcessTopEntry,
  type RawStat,
} from './server-monitor-core'
import { isRetryable } from '../ssh-error-types'

const POLL_INTERVAL_MS = 5000
const PROCS_INTERVAL_MS = 60000 // 进程 TOP 慢轮询：ps 排序开销大，不随 5s 快轮询放大
const HISTORY_MAX = 60       // 状态条 sparkline 保留点数（5min @ 5s）
const FAIL_STOP_THRESHOLD = 3
const RETRY_INTERVAL_MS = 15000 // 连续失败停止后，目标会话仍在时后台重试的间隔

/** 切走某远程会话时保留其历史快照，切回时恢复，避免切 tab 丢 sparkline 数据 */
interface RetainedHistory {
  current: ServerMetrics | null
  procs: ProcessTopEntry[]
  cpu: number[]
  mem: number[]
  load: number[]
  gpu: Record<string, { util: number[]; mem: number[] }>
  errorCount: number
  lastError: { message: string; code: string; retryable: boolean } | null
  prevStat: RawStat | null
}
const RETAINED_MAX = 16

/** 给 Promise 加硬超时兜底：超时 reject，保证 await 必 settle（用于防止采集卡死） */
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(label)), ms)
    p.then(
      v => { clearTimeout(t); resolve(v) },
      e => { clearTimeout(t); reject(e) },
    )
  })
}

export const useServerMonitorStore = defineStore('server-monitor', () => {
  const sshStore = useSSHStore()

  const current = ref<ServerMetrics | null>(null)
  /** 进程 TOP（60s 慢轮询独立采样，与 5s 指标解耦；失败静默保留上次列表） */
  const procs = ref<ProcessTopEntry[]>([])
  /** 历史序列（时间正序，长度 ≤ HISTORY_MAX）— 状态条 sparkline 数据源 */
  const cpuHistory = ref<number[]>([])
  const memHistory = ref<number[]>([])
  const loadHistory = ref<number[]>([])
  /** 每卡利用率/显存历史（nvitop 模式逐格色块条；键 = GPU index 字符串） */
  const gpuHist = ref<Record<string, { util: number[]; mem: number[] }>>({})
  /** 连续失败计数（达 FAIL_STOP_THRESHOLD 转后台重试） */
  const errorCount = ref(0)
  const polling = ref(false)
  /** 最近一次采集错误（结构化：code + 友好中文文案 + 是否可重试） */
  const lastError = ref<{ message: string; code: string; retryable: boolean } | null>(null)

  let timer: ReturnType<typeof setTimeout> | null = null
  let procsTimer: ReturnType<typeof setTimeout> | null = null
  let inflight = false
  let procsInflight = false
  let activeSshId = ''
  /** 监控目标会话（连续失败停止后仍保留，用于后台重试；stop 时清空） */
  let targetSshId = ''
  /** 连续失败停止后的后台重试定时器（与 5s 轮询互斥） */
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  /** 上一次 CPU 原始计数器（跨轮次差分算使用率） */
  let prevStat: RawStat | null = null
  /** 各会话的历史快照（切走保留、切回恢复），避免切换 tab 清空 sparkline 历史 */
  const retained: Record<string, RetainedHistory> = {}

  function retainActive(): void {
    if (!activeSshId) return
    retained[activeSshId] = {
      current: current.value,
      procs: procs.value,
      cpu: cpuHistory.value,
      mem: memHistory.value,
      load: loadHistory.value,
      gpu: gpuHist.value,
      errorCount: errorCount.value,
      lastError: lastError.value,
      prevStat,
    }
    // 限制保留的会话数，避免长生命周期下无限增长
    const keys = Object.keys(retained)
    if (keys.length > RETAINED_MAX) delete retained[keys[0]]
  }

  function restoreFrom(saved: RetainedHistory): void {
    current.value = saved.current
    procs.value = saved.procs
    cpuHistory.value = saved.cpu
    memHistory.value = saved.mem
    loadHistory.value = saved.load
    gpuHist.value = saved.gpu
    errorCount.value = saved.errorCount
    lastError.value = saved.lastError
    prevStat = saved.prevStat
  }

  function pushHistory(arr: number[], v: number) {
    arr.push(v)
    if (arr.length > HISTORY_MAX) arr.shift()
  }

  /** 采样一次。返回 true = 成功取到指标。
   *  失败路径一律结构化记 lastError；连续失败达阈值 → 转后台重试模式。 */
  async function sample(): Promise<boolean> {
    if (inflight) return false
    // 会话已不在列表 / 非远程：不再静默 return（会导致 polling=true 却永远
    // 无数据也无错误 → UI 卡在"正在采集…"）。记为错误并停止轮询，让 UI 给出原因。
    if (!activeSshId) {
      stop()
      return false
    }
    const session = sshStore.sessions.find(s => s.id === activeSshId)
    if (!session || session.type !== 'remote') {
      errorCount.value += 1
      // 与 describeMonitorError 同 code（SESSION_NOT_FOUND）语义保持一致：
      // retryable 统一由共享 isRetryable 推导（单一事实源），避免同 code 两处理解漂移。
      lastError.value = { message: '监控目标会话不可用', code: 'SESSION_NOT_FOUND', retryable: isRetryable('SESSION_NOT_FOUND') }
      if (errorCount.value >= FAIL_STOP_THRESHOLD) enterRetryMode()
      return false
    }
    inflight = true
    try {
      // 硬超时兜底：exec 自带 10s，再加 12s 外层保护，确保 Promise 必 settle、
      // inflight 必释放，杜绝"采集卡死"（inflight 永久为 true 导致后续轮询全部跳过）
      const res = await withTimeout(
        window.electronAPI!.sshExec(activeSshId, METRICS_CMD, 10),
        12000,
        'metrics poll timeout',
      )
      if (activeSshId !== session.id) return false // 采集期间切换了会话
      const host = sshStore.connections.find(c => c.id === session.connectionId)?.host
      const parsed = parseMetricsRaw(res.stdout, host)
      if (!parsed) throw new Error('metrics parse failed')
      const cpu: CpuSample = computeCpuDiff(prevStat, parsed.rawStat)
      prevStat = parsed.rawStat
      const { rawStat: _discard, ...rest } = parsed
      current.value = { ...rest, sshId: session.id, cpu, ts: Date.now() }
      errorCount.value = 0
      lastError.value = null
      pushHistory(cpuHistory.value, cpu.used)
      pushHistory(memHistory.value, rest.mem.percent)
      pushHistory(loadHistory.value, rest.load[0])
      for (const g of rest.gpus) {
        const key = String(g.index)
        const h = gpuHist.value[key] ?? { util: [], mem: [] }
        pushHistory(h.util, g.utilization)
        pushHistory(h.mem, g.memPercent)
        gpuHist.value[key] = h
      }
      return true
    } catch (e: unknown) {
      if (activeSshId !== session.id) return false
      errorCount.value += 1
      // 结构化错误：走共享 SSH 错误文案，不再把 Electron IPC 原始串透给 UI
      lastError.value = describeMonitorError(e)
      if (errorCount.value >= FAIL_STOP_THRESHOLD) {
        enterRetryMode()
      }
      return false
    } finally {
      inflight = false
    }
  }

  function scheduleNext() {
    if (!polling.value) return
    timer = setTimeout(() => {
      timer = null
      void sample()
      scheduleNext()
    }, POLL_INTERVAL_MS)
  }

  /** 进程 TOP 采样（60s 慢轮询）。失败静默：保留上次列表，不进指标错误条。 */
  async function sampleProcs(): Promise<void> {
    if (procsInflight || !polling.value || !activeSshId) return
    const session = sshStore.sessions.find(s => s.id === activeSshId)
    if (!session || session.type !== 'remote') return
    procsInflight = true
    try {
      const res = await withTimeout(
        window.electronAPI!.sshExec(activeSshId, PROCS_CMD, 10),
        12000,
        'procs poll timeout',
      )
      if (activeSshId !== session.id) return // 采样期间切换了会话
      procs.value = parseProcsRaw(res.stdout)
    } catch {
      // 静默：进程表失败不干扰指标面板，下一轮 60s 自然重试
    } finally {
      procsInflight = false
    }
  }

  function scheduleProcs() {
    procsTimer = setTimeout(() => {
      procsTimer = null
      void sampleProcs()
      scheduleProcs()
    }, PROCS_INTERVAL_MS)
  }

  /** 连续失败停止 5s 轮询，但进入后台重试：只要目标会话仍在列表，
   *  每隔 RETRY_INTERVAL_MS 再采样一次；采样成功即自动恢复轮询。
   *  这样"重连后自动恢复"对通道挂死/短暂不可达也成立（不只是换会话 id）。
   *  重试本身是有限成本（15s 一次只读探测），会话从列表消失或 stop() 时即终止。 */
  function enterRetryMode() {
    if (timer) { clearTimeout(timer); timer = null }
    polling.value = false
    if (retryTimer) return // 已在重试中
    retryTimer = setTimeout(async () => {
      retryTimer = null
      const stillThere = targetSshId &&
        sshStore.sessions.some(s => s.id === targetSshId && s.type === 'remote')
      if (!stillThere) { stop(); return }
      const ok = await sample()
      // 采样在飞期间用户可能 stop()/切会话（activeSshId 被清空）→ 不得复活轮询
      if (ok && !polling.value && activeSshId) {
        // 采样成功但 5s 轮询尚未恢复（start/scheduleNext 未重挂）→ 恢复轮询
        polling.value = true
        scheduleNext()
      }
    }, RETRY_INTERVAL_MS)
  }

  /**
   * 开始监控指定远程会话。切换会话**不需要**调用方先 stop：本函数自己按
   * retainActive() → stop() 的顺序保留上一会话的历史快照再恢复目标会话的。
   * 调用方抢先 stop() 会让 activeSshId 提前清空，retainActive 无从归档 ——
   * 整套快照保留机制静默失效，表现为切 tab 后 sparkline 从头画。
   */
  function start(sshId: string) {
    const session = sshStore.sessions.find(s => s.id === sshId)
    if (!session || session.type !== 'remote') return
    // 保留上一会话的历史快照，随后 stop() 清零当前槽位；若目标会话已有快照则恢复，
    // 避免切 tab 清空 sparkline 历史（数据丢失竞态）。stop() 仍负责定时器/轮询清理。
    retainActive()
    stop()
    const saved = retained[sshId]
    if (saved) restoreFrom(saved)
    activeSshId = sshId
    targetSshId = sshId
    polling.value = true
    void sample()
    scheduleNext()
    // 进程 TOP 首轮立即采一次，之后 60s 一轮
    void sampleProcs()
    scheduleProcs()
  }

  /** 停表并清空全部读数：唯一的状态清零入口 */
  function stop() {
    polling.value = false
    activeSshId = ''
    targetSshId = ''
    current.value = null
    procs.value = []
    cpuHistory.value = []
    memHistory.value = []
    loadHistory.value = []
    gpuHist.value = {}
    errorCount.value = 0
    lastError.value = null
    prevStat = null
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    if (procsTimer) {
      clearTimeout(procsTimer)
      procsTimer = null
    }
    if (retryTimer) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
  }

  return {
    current,
    procs,
    cpuHistory,
    memHistory,
    loadHistory,
    gpuHist,
    polling,
    lastError,
    start,
    stop,
  }
})
