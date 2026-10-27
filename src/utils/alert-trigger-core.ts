/**
 * 告警触发诊断 — 纯核心（无 vue / pinia 依赖，独立单测；拆分模式同 server-monitor-core.ts）
 *
 * 指标只存在于渲染层内存（backend/routes 无任何 metrics/monitor 端点），因此告警
 * 判断与冷却全部在这里完成；执行层（composables/useAlertTrigger.ts）只做 watch
 * 接线与 agentStore.runAgent 调用，从而零成本继承 /agent/run 的审批卡、审计与熔断。
 *
 * 规则与每主机开关持久化在 localStorage（键级原子写，无文件落盘，不涉及 tmp+rename
 * 约束）；运行时计数与冷却状态纯内存 —— 无渲染层 = 无轮询 = 无触发源，跨重启持久化
 * 冷却非必需（后端持久化规则留 v2：app_setting，先例 schedule_engine.py db.get_app_setting）。
 *
 * 关键样本契约（对齐 stores/server-monitor-core.ts，集成者合并时需复核）：
 * - computeCpuDiff 首轮/计数器未前进返回全 0（server-monitor-core.ts:603-604）→
 *   cpu/iowait 的全 0 样本不计入连续越限（不升不降），否则「CPU<10%」类低阈值规则
 *   会把无效差分当成连续越限；
 * - 采集连败转后台重试期间（server-monitor.ts enterRetryMode，polling=false）current
 *   停更，样本冻结不喂计数，连续计数以最后一次成功样本为准，不因时间流逝虚增。
 */
import type { ServerMetrics } from '@/stores/server-monitor-core'

// ── 规则模型 ──

export const ALERT_METRICS = ['cpu', 'iowait', 'mem', 'load1', 'disk', 'gpu_mem', 'swap'] as const
export type AlertMetric = (typeof ALERT_METRICS)[number]

export const ALERT_OPS = ['>', '>=', '<', '<='] as const
export type AlertOp = (typeof ALERT_OPS)[number]

/** localStorage 键（v1：预置规则落盘一份，用户手改生效于下次加载；无规则编辑 UI） */
export const ALERT_CONFIG_STORAGE_KEY = 'ckun.alertTrigger.rules.v1'

/** 诊断 task 文本硬上限。AgentRunRequest.task 上限 50000（routes/agent.py:398），
 *  这里远低于它；只塞主机名+触发规则+越限指标+mem/swap+前 3 磁盘+GPU 摘要，不塞 ports/procs。 */
export const ALERT_TASK_MAX_CHARS = 4000

/** 历史序列注入诊断文本的尾部点数（历史各 ≤60 点，全塞会膨胀） */
export const ALERT_HISTORY_TAIL = 12

/** consecutiveN 合法区间：1 个样本即可触发 ~ 720 个（= 1 小时 @ 5s） */
const RULE_N_MAX = 720
/** cooldownMinutes 合法区间：1 分钟 ~ 1 天 */
const COOLDOWN_MIN = 1
const COOLDOWN_MAX = 1440
const COOLDOWN_DEFAULT = 30
const TRUNCATE_MARKER = '\n…（指标已截断）'

export interface AlertRule {
  id: string
  metric: AlertMetric
  op: AlertOp
  threshold: number
  consecutiveN: number
  enabled: boolean
  /** 仅 metric==='disk'：'any' = 任一挂载点；'mount:<路径>' = 指定挂载点（disks ≤8 块） */
  diskScope: 'any' | `mount:${string}`
}

export interface AlertTriggerConfig {
  version: 1
  rules: AlertRule[]
  /** 按主机开关（键=主机名；缺省 = 启用，值 false = 该主机关闭告警） */
  hostOverrides: Record<string, boolean>
  cooldownMinutes: number
}

export const METRIC_LABELS: Record<AlertMetric, string> = {
  cpu: 'CPU 使用率',
  iowait: 'CPU iowait',
  mem: '内存使用率',
  load1: '负载(1分钟)',
  disk: '磁盘使用率',
  gpu_mem: 'GPU 显存使用率',
  swap: 'Swap 使用率',
}

