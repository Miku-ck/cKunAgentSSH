/**
 * 服务器实时监控 — 远端指标轮询采集（B 阶段）
 *
 * 复用既有 ssh:exec IPC（exec channel，独立于终端 shell，不污染交互终端）。
 * 一条聚合命令取 CPU/内存/负载/磁盘/进程TOP/监听端口，key=value 行输出，
 * 解析失败或会话断开时静默失败（连续失败 3 次停止轮询）。
 *
 * 状态放 Pinia store：Dashboard 视图切换/卸载不中断轮询；
 * 会话全部关闭时由 useSshNavigation 的 watch 停止。
 * 纯解析/差分逻辑在 server-monitor-core.ts（无 vue 依赖，独立单测）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { useSSHStore } from './ssh'
import { useSshAiStore } from './ssh-ai'
import {
  METRICS_CMD,
  parseMetricsRaw,
  computeCpuDiff,
  describeMonitorError,
  type ServerMetrics,
  type CpuSample,
  type DiskMount,
  type RawStat,
} from './server-monitor-core'
import { isRetryable } from '../ssh-error-types'

const POLL_INTERVAL_MS = 5000
const HISTORY_MAX = 60       // 状态条 sparkline 保留点数（5min @ 5s）
const FAIL_STOP_THRESHOLD = 3
const RETRY_INTERVAL_MS = 15000 // 连续失败停止后，目标会话仍在时后台重试的间隔

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
  let inflight = false
  let activeSshId = ''
  /** 监控目标会话（连续失败停止后仍保留，用于后台重试；stop 时清空） */
  let targetSshId = ''
  /** 连续失败停止后的后台重试定时器（与 5s 轮询互斥） */
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  /** 上一次 CPU 原始计数器（跨轮次差分算使用率） */
  let prevStat: RawStat | null = null

  /** 首个根磁盘（状态条/卡片用） */
  const rootDisk = computed<DiskMount | null>(() => {
    const d = current.value?.disks
    if (!d?.length) return null
    return d.find(x => x.mount === '/') || d[0]
  })

  function pushHistory(arr: number[], v: number) {
    arr.push(v)
    if (arr.length > HISTORY_MAX) arr.shift()
  }

  /** 监控指标异常/选中片段 → 预填 AI 面板（复用 A 的 pendingPrefill 通路） */
  function askAiAboutMetrics(hint: string) {
    const m = current.value
    if (!m) return
    const lines = [
      `请帮我分析这台服务器（${m.host || '未知'}）的当前运行状态：`,
      `CPU 使用率 ${m.cpu.used.toFixed(1)}%（iowait ${m.cpu.iowait.toFixed(1)}%），负载 ${m.load.join(' / ')}`,
      `内存 ${m.mem.percent.toFixed(1)}%（${(m.mem.used / 1024).toFixed(2)}G / ${(m.mem.total / 1024).toFixed(2)}G）`,
    ]
    const rd = rootDisk.value
    if (rd) {
      lines.push(`根分区使用 ${rd.percent}%（${(rd.used / 1024).toFixed(0)}G / ${(rd.total / 1024).toFixed(0)}G）`)
    }
    if (m.procs.length) {
      lines.push('TOP 进程: ' + m.procs.map(p => `${p.name}(pid ${p.pid}, cpu ${p.cpu.toFixed(1)}%)`).join(', '))
    }
    if (m.gpus.length) {
      lines.push('GPU: ' + m.gpus.map(g => `${g.name}(util ${g.utilization.toFixed(0)}%, 显存 ${g.memPercent.toFixed(0)}% ${(g.memUsed / 1024).toFixed(1)}G / ${(g.memTotal / 1024).toFixed(1)}G, ${g.temperature}℃)`).join(', '))
    }
    lines.push(hint)
    const sshAi = useSshAiStore()
    sshAi.askAiAboutSelection(lines.join('\n'))
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

  /** 开始监控指定远程会话（会话变化时由调用方 stop 后重新 start） */
  function start(sshId: string) {
    const session = sshStore.sessions.find(s => s.id === sshId)
    if (!session || session.type !== 'remote') return
    // stop() 负责全部清零，这里不再抄第二份清单 —— 上一版正是在两处各写一份，
    // stop() 那份漏了 lastError/errorCount/prevStat，于是关掉主机后仪表盘仍然
    // 挂着「监控已中断」，而「当前终端没有远程连接」的空态整条不可达。
    stop()
    activeSshId = sshId
    targetSshId = sshId
    polling.value = true
    void sample()
    scheduleNext()
  }

  /** 停表并清空全部读数：唯一的状态清零入口 */
  function stop() {
    polling.value = false
    activeSshId = ''
    targetSshId = ''
    current.value = null
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
    if (retryTimer) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
  }

  return {
    current,
    cpuHistory,
    memHistory,
    loadHistory,
    gpuHist,
    polling,
    lastError,
    start,
    stop,
    askAiAboutMetrics,
  }
})