/** 百分比类指标（load1 是绝对值，不带 %） */
function metricHasPercent(metric: AlertMetric): boolean {
  return metric !== 'load1'
}

/** 规则的人类可读描述（诊断文本与 toast 共用口径） */
export function describeRule(rule: AlertRule): string {
  const unit = metricHasPercent(rule.metric) ? '%' : ''
  const scope = rule.metric === 'disk'
    ? (rule.diskScope === 'any' ? '（任一挂载点）' : `（挂载点 ${rule.diskScope.slice('mount:'.length)}）`)
    : ''
  return `${METRIC_LABELS[rule.metric]} ${rule.op} ${rule.threshold}${unit}${scope}`
}

// ── 预置规则（设计 §1.2：cpu>90%×6、mem>90%×12、disk>92%×24 任一挂载点） ──

export function defaultAlertConfig(): AlertTriggerConfig {
  return {
    version: 1,
    rules: [
      { id: 'cpu_high', metric: 'cpu', op: '>', threshold: 90, consecutiveN: 6, enabled: true, diskScope: 'any' },
      { id: 'mem_high', metric: 'mem', op: '>', threshold: 90, consecutiveN: 12, enabled: true, diskScope: 'any' },
      { id: 'disk_high', metric: 'disk', op: '>', threshold: 92, consecutiveN: 24, enabled: true, diskScope: 'any' },
    ],
    hostOverrides: {},
    cooldownMinutes: COOLDOWN_DEFAULT,
  }
}

// ── 校验（版本化 + 损坏兜底重置） ──

function sanitizeRule(raw: unknown): AlertRule | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const id = typeof r.id === 'string' ? r.id.trim().slice(0, 64) : ''
  const metric = ALERT_METRICS.find(m => m === r.metric)
  const op = ALERT_OPS.find(o => o === r.op)
  const threshold = typeof r.threshold === 'number' && Number.isFinite(r.threshold) ? r.threshold : NaN
  const n = typeof r.consecutiveN === 'number' && Number.isFinite(r.consecutiveN) ? Math.round(r.consecutiveN) : NaN
  // id/metric/op/threshold 无法修复 → 丢弃该条；consecutiveN 只有下限不可修复（0/负数），
  // 上限夹取即可（用户手改超界的善意兜底）
  if (!id || !metric || !op || !Number.isFinite(threshold) || !Number.isFinite(n) || n < 1) return null
  let diskScope: AlertRule['diskScope'] = 'any'
  if (typeof r.diskScope === 'string' && (r.diskScope === 'any' || r.diskScope.startsWith('mount:'))) {
    diskScope = r.diskScope as AlertRule['diskScope']
  }
  return {
    id,
    metric,
    op,
    threshold,
    consecutiveN: Math.min(n, RULE_N_MAX),
    // 缺省视为启用：手写规则不带 enabled 字段也能生效
    enabled: r.enabled !== false,
    diskScope,
  }
}

function sanitizeOverrides(raw: unknown): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  if (!raw || typeof raw !== 'object') return out
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'boolean') out[k] = v
  }
  return out
}

/** 未知形状 → 可用配置。整体损坏（版本不符/结构非法）回落预置三条；
 *  rules 数组内个别条目非法则丢弃、合法的保留（显式清空 rules 为合法状态）。 */
export function sanitizeAlertConfig(raw: unknown): AlertTriggerConfig {
  const fallback = defaultAlertConfig()
  if (!raw || typeof raw !== 'object') return fallback
  const o = raw as Record<string, unknown>
  if (o.version !== 1) return fallback
  const rules = Array.isArray(o.rules)
    ? o.rules.map(sanitizeRule).filter((r): r is AlertRule => r !== null)
    : fallback.rules
  const cd = typeof o.cooldownMinutes === 'number' && Number.isFinite(o.cooldownMinutes)
    ? Math.round(o.cooldownMinutes)
    : COOLDOWN_DEFAULT
  return {
    version: 1,
    rules,
    hostOverrides: sanitizeOverrides(o.hostOverrides),
    cooldownMinutes: Math.min(COOLDOWN_MAX, Math.max(COOLDOWN_MIN, cd)),
  }
}

// ── localStorage 读写 ──

/** 读写只需 KV 两方法（浏览器 Storage / 测试注入 / node 后备同一结构） */
export interface KvStorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** 无 window（node 测试环境）的后备存储；测试也用它预置规则 */
export const alertMemoryStorage: KvStorageLike = (() => {
  const m = new Map<string, string>()
  return {
    getItem: k => m.get(k) ?? null,
    setItem: (k, v) => { m.set(k, v) },
  }
})()

function defaultStorage(): KvStorageLike {
  try {
    if (typeof localStorage !== 'undefined') return localStorage
  } catch { /* 部分嵌入环境读 localStorage 即抛 */ }
  return alertMemoryStorage
}

/** 读取规则配置：键不存在 / JSON 损坏 / 版本不符 → 重置为预置三条（损坏兜底重置） */
export function loadAlertConfig(storage: KvStorageLike = defaultStorage()): AlertTriggerConfig {
  try {
    const raw = storage.getItem(ALERT_CONFIG_STORAGE_KEY)
    if (!raw) return defaultAlertConfig()
    return sanitizeAlertConfig(JSON.parse(raw) as unknown)
  } catch {
    // 损坏兜底要落到盘上：只重置内存的话，坏 JSON 每次挂载都会再炸一遍
    const fallback = defaultAlertConfig()
    saveAlertConfig(fallback, storage)
    return fallback
  }
}

/** 写回配置。失败（配额/隐私模式）返回 false 由调用方决定是否提示，不抛出。 */
export function saveAlertConfig(config: AlertTriggerConfig, storage: KvStorageLike = defaultStorage()): boolean {
  try {
    storage.setItem(ALERT_CONFIG_STORAGE_KEY, JSON.stringify(sanitizeAlertConfig(config)))
    return true
  } catch {
    return false
  }
}

// ── 运行时状态机（纯内存，键 = host + ruleId） ──

export interface AlertRuntime {
  /** 连续越限计数（键 → 次数） */
  counts: Map<string, number>
  /** 冷却截止时刻（epoch ms；以触发时刻起算，run 失败照常计时防重置风暴窗口） */
  cooldownUntil: Map<string, number>
  /** 触发在飞（建会话 + runAgent 组装期间），防同 tick 重复触发 */
  inFlight: Set<string>
}

export function runtimeKey(host: string, ruleId: string): string {
  return `${host}\n${ruleId}`
}

export function createAlertRuntime(): AlertRuntime {
  return { counts: new Map(), cooldownUntil: new Map(), inFlight: new Set() }
}

/** stop()/切会话：计数与冷却一并清零（设计 §1.2） */
export function resetAlertRuntime(rt: AlertRuntime): void {
  rt.counts.clear()
  rt.cooldownUntil.clear()
  rt.inFlight.clear()
}

export function setInFlight(rt: AlertRuntime, host: string, ruleId: string, v: boolean): void {
  const key = runtimeKey(host, ruleId)
  if (v) rt.inFlight.add(key)
  else rt.inFlight.delete(key)
}

/** 触发即落冷却（必须在任何异步步骤之前调用）：计数清零，冷却以 now 起算 */
export function markTriggered(rt: AlertRuntime, host: string, ruleId: string, now: number, cooldownMinutes: number): void {
  const key = runtimeKey(host, ruleId)
  rt.cooldownUntil.set(key, now + cooldownMinutes * 60_000)
  rt.counts.set(key, 0)
}

export interface RuleEvaluation {
  rule: AlertRule
  /** 本样本下该指标的值；null = 规则在本样本不适用（swap 无 / 挂载点缺失 / 无 GPU / 无磁盘） */
  value: number | null
  breached: boolean
  /** 全 0 CPU 差分（computeCpuDiff 首轮/计数器未前进契约）→ cpu/iowait 本样本无效，不参与计数 */
  validSample: boolean
}

function compare(op: AlertOp, value: number, threshold: number): boolean {
  switch (op) {
    case '>': return value > threshold
    case '>=': return value >= threshold
    case '<': return value < threshold
    case '<=': return value <= threshold
  }
}

/** 单样本评估：按指标取值域对齐 ServerMetrics 形态取数，不改变任何状态。
 *  enabled:false 的规则视同不适用（value=null → processSample 计数归零）：
 *  禁用即退出评估且不留陈旧计数，重新启用后需重新累积 N 次。 */
export function evaluateRules(rules: readonly AlertRule[], m: ServerMetrics): RuleEvaluation[] {
  // computeCpuDiff 契约：首轮/计数器未前进 → {used:0, iowait:0}。全 0 即无效差分
  const cpuDiffInvalid = m.cpu.used === 0 && m.cpu.iowait === 0
  return rules.map((rule) => {
    if (!rule.enabled) return { rule, value: null, breached: false, validSample: true }
    let value: number | null = null
    switch (rule.metric) {
      case 'cpu': value = m.cpu.used; break
      case 'iowait': value = m.cpu.iowait; break
      case 'mem': value = m.mem.percent; break
      case 'load1': value = m.load[0]; break
      case 'disk':
        if (rule.diskScope.startsWith('mount:')) {
          const target = rule.diskScope.slice('mount:'.length)
          value = m.disks.find(d => d.mount === target)?.percent ?? null
        } else {
          value = m.disks.length > 0 ? Math.max(...m.disks.map(d => d.percent)) : null
        }
        break
      case 'gpu_mem':
        value = m.gpus.length > 0 ? Math.max(...m.gpus.map(g => g.memPercent)) : null
        break
      case 'swap':
        // 设计明确：swap.total=0（无交换分区）时规则跳过
        value = m.swap.total > 0 ? (m.swap.used / m.swap.total) * 100 : null
        break
    }
    const validSample = rule.metric === 'cpu' || rule.metric === 'iowait' ? !cpuDiffInvalid : true
    // NaN 比较恒 false：脏数据自然 fail-closed（不触发）
    const breached = validSample && value !== null && compare(rule.op, value, rule.threshold)
    return { rule, value, breached, validSample }
  })
}

/**
 * 喂一次样本：更新各规则连续计数，返回本 tick 应触发的规则（count≥N 且不在冷却/在飞）。
 *
 * 计数规则：
 * - 全 0 CPU 差分样本 → 跳过（计数不升不降，冻结）；
 * - 规则不适用/禁用（value=null）→ 连续链断开，计数归零（fail-closed：绝不拿陈旧计数触发）；
 * - 越限 → +1；未越限 → 归零。
 * 冷却判断只读 rt.cooldownUntil（markTriggered 落冷却时已按触发时刻算好截止），
 * 本函数不感知冷却时长。触发候选只在这里产生，真正的 runAgent 发起（含
 * isRunning/后端就绪拦截）在执行层。
 */
export function processSample(
  rt: AlertRuntime,
  host: string,
  evals: readonly RuleEvaluation[],
  now: number,
): AlertRule[] {
  const fired: AlertRule[] = []
  for (const ev of evals) {
    const key = runtimeKey(host, ev.rule.id)
    if (!ev.validSample) continue
    if (ev.value === null) {
      rt.counts.set(key, 0)
      continue
    }
    const next = ev.breached ? (rt.counts.get(key) ?? 0) + 1 : 0
    rt.counts.set(key, next)
    const cooling = (rt.cooldownUntil.get(key) ?? 0) > now
    if (next >= ev.rule.consecutiveN && !cooling && !rt.inFlight.has(key)) fired.push(ev.rule)
  }
  return fired
}

export function isHostEnabled(config: AlertTriggerConfig, host: string): boolean {
  return config.hostOverrides[host] !== false
}

// ── 诊断 task 文本与 system prompt ──

/** 诊断 run 的 system prompt：说明来源、只读定位、审批卡语义与 600s 兜底（设计 §4.2） */
export const ALERT_SYSTEM_PROMPT = [
  '这是一次由服务器指标告警自动发起的诊断任务（非用户手动提问），用户可能不在场。',
  '请先只读定位：优先用只读命令查看进程、内存、磁盘 IO、网络与日志等现场，判断最可能的原因与影响面。',
  '然后产出明确结论与建议的修复命令清单，逐条说明用途与风险。',
  '任何会产生变更的命令都不要静默执行：系统会弹出审批卡由用户裁决；若审批卡长时间无人处理，后端将按拒绝放行继续（这是预期行为，请在结论中给出被拒后的替代建议）。',
  '若指标显示告警为瞬时抖动或已回落，请如实说明并给出观察建议，不要为了「做点什么」而发起变更。',
].join('\n')

export interface DiagnosisTaskInput {
  host: string
  rule: AlertRule
  metrics: ServerMetrics
  cpuHistory: readonly number[]
  memHistory: readonly number[]
  /** epoch ms；测试注入以获得确定输出 */
  now?: number
}

function fmtNum(v: number): string {
  return String(Math.round(v * 10) / 10)
}

/** 触发时刻快照 → 诊断 task 文本（纯函数，硬上限 ALERT_TASK_MAX_CHARS，超长截断） */
export function buildDiagnosisTaskText(input: DiagnosisTaskInput): string {
  const { host, rule, metrics: m, cpuHistory, memHistory } = input
  const when = new Date(input.now ?? Date.now()).toISOString()
  const [ev] = evaluateRules([rule], m)
  const triggerValue = ev && ev.value !== null ? fmtNum(ev.value) : '—'

  const lines: string[] = []
  lines.push(`【自动告警诊断】主机 ${host}`)
  lines.push(`触发规则：${describeRule(rule)}，连续 ${rule.consecutiveN} 个采样（5 秒间隔）越限，触发时值 ${triggerValue}`)
  lines.push(`指标快照（${when}）：`)
  lines.push(`- CPU：使用率 ${fmtNum(m.cpu.used)}%，iowait ${fmtNum(m.cpu.iowait)}%`)
  lines.push(`- 负载（1/5/15 分钟）：${fmtNum(m.load[0])} / ${fmtNum(m.load[1])} / ${fmtNum(m.load[2])}`)
  lines.push(`- 内存：${fmtNum(m.mem.percent)}%（已用 ${fmtNum(m.mem.used)}/${fmtNum(m.mem.total)} MB）`)
  if (m.swap.total > 0) {
    lines.push(`- Swap：已用 ${fmtNum(m.swap.used)}/${fmtNum(m.swap.total)} MB`)
  }
  const topDisks = [...m.disks].sort((a, b) => b.percent - a.percent).slice(0, 3)
  if (topDisks.length > 0) {
    const desc = topDisks
      .map(d => `${d.mount} ${fmtNum(d.percent)}%（${fmtNum(d.used)}/${fmtNum(d.total)} MB）`)
      .join('；')
    lines.push(`- 磁盘（按使用率前 ${topDisks.length}）：${desc}`)
  }
  if (m.gpus.length > 0) {
    const desc = m.gpus
      .map(g => `GPU${g.index} ${g.name} 利用率${fmtNum(g.utilization)}% 显存${fmtNum(g.memPercent)}%`)
      .join('；')
    lines.push(`- GPU ${m.gpus.length} 卡：${desc}`)
  } else if (m.gpuError) {
    lines.push(`- GPU 采集异常：${m.gpuError}`)
  }
  const cpuTail = cpuHistory.slice(-ALERT_HISTORY_TAIL)
  if (cpuTail.length > 0) {
    lines.push(`CPU 使用率近 ${cpuTail.length} 点（旧→新）：${cpuTail.map(fmtNum).join(', ')}%`)
  }
  const memTail = memHistory.slice(-ALERT_HISTORY_TAIL)
  if (memTail.length > 0) {
    lines.push(`内存使用率近 ${memTail.length} 点（旧→新）：${memTail.map(fmtNum).join(', ')}%`)
  }
  lines.push('请只读诊断：先判断最可能的原因与影响面，再给出结论与建议的修复命令清单；变更命令不要直接执行，列出后由用户在审批卡裁决。')

  let text = lines.join('\n')
  if (text.length > ALERT_TASK_MAX_CHARS) {
    text = text.slice(0, ALERT_TASK_MAX_CHARS - TRUNCATE_MARKER.length) + TRUNCATE_MARKER
  }
  return text
}
