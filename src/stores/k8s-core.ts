/**
 * K8s 管理 — 纯核心（无 vue/pinia 依赖，可独立单测）
 *
 * 与 docker-core.ts 同构：只放「命令构造 + 输出解析 + 错误归类」这些纯函数，
 * sshExec 编排与状态在 components/ssh/k8s/context.ts。
 *
 * 取数一律 `-o json`，不用 jsonpath / custom-columns。理由不是省事：
 * node 的 Ready 在 status.conditions 数组里（要按 type 挑元素），Pod 的 READY 与
 * 状态列要跟 `kubectl get pods` 同口径（init 容器计数、waiting.reason 决定显示
 * CrashLoopBackOff 而不是 Pending）—— 这两件都需要数组谓词，模板表达式做不到。
 * 代价是响应体比 TSV 大得多，会真撞上单次 1M 字符上限，所以两条兜法：
 *   1. 默认落在一个具体 namespace（不是 -A），按 ns 把体积切开；
 *   2. 解析前先 requireParsableJson —— 截断的 JSON 是「不可解析的东西」，必须报成
 *      「输出被截断，请缩小范围」，绝不能静默变成空列表（那读起来像「这个集群没有
 *      Pod」，是运维场景里最危险的误导）。
 *
 * 每条取数带 --request-timeout=12s，比渲染层 15s 的取数预算短：让 kubectl 自己超时
 * 退出、留下一句能读的 stderr，好过 ssh exec 超时后 exitCode 为 null（什么都不知道）。
 */
import { extractSSHError } from '@/types/ssh-error-types'
import { isOutputTruncated } from '../../shared/exec-outcome-core'

/** K8s 操作错误归类结果：UI 据 message/hint 渲染错误带，retryable 决定是否给重试 */
export interface K8sError {
  code: string
  message: string
  hint?: string
  retryable: boolean
}

// ── 数据类型 ──

export interface NodeRow {
  name: string
  /** 从 node-role.kubernetes.io/* 标签推出的角色；无角色标签时空串 */
  roles: string
  status: string
  ready: boolean
  unschedulable: boolean
  version: string
  internalIp: string
  os: string
  arch: string
  age: string
  /** allocatable（不是 capacity）：调度真正能用的量 */
  cpu: string
  memory: string
  pods: string
  /** kubectl top 富集；没有 metrics-server 时整列缺失（不是 0） */
  cpuUsage?: string
  cpuPerc?: string
  memUsage?: string
  memPerc?: string
}

/** 负载五类合并一张表：kind 决定「期望/就绪」从哪个字段读 */
export interface WorkloadRow {
  kind: string
  name: string
  namespace: string
  /** 就绪/期望，如 "2/3"；CronJob 这类没有就绪计数的留空串 */
  ready: string
  desired: number
  readyCount: number
  status: string
  age: string
  image: string
}

export interface PodRow {
  name: string
  namespace: string
  /** 与 kubectl get pods 的 READY 列同口径 */
  ready: string
  status: string
  phase: string
  restarts: number
  age: string
  ip: string
  node: string
  image: string
  readyCount: number
  totalCount: number
  deleting: boolean
}

export interface ServiceRow {
  kind: string
  name: string
  namespace: string
  type: string
  clusterIp: string
  externalIp: string
  ports: string
  /** Ingress 专用：host/path→svc:port 折叠串；Service 留空 */
  rules: string
  age: string
}

export interface EventRow {
  namespace: string
  /** 对象折叠串，如 Pod/nginx-7d9 */
  object: string
  type: string
  reason: string
  message: string
  count: number
  lastSeen: string
  source: string
}

export interface ResourceDetail {
  kind: string
  name: string
  namespace: string
  labels: Record<string, string>
  conditions: { type: string; status: string; reason: string; message: string }[]
  containers: {
    name: string
    image: string
    init: boolean
    ready: string
    state: string
    restarts: number
    limits: string
    requests: string
  }[]
  owner: string
  events: EventRow[]
  /** 原始 JSON：详情里的「完整 JSON」兜底视图 */
  raw: string
}

/**
 * 行键：忙锁、选中高亮、详情定位三处共用同一个形状。定义只留这一处 ——
 * 视图与页签各写一份模板串时，改一处会让「正在转圈的那一行」和「被选中的那一行」错开。
 */
export const workloadRowKey = (w: Pick<WorkloadRow, 'kind' | 'namespace' | 'name'>): string =>
  `${w.kind}/${w.namespace}/${w.name}`
export const podRowKey = (p: Pick<PodRow, 'namespace' | 'name'>): string => `${p.namespace}/${p.name}`
export const serviceRowKey = (s: Pick<ServiceRow, 'kind' | 'namespace' | 'name'>): string =>
  `${s.kind}/${s.namespace}/${s.name}`
export const nodeRowKey = (n: Pick<NodeRow, 'name'>): string => n.name

/** 详情抽屉的定位三元组：getOneCmd 与 objectEventsCmd 都从这里取 */
export interface DetailTarget { kind: string; name: string; namespace: string }
export const detailOfWorkload = (w: WorkloadRow): DetailTarget => ({ kind: w.kind, name: w.name, namespace: w.namespace })
export const detailOfPod = (p: PodRow): DetailTarget => ({ kind: 'Pod', name: p.name, namespace: p.namespace })
export const detailOfService = (s: ServiceRow): DetailTarget => ({ kind: s.kind, name: s.name, namespace: s.namespace })
export const detailOfNode = (n: NodeRow): DetailTarget => ({ kind: 'Node', name: n.name, namespace: '' })

// ── 名称与命名空间 ──

// K8s 的对象名与命名空间是 DNS-1123（只含小写字母、数字、- 与 .），不含空格与引号。
// 因此「过得了校验就出不了 shell 边界」。过不了就是数据不对，必须当场讲清楚，
// 而不是转义后发出去、换一个「什么都没有」的空列表 —— 空列表在运维面上是谎言。
const DNS1123_RE = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/

export function validateObjectName(value: string, what = '资源名'): string | null {
  const v = String(value ?? '').trim()
  if (!v) return `${what}不能为空`
  if (v.length > 253) return `${what}过长（最多 253 字符）`
  if (!DNS1123_RE.test(v)) return `${what}「${v}」不是合法的 Kubernetes 名称（只允许小写字母、数字、- 和 .）`
  return null
}

/**
 * 动作必须落在具体 ns 上，而且只能从行数据里取：列表一律 -A，界面上没有「当前
 * 命名空间」这个状态，所以动作的目标 ns 与用户看到的那一行天然同源 —— 不存在
 * 「在全部视图里点了行、却按某个下拉的值去改」这种错法。
 */
export function assertConcreteNs(ns: string): string | null {
  return validateObjectName(ns, '命名空间')
}

/** 校验通过就返回可安全插入的 ns 片段，否则抛（调用方是 UI 动作，抛错即拒绝下发） */
function scoped(ns: string): string {
  const err = assertConcreteNs(ns)
  if (err) throw new Error(err)
  return ` -n ${String(ns).trim()}`
}

export function validateReplicas(input: string | number): string | null {
  const raw = String(input ?? '').trim()
  if (!/^\d+$/.test(raw)) return '副本数只能是非负整数'
  if (Number(raw) > 999) return '副本数上限 999'
  return null
}

// ── 采集命令 ──

export const REQ_TIMEOUT_SEC = 12
const REQ = `--request-timeout=${REQ_TIMEOUT_SEC}s`

/** 合并一次调用取五类负载；集群缺某类时整条失败，由 K8S_MISSING_KIND 说明 */
const WORKLOAD_KINDS = ['deployments', 'statefulsets', 'daemonsets', 'jobs', 'cronjobs']

/**
 * 列表一律 -A：命名空间选择器只是对已取回行的客户端收窄，不构成第二种取数范围。
 * 代价是 pods / events 的响应体最大 —— 撞上 1M 时 requireParsableJson 如实抛
 * K8S_TRUNCATED（带可操作建议），不会谎报成「这里没有资源」。
 */
export const CONTEXT_CMD = `kubectl config current-context`
/**
 * 未检测到 k8s 时的现场自检：应用实际以哪个账号、哪个 HOME、哪个 kubectl 执行，
 * KUBECONFIG 指到哪、默认配置文件读不读得到。全部是本地读取，不碰网络。
 * 输出用 key=value 而不是人读表格，解析不依赖列对齐。
 */
export const ENV_PROBE_CMD = [
  'echo "user=$(id -un 2>/dev/null)"',
  'echo "home=$HOME"',
  'echo "kubeconfig=${KUBECONFIG:-}"',
  'echo "kubectl=$(command -v kubectl 2>/dev/null)"',
  'if [ -r "$HOME/.kube/config" ]; then echo "config_file=readable"; else echo "config_file=missing"; fi',
].join('; ')
export const NODES_CMD = `kubectl get nodes ${REQ} -o json`
/** top 是人读表格：列数随版本变（3 或 5），--no-headers 后按空白拆比 -o json 稳 */
export const TOP_NODES_CMD = `kubectl top nodes --no-headers ${REQ}`
export const WORKLOADS_CMD = `kubectl get ${WORKLOAD_KINDS.join(',')} ${REQ} -o json -A`
export const PODS_CMD = `kubectl get pods ${REQ} -o json -A`
export const SERVICES_CMD = `kubectl get services,ingresses ${REQ} -o json -A`

/**
 * 事件默认只取 Warning：Normal 里绝大多数是调度器与拉镜像的成功心跳，全命名空间下
 * 一屏刷不完，也回答不了「为什么红了」；顺带把体积压住（它是最容易顶到 1M 的一类）。
 */
export const EVENTS_CMD = `kubectl get events ${REQ} -o json -A --field-selector type=Warning --sort-by=.lastTimestamp`
export const EVENTS_CMD_WITH_NORMAL = `kubectl get events ${REQ} -o json -A --sort-by=.lastTimestamp`

export function getOneCmd(kind: string, name: string, ns: string): string {
  const err = validateObjectName(name, '资源名')
  if (err) throw new Error(err)
  // 集群级对象（Node / Namespace）没有 ns 可带：行数据给空串，这里就不拼
  return `kubectl get ${shortKind(kind)} ${name}${ns ? ` -n ${ns}` : ''} ${REQ} -o json`
}

/** 详情页 YAML 页签：原文直出，不做结构化投影 */
export function getOneYamlCmd(kind: string, name: string, ns: string): string {
  const err = validateObjectName(name, '资源名')
  if (err) throw new Error(err)
  return `kubectl get ${shortKind(kind)} ${name}${ns ? ` -n ${ns}` : ''} ${REQ} -o yaml`
}

/**
 * 某个对象的关联事件（详情抽屉）。
 * ns 传空 = 集群级对象（Node / Namespace）：它们的 Event 落在 default 命名空间里，
 * 所以这里不能拒绝空值，而是要显式补成 -n default。
 */
export function objectEventsCmd(name: string, kind: string, ns: string): string {
  const err = validateObjectName(name, '资源名')
  if (err) throw new Error(err)
  const where = String(ns ?? '').trim() ? ` -n ${String(ns).trim()}` : ' -n default'
  return `kubectl get events ${REQ}${where} --field-selector involvedObject.name=${name},involvedObject.kind=${kind} --sort-by=.lastTimestamp -o json`
}

/** --previous：崩溃容器的当前日志是空的，上一次死亡那份才是答案 */
export function logsCmd(
  pod: string, ns: string, opts: { container?: string; tail?: number; previous?: boolean } = {},
): string {
  const err = validateObjectName(pod, 'Pod 名')
  if (err) throw new Error(err)
  if (opts.container) {
    const cErr = validateObjectName(opts.container, '容器名')
    if (cErr) throw new Error(cErr)
  }
  const tail = Math.max(1, Math.floor(opts.tail ?? 200))
  return `kubectl logs ${pod}${scoped(ns)}${opts.container ? ` -c ${opts.container}` : ''} --tail=${tail}${opts.previous ? ' --previous' : ''} 2>&1`
}

// ── 四个窄动作（单条命令、显式 ns、名称已校验） ──

export const SCALABLE_KINDS = ['deployments', 'statefulsets']

/** 动作下发前的统一校验：ns 必须具体、名字必须是合法 DNS-1123 */
function actionGuard(name: string, ns: string): void {
  const err = assertConcreteNs(ns) || validateObjectName(name, '资源名')
  if (err) throw new Error(err)
}

export function rolloutRestartCmd(kind: string, name: string, ns: string): string {
  const err = validateObjectName(name, '资源名')
  if (err) throw new Error(err)
  return `kubectl rollout restart ${shortKind(kind)}/${name}${scoped(ns)} ${REQ}`
}

export function scaleCmd(kind: string, name: string, ns: string, replicas: number | string): string {
  const err = validateObjectName(name, '资源名') || validateReplicas(replicas)
  if (err) throw new Error(err)
  const k = shortKind(kind)
  if (!SCALABLE_KINDS.includes(k)) throw new Error(`${kind} 没有副本数可改`)
  return `kubectl scale ${k}/${name}${scoped(ns)} --replicas=${Number(replicas)}`
}

/** 删 Pod 的语义是「换一个」：控制器管理的会立刻重建，裸 Pod 是真没了 —— 确认框必须写清 */
export function deletePodCmd(name: string, ns: string): string {
  actionGuard(name, ns)
  return `kubectl delete pods ${name}${scoped(ns)}`
}

export function deleteJobCmd(name: string, ns: string): string {
  actionGuard(name, ns)
  return `kubectl delete jobs ${name}${scoped(ns)}`
}

/** 确认框按动作给不同的后果说明，避免一句「确定吗」 */
export const ACTION_CONSEQUENCE: Record<string, string> = {
  rolloutRestart: '逐个替换该负载的全部 Pod（滚动，不是一次清空）；不可用时长取决于副本数与就绪探针。',
  scale: '立刻增删 Pod；被缩掉的 Pod 里正在处理的请求会中断。',
  deletePod: '由控制器（Deployment/StatefulSet/DaemonSet/Job）管理的 Pod 会重建一个；裸 Pod 不重建，是真删。',
  deleteJob: '连同该 Job 已生成的 Pod 一起删除，未跑完的批次不再重试。',
}

export function shortKind(kind: string): string {
  const map: Record<string, string> = {
    Deployment: 'deployments', StatefulSet: 'statefulsets', DaemonSet: 'daemonsets',
    Job: 'jobs', CronJob: 'cronjobs', Pod: 'pods', Service: 'services', Ingress: 'ingresses',
    Node: 'nodes', Namespace: 'namespaces', Event: 'events',
  }
  const k = String(kind ?? '').trim()
  if (map[k]) return map[k]
  // 已经是复数短名（deployments/pods/…）时原样传，避免再拼 s
  return /s$/.test(k) ? k : `${k.toLowerCase()}s`
}

// ── 解析底座 ──

interface K8sObject {
  kind?: string
  metadata?: Record<string, unknown>
  spec?: Record<string, unknown>
  status?: Record<string, unknown>
  [key: string]: unknown
}

function asRec(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
}
function asArr(v: unknown): unknown[] { return Array.isArray(v) ? v : [] }
function asStr(v: unknown): string { return typeof v === 'string' ? v : '' }
/** 标量转文本：数字与布尔也要出字（端口号、副本数都是数字，asStr 会把它们吞成空串） */
function asText(v: unknown): string {
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return ''
}
function asNum(v: unknown): number | undefined {
  if (typeof v === 'number') return Number.isFinite(v) ? v : undefined
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return undefined
}
const metaOf = (o: K8sObject): Record<string, unknown> => asRec(o.metadata)
const specOf = (o: K8sObject): Record<string, unknown> => asRec(o.spec)
const statusOf = (o: K8sObject): Record<string, unknown> => asRec(o.status)

function k8sErr(code: string, message: string, hint: string, retryable: boolean): K8sError {
  return { code, message, hint, retryable }
}

/**
 * 取 items（kubectl 的 list 响应恒有 items；单对象 get 没有）。
 * 截断与不可解析都如实抛出，绝不返回空数组冒充「这里没有资源」。
 */
export function requireParsableJson(raw: string): K8sObject[] {
  const text = String(raw ?? '').trim()
  if (!text) return []
  if (isOutputTruncated(raw)) {
    throw k8sErr(
      'K8S_TRUNCATED',
      '返回内容超过单次 1M 字符上限，这份列表不完整',
      '列表是全命名空间取的，最容易顶到上限：用搜索框按名称/命名空间收窄，事件保持「只看 Warning」，或到详情里逐个对象看',
      false,
    )
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw k8sErr(
      'K8S_BAD_JSON',
      'kubectl 的输出不是可解析的 JSON',
      '常见于该版本的 kubectl 不认这个输出格式，或命令被登录 shell 的别名改写',
      false,
    )
  }
  if (Array.isArray(parsed)) return parsed as K8sObject[]
  const rec = asRec(parsed)
  if (rec.items !== undefined) return asArr(rec.items) as K8sObject[]
  return Object.keys(rec).length ? [rec as K8sObject] : []
}

// K8s quantity 是「数字 + 后缀」：4 / 500m / 3910276Ki / 1e3。两套进位混用是真实存在的
// （Gi 与 G 差 7.4%），所以按官方口径分开算：Ki/Mi/Gi/Ti/Pi 是 1024 进制，
// n/u/m/k/M/G/T/P/E 是 10 进制。认不出后缀就返回 undefined，不做看似合理的猜测。
const BINARY_SUFFIX: Record<string, number> = {
  Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4, Pi: 1024 ** 5,
}
const DECIMAL_SUFFIX: Record<string, number> = {
  n: 1e-9, u: 1e-6, m: 1e-3, '': 1, k: 1e3, K: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15, E: 1e18,
}

/** → 基准量（内存=字节，CPU=核数）。undefined 表示读不懂，调用方留「—」 */
export function quantityToBase(q: unknown): number | undefined {
  const s = asText(q).trim()
  if (!s) return undefined
  // 数字部分允许 1e3 这种指数写法（K8s quantity 认它），后缀单独一段
  const m = /^([0-9.]+(?:[eE][-+]?[0-9]+)?)([A-Za-z]*)$/.exec(s)
  if (!m) return undefined
  const n = Number(m[1])
  if (!Number.isFinite(n)) return undefined
  const suffix = m[2]
  if (!suffix) return n
  if (BINARY_SUFFIX[suffix]) return n * BINARY_SUFFIX[suffix]
  if (DECIMAL_SUFFIX[suffix] !== undefined) return n * DECIMAL_SUFFIX[suffix]
  return undefined
}

export function formatK8sBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) {
    const gib = bytes / 1024 ** 3
    return `${gib >= 10 ? Math.round(gib) : gib.toFixed(1)}Gi`
  }
  if (bytes >= 1024 ** 2) return `${Math.round(bytes / 1024 ** 2)}Mi`
  return `${Math.max(1, Math.round(bytes / 1024))}Ki`
}

/** CPU 数量 → 核数文本：'500m' → 0.5，'4' → 4，'2000000n' → 0.2 */
export function cpuText(q: unknown): string {
  const cores = quantityToBase(q)
  if (cores === undefined) return asText(q)
  const rounded = Math.round(cores * 1000) / 1000
  return String(rounded)
}

/** age：与 kubectl 同口径的极简相对时长（40s / 12m / 3h / 5d） */
export function ageText(iso: string, now = Date.now()): string {
  const t = Date.parse(String(iso ?? '') || '')
  if (!Number.isFinite(t)) return ''
  const s = Math.max(0, Math.floor((now - t) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const d = Math.floor(h / 24)
  if (d < 365) return `${d}d`
  return `${Math.floor(d / 365)}y`
}

/** 时刻列：统一成本地 HH:mm:ss；读不出时刻就原样显示（不谎报成空） */
export function timeText(iso: string): string {
  const raw = String(iso ?? '')
  const t = Date.parse(raw)
  if (!Number.isFinite(t)) return raw
  return new Date(t).toLocaleTimeString()
}

function conditionOf(o: K8sObject, type: string): Record<string, unknown> | null {
  for (const c of asArr(statusOf(o).conditions)) {
    const rec = asRec(c)
    if (asStr(rec.type) === type) return rec
  }
  return null
}

/** node-role.kubernetes.io/<role> 的值恒为空串，存在即角色；worker 排最后（多数节点都是它） */
export function nodeRoles(labels: Record<string, unknown>): string {
  const out: string[] = []
  for (const key of Object.keys(labels || {})) {
    const prefix = 'node-role.kubernetes.io/'
    if (!key.startsWith(prefix)) continue
    const role = key.slice(prefix.length)
    if (role && !out.includes(role)) out.push(role)
  }
  if (!out.length) return ''
  const others = out.filter(r => r !== 'worker')
  const workers = out.filter(r => r === 'worker')
  return [...others, ...workers].join(',')
}

export function parseNodes(raw: string): NodeRow[] {
  return requireParsableJson(raw).map(o => {
    const m = metaOf(o)
    const st = statusOf(o)
    const sp = specOf(o)
    const readyCond = conditionOf(o, 'Ready')
    const ready = asStr(readyCond?.status) === 'True'
    const unschedulable = sp.unschedulable === true
    const info = asRec(st.nodeInfo)
    const allocatable = asRec(st.allocatable)
    const addresses = asArr(st.addresses).map(asRec)
    const addr = (type: string): string => asText(addresses.find(a => asStr(a.type) === type)?.address)
    const memBytes = quantityToBase(allocatable.memory)
    return {
      name: asStr(m.name),
      roles: nodeRoles(asRec(m.labels)),
      status: ready
        ? (unschedulable ? 'Ready,SchedulingDisabled' : 'Ready')
        : (unschedulable ? 'NotReady,SchedulingDisabled' : 'NotReady'),
      ready,
      unschedulable,
      version: asStr(info.kubeletVersion),
      internalIp: addr('InternalIP'),
      os: asStr(info.operatingSystem),
      arch: asStr(info.architecture),
      age: ageText(asStr(m.creationTimestamp)),
      cpu: cpuText(allocatable.cpu),
      memory: memBytes ? formatK8sBytes(memBytes) : '',
      pods: asText(allocatable.pods),
    }
  })
}

/**
 * kubectl top nodes --no-headers 的表：默认 5 列 NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%，
 * `-v=0` 时退化成 3 列 NAME CPU MEMORY。
 * 失败（多数是没装 metrics-server）时它吐的是一行错误文本，所以逐列验形 ——
 * 不校验就会把「error: Metrics API not available」收成一条 cpu='Metrics' 的假数据。
 */
const QTY_CELL_RE = /^[0-9.]+([eE][-+]?[0-9]+)?(n|u|m|k|M|G|T|P|E)?i?$/
const PERC_CELL_RE = /^[0-9.]+%$/

export interface NodeUsage { cpu: string; cpuPerc: string; mem: string; memPerc: string }

export function parseTopNodes(raw: string): Map<string, NodeUsage> {
  const out = new Map<string, NodeUsage>()
  for (const line of String(raw ?? '').split('\n')) {
    const cells = line.replace(/\r$/, '').trim().split(/\s+/)
    if (cells.length < 3 || !cells[0] || cells[0] === 'Name') continue
    if (cells.length >= 5) {
      if (!QTY_CELL_RE.test(cells[1]) || !PERC_CELL_RE.test(cells[2]) || !QTY_CELL_RE.test(cells[3])) continue
      out.set(cells[0], { cpu: cells[1], cpuPerc: cells[2], mem: cells[3], memPerc: cells[4] })
    } else {
      if (!QTY_CELL_RE.test(cells[1]) || !QTY_CELL_RE.test(cells[2])) continue
      out.set(cells[0], { cpu: cells[1], cpuPerc: '', mem: cells[2], memPerc: '' })
    }
  }
  return out
}

/** top 结果并回节点行：命中才填，没装 metrics-server 时用量列保持缺省（不是 0） */
export function mergeNodeUsage(rows: NodeRow[], usage: Map<string, NodeUsage>): NodeRow[] {
  if (!usage.size) return rows
  return rows.map(r => {
    const hit = usage.get(r.name)
    if (!hit) return r
    return { ...r, cpuUsage: hit.cpu, cpuPerc: hit.cpuPerc, memUsage: hit.mem, memPerc: hit.memPerc }
  })
}

interface WorkloadCounts { desired?: number; ready?: number }

/** 每类负载的「期望/就绪」字段路径不同，按 kind 分派；判不出来就留空，不猜 */
function workloadCounts(kind: string, sp: Record<string, unknown>, st: Record<string, unknown>): WorkloadCounts {
  if (kind === 'Deployment' || kind === 'StatefulSet') {
    return { desired: asNum(sp.replicas) ?? 0, ready: asNum(st.readyReplicas) }
  }
  if (kind === 'DaemonSet') {
    return { desired: asNum(st.desiredNumberScheduled), ready: asNum(st.numberReady) }
  }
  if (kind === 'Job') {
    return { desired: asNum(sp.completions) ?? 1, ready: asNum(st.succeeded) }
  }
  return {}
}

function workloadImage(sp: Record<string, unknown>): string {
  const direct = asArr(asRec(asRec(sp.template).spec).containers)
    .map(c => asStr(asRec(c).image)).filter(Boolean)
  if (direct.length) return direct[0]
  const jobSpec = asRec(asRec(sp.jobTemplate).spec)
  const fromJob = asArr(asRec(asRec(jobSpec.template).spec).containers)
    .map(c => asStr(asRec(c).image)).filter(Boolean)
  return fromJob[0] || ''
}

function workloadStatus(kind: string, o: K8sObject, sp: Record<string, unknown>, c: WorkloadCounts): string {
  if (kind === 'CronJob') {
    const schedule = asText(sp.schedule)
    if (sp.suspend === true) return 'Suspended'
    const last = asStr(o.status?.lastScheduleTime)
    return schedule ? `${schedule} · ${last ? `上次 ${ageText(last)}` : '尚未触发'}` : 'CronJob'
  }
  if (kind === 'Job') {
    if (conditionOf(o, 'Failed')) return 'Failed'
    if (conditionOf(o, 'Complete')) return 'Completed'
    return 'Running'
  }
  const desired = c.desired ?? 0
  const ready = c.ready
  if (conditionOf(o, 'ReplicaFailure')) return 'Failure'
  if (desired === 0) return 'Scaled to 0'
  if (ready !== undefined && ready === desired) return 'OK'
  if (conditionOf(o, 'Progressing')?.reason === 'Progressing') return 'Rolling'
  return 'Degraded'
}

export function parseWorkloads(raw: string): WorkloadRow[] {
  return requireParsableJson(raw).map(o => {
    const kind = asStr(o.kind)
    const m = metaOf(o)
    const sp = specOf(o)
    const st = statusOf(o)
    const c = workloadCounts(kind, sp, st)
    const ready = c.ready ?? 0
    const desired = c.desired
    return {
      kind,
      name: asStr(m.name),
      namespace: asStr(m.namespace),
      ready: desired === undefined ? '' : `${ready}/${desired}`,
      desired: desired ?? 0,
      readyCount: ready,
      status: workloadStatus(kind, o, sp, c),
      age: ageText(asStr(m.creationTimestamp)),
      image: workloadImage(sp),
    }
  })
}

/**
 * Pod 的 READY 计数与 kubectl 同口径：分母含 init 容器；init 容器以
 * 「terminated 且 exit 0」或 ready=true（init 容器带 restartPolicy=Always 时）计就绪。
 */
export function podReadyCounts(pod: K8sObject): { ready: number; total: number } {
  const sp = specOf(pod)
  const st = statusOf(pod)
  const containers = asArr(sp.containers)
  const initContainers = asArr(sp.initContainers)
  let ready = 0
  for (const item of asArr(st.containerStatuses)) if (asRec(item).ready === true) ready++
  for (const item of asArr(st.initContainerStatuses)) {
    const s = asRec(item)
    const terminated = asRec(asRec(s.state).terminated)
    if (s.ready === true || terminated.exitCode === 0) ready++
  }
  return { ready, total: containers.length + initContainers.length }
}

/**
 * 崩溃循环 / 拉镜像失败这类 waiting reason 才是 Pod 的真实状态，phase 只会停在 Pending。
 * 优先级与 kubectl 一致：Terminating 压过一切，其后取第一个非 PodInitializing 的 reason。
 */
export function podDisplayStatus(pod: K8sObject): { status: string; phase: string; deleting: boolean } {
  const m = metaOf(pod)
  const st = statusOf(pod)
  const phase = asStr(st.phase) || 'Unknown'
  const deleting = Boolean(m.deletionTimestamp)
  const reasons: string[] = []
  for (const list of [st.containerStatuses, st.initContainerStatuses]) {
    for (const item of asArr(list)) {
      const state = asRec(asRec(item).state)
      const waitingReason = asStr(asRec(state.waiting).reason)
      if (waitingReason) reasons.push(waitingReason)
      const terminated = asRec(state.terminated)
      if (asStr(terminated.reason) && asNum(terminated.exitCode) !== 0) reasons.push(asStr(terminated.reason))
    }
  }
  if (deleting) return { status: 'Terminating', phase, deleting }
  const reason = reasons.find(r => r !== 'PodInitializing') || reasons[0] || ''
  if (reason) return { status: reason, phase, deleting }
  if (phase === 'Succeeded') return { status: 'Completed', phase, deleting }
  if (phase === 'Failed') {
    return { status: asStr(st.reason) === 'Evicted' ? 'Evicted' : 'Failed', phase, deleting }
  }
  return { status: phase, phase, deleting }
}

/** 重启次数取各容器的最大值，与 kubectl 的 RESTARTS 列一致 */
export function podRestarts(pod: K8sObject): number {
  const st = statusOf(pod)
  let max = 0
  for (const list of [st.containerStatuses, st.initContainerStatuses]) {
    for (const item of asArr(list)) {
      const n = asNum(asRec(item).restartCount)
      if (n !== undefined && n > max) max = n
    }
  }
  return max
}

export function parsePods(raw: string): PodRow[] {
  return requireParsableJson(raw).map(o => {
    const m = metaOf(o)
    const sp = specOf(o)
    const st = statusOf(o)
    const counts = podReadyCounts(o)
    const ds = podDisplayStatus(o)
    const first = asRec(asArr(sp.containers)[0])
    return {
      name: asStr(m.name),
      namespace: asStr(m.namespace),
      ready: counts.total ? `${counts.ready}/${counts.total}` : '0/0',
      status: ds.status,
      phase: ds.phase,
      restarts: podRestarts(o),
      age: ageText(asStr(m.creationTimestamp)),
      ip: asStr(st.podIP),
      node: asStr(sp.nodeName),
      image: asStr(first.image),
      readyCount: counts.ready,
      totalCount: counts.total,
      deleting: ds.deleting,
    }
  })
}

/**
 * 行的颜色语义 —— 「这一行是不是坏了」只留这一处判据。
 * 页签自己比 status 字符串、状态栏比 isAbnormalPod，两边就会打架：
 * 一个 Running 但探针没过的 Pod 会被涂绿，同时被计数成异常。
 */
export function podTone(p: Pick<PodRow, 'phase' | 'status' | 'restarts' | 'readyCount' | 'totalCount'>): 'ok' | 'warn' | 'bad' {
  if (p.phase === 'Succeeded' || p.status === 'Completed') return 'ok'
  if (p.status === 'Terminating') return 'warn'
  if (p.phase === 'Failed' || p.status === 'Evicted') return 'bad'
  if (p.restarts > 0) return 'bad'
  if (p.phase !== 'Running') return 'warn'
  return p.totalCount > 0 && p.readyCount === p.totalCount ? 'ok' : 'bad'
}

/** 异常判据（对应 docker 的 isAbnormal）：颜色不是 ok 就算异常，计数与置顶共用它 */
export function isAbnormalPod(p: Pick<PodRow, 'phase' | 'status' | 'restarts' | 'readyCount' | 'totalCount'>): boolean {
  return podTone(p) !== 'ok'
}

/** Service/Ingress 合并一张表，kind 决定字段读法 */
export function parseServices(raw: string): ServiceRow[] {
  return requireParsableJson(raw).map(o => {
    const kind = asStr(o.kind)
    const m = metaOf(o)
    const sp = specOf(o)
    const st = statusOf(o)
    const age = ageText(asStr(m.creationTimestamp))
    if (kind === 'Ingress') {
      const rules = asArr(sp.rules).map(r => {
        const rec = asRec(r)
        const host = asStr(rec.host) || '*'
        const paths = asArr(asRec(rec.http).paths).map(p => {
          const prec = asRec(p)
          const backend = asRec(prec.backend)
          const svc = asRec(backend.service)
          const port = asRec(backend.port)
          return `${asStr(prec.path) || '/'}→${asStr(svc.name)}:${asText(port.number) || asText(port.name)}`
        })
        return `${host} ${paths.join(' ')}`
      })
      const lb = asArr(asRec(st.loadBalancer).ingress)
      return {
        kind, name: asStr(m.name), namespace: asStr(m.namespace),
        type: asStr(sp.ingressClassName) || 'Ingress',
        clusterIp: '',
        externalIp: lb.map(x => asText(asRec(x).ip) || asText(asRec(x).hostname)).filter(Boolean).join(','),
        ports: '',
        rules: rules.join(' / '),
        age,
      }
    }
    const ports = asArr(sp.ports).map(p => {
      const rec = asRec(p)
      const nodePort = asNum(rec.nodePort)
      return `${asText(rec.port)}${nodePort ? `:${nodePort}` : ''}/${asStr(rec.protocol) || 'TCP'}`
    })
    const lb = asArr(asRec(st.loadBalancer).ingress)
    const external = [
      ...asArr(sp.externalIPs).map(asText),
      ...lb.map(x => asText(asRec(x).ip) || asText(asRec(x).hostname)),
    ].filter(Boolean)
    const endpoints = asArr(st.endpoints)
    return {
      kind, name: asStr(m.name), namespace: asStr(m.namespace),
      type: asStr(sp.type) || 'ClusterIP',
      clusterIp: asStr(sp.clusterIP) === 'None' ? 'None' : asText(sp.clusterIP),
      externalIp: external.join(','),
      ports: ports.join(','),
      rules: endpoints.length ? `${endpoints.length} 个后端` : '',
      age,
    }
  })
}

function eventObjectOf(o: K8sObject): string {
  const inv = asRec(o.involvedObject)
  const name = asStr(inv.name)
  const kind = asStr(inv.kind)
  return kind ? `${kind}/${name}` : name
}

export function parseEvents(raw: string): EventRow[] {
  // kubectl 已按 lastTimestamp 升序排好（--sort-by），这里只翻成「最近的在上」，
  // 不在本地重排：本地再排一次会让「服务端排序」这个约定变成可选，日后改一处就漂移。
  return requireParsableJson(raw).map(o => {
    const m = metaOf(o)
    return {
      namespace: asStr(m.namespace),
      object: eventObjectOf(o),
      type: asStr(o.type) || 'Normal',
      reason: asStr(o.reason),
      message: asStr(o.message),
      count: asNum(o.count) ?? 1,
      lastSeen: timeText(asStr(o.lastTimestamp) || asStr(o.eventTime) || asStr(m.creationTimestamp)),
      source: asStr(asRec(o.source).component) || asStr(asRec(o.reportingInstance).name),
    }
  }).reverse()
}

// ── 详情 ──

function resourceListText(resources: Record<string, unknown>, key: string): string {
  return Object.entries(asRec(resources[key]))
    .map(([k, v]) => `${k}=${asText(v)}`)
    .join(',')
}

export function parseResourceDetail(raw: string, relatedEventsRaw = ''): ResourceDetail {
  const o = requireParsableJson(raw)[0] || {}
  const m = metaOf(o)
  const sp = specOf(o)
  const st = statusOf(o)
  const statusByName = new Map<string, Record<string, unknown>>()
  for (const item of asArr(st.containerStatuses)) statusByName.set(asStr(asRec(item).name), asRec(item))
  for (const item of asArr(st.initContainerStatuses)) statusByName.set(asStr(asRec(item).name), asRec(item))
  const describe = (item: unknown, init: boolean) => {
    const c = asRec(item)
    const s = asRec(statusByName.get(asStr(c.name)))
    const stateKeys = asRec(s.state)
    const stateType = Object.keys(stateKeys)[0] || ''
    const inner = asRec(stateKeys[stateType])
    const resources = asRec(c.resources)
    const terminated = asRec(stateKeys.terminated)
    const ready = s.ready === true
      ? 'ready'
      : (stateType === 'terminated' && asNum(terminated.exitCode) === 0 ? 'done' : stateType || 'waiting')
    return {
      name: asStr(c.name),
      image: asStr(c.image),
      init,
      ready,
      state: [stateType, asStr(inner.reason), asStr(inner.message)].filter(Boolean).join(' · '),
      restarts: asNum(s.restartCount) ?? 0,
      limits: resourceListText(resources, 'limits'),
      requests: resourceListText(resources, 'requests'),
    }
  }
  const owners = asArr(m.ownerReferences).map(asRec)
  return {
    kind: asStr(o.kind),
    name: asStr(m.name),
    namespace: asStr(m.namespace),
    labels: Object.fromEntries(Object.entries(asRec(m.labels)).map(([k, v]) => [k, asText(v)])),
    conditions: asArr(st.conditions).map(c => {
      const rec = asRec(c)
      return { type: asStr(rec.type), status: asText(rec.status), reason: asStr(rec.reason), message: asStr(rec.message) }
    }),
    containers: [
      ...asArr(sp.containers).map(item => describe(item, false)),
      ...asArr(sp.initContainers).map(item => describe(item, true)),
    ],
    owner: owners.length ? `${asStr(owners[0].kind)}/${asStr(owners[0].name)}` : '',
    events: relatedEventsRaw ? parseEvents(relatedEventsRaw) : [],
    raw,
  }
}

/** Pod 的容器名（logsCmd 选容器用；顺序与 spec.containers 一致，init 在后） */
export function podContainerNames(raw: string): { name: string; init: boolean }[] {
  const sp = specOf(requireParsableJson(raw)[0] || {})
  return [
    ...asArr(sp.containers).map(c => ({ name: asStr(asRec(c).name), init: false })),
    ...asArr(sp.initContainers).map(c => ({ name: asStr(asRec(c).name), init: true })),
  ].filter(x => x.name)
}

// ── 错误归类 ──

/** kubectl 在没有可用 kubeconfig 时回落的默认地址 */
const API_FALLBACK_ADDR = /(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0):8080\b/i

/** 响应体没进 JSON 解码：'<' 是一段网页，其它字符多为压缩/乱码字节 */
const API_REPLY_NOT_JSON = /invalid character .* looking for beginning of value/i

/**
 * 用户看到的措辞：message 说「哪里不对」，hint 说「怎么办」，各留一句人话。
 * kubectl 的日志行、调试开关、RBAC / 证书术语只对排障者有意义，对页面使用者只会读成
 * 「程序坏了」。认不出的失败才保留原始首行 —— 那是在如实说「不知道这是什么」，而不是
 * 编一句像样的解释。
 */
const ERROR_COPY = {
  KUBECTL_NOT_FOUND: {
    message: '未检测到 k8s',
    hint: '这台主机没有安装 kubectl，或当前账号找不到它；装上后再试',
  },
  KUBECONFIG_UNREADABLE: {
    message: '未检测到 k8s',
    hint: '当前 SSH 账号读不到 kubeconfig，或它引用的证书文件；整理成一份自带证书的配置，放到该账号的 ~/.kube/config 后再试',
  },
  K8S_CONTEXT_MISSING: {
    message: '未检测到 k8s',
    hint: '当前 SSH 账号的 kubeconfig 里没有指定集群；先选定一个 context 再试',
  },
  K8S_NOT_DETECTED: {
    message: '未检测到 k8s',
    hint: '当前 SSH 账号没有可用的集群配置；从集群取一份 kubeconfig 放到该账号的 ~/.kube/config 后再试',
  },
  K8S_API_NOT_JSON: {
    message: '集群地址配置有误',
    hint: '这个地址回的是网页，不是 Kubernetes API；核对 kubeconfig 里的 server 地址和端口',
  },
  K8S_NO_CLUSTER: {
    message: '连不上集群',
    hint: '请检查网络，或确认 API server 在运行、端口已放通',
  },
  K8S_AUTH: {
    message: '集群认证失败',
    hint: '登录凭据或证书已失效；重新取一份 kubeconfig 再试',
  },
  K8S_FORBIDDEN: {
    message: '当前账号没有读取权限',
    hint: '需要管理员给这个账号授予查看权限',
  },
  K8S_MISSING_KIND: {
    message: '集群里没有这类资源',
    hint: '例如没有安装 Ingress，或集群版本较旧；其他页签不受影响',
  },
  K8S_NO_METRICS: {
    message: '集群没有安装 metrics-server',
    hint: '实时 CPU / 内存用量需要它；列表照常显示，用量列留空',
  },
  K8S_NOT_FOUND_OBJ: {
    message: '这个资源已经不在了',
    hint: '可能已被删除；刷新列表再试',
  },
} as const

/**
 * 依据 kubectl 的 stderr + 退出码归类，返回 null 表示无 kubectl 层错误。
 * 与 dockerErrorFromResult 同构：exit 0 即成功（kubectl 常把 warning 打到 stderr，
 * 那句不代表失败），非零才继续分类；顺序敏感，最具体的判据在前。
 */
export function k8sErrorFromResult(stderr: string, exitCode: number | null, mergedStdout = ''): K8sError | null {
  if (exitCode === 0) return null
  const e = String(stderr ?? '').trim() || String(mergedStdout ?? '').trim()
  const first = e.split('\n')[0] || ''

  if (exitCode === 127 || /kubectl:.*command not found|command not found: kubectl|No such file or directory.*kubectl/i.test(e)) {
    return { code: 'KUBECTL_NOT_FOUND', ...ERROR_COPY.KUBECTL_NOT_FOUND, retryable: false }
  }
  // kubeconfig 读不到：kubeadm/k3s 上非 root 用户最常见的一条。
  // 「Error in configuration」多是配置里的证书仍是绝对路径、指向别的账号的目录
  // （minikube 的 kubeconfig 就引用 /root/.minikube 下的 client.crt/key）——
  // 把 root 的配置直接拷给别的账号时必然踩到，和「读不到配置文件」同一族。
  if (/error loading config file|Error in configuration:|unable to read (client-cert|client-key|certificate-authority)|\.kube\/config.*(no such file|permission denied)|permission denied.*(admin\.conf|kubelet\.conf|\.kube)/i.test(e)) {
    return { code: 'KUBECONFIG_UNREADABLE', ...ERROR_COPY.KUBECONFIG_UNREADABLE, retryable: false }
  }
  if (/current-context is not set|no configuration has been provided|invalid configuration|empty initial config/i.test(e)) {
    return { code: 'K8S_CONTEXT_MISSING', ...ERROR_COPY.K8S_CONTEXT_MISSING, retryable: false }
  }
  if (/error: Metrics API not available|Metrics API not available|not found: metrics\.k8s\.io/i.test(e)) {
    return { code: 'K8S_NO_METRICS', ...ERROR_COPY.K8S_NO_METRICS, retryable: false }
  }
  if (/Unauthorized|x509|certificate signed by unknown authority|tls: /i.test(e)) {
    return { code: 'K8S_AUTH', ...ERROR_COPY.K8S_AUTH, retryable: false }
  }
  if (/cannot (list|get|watch) resource|is forbidden|error: forbidden/i.test(e)) {
    return { code: 'K8S_FORBIDDEN', ...ERROR_COPY.K8S_FORBIDDEN, retryable: false }
  }
  if (/the server doesn't have a resource type/i.test(e)) {
    return { code: 'K8S_MISSING_KIND', ...ERROR_COPY.K8S_MISSING_KIND, retryable: false }
  }
  if (/the connection to the server .* (was refused|failed)|dial tcp .*connection refused|no such host|was rejected by the server|context deadline exceeded|client\.timeout exceeded|net\.http: request timed out/i.test(e)) {
    // kubectl 找不到可用 kubeconfig 时不说「没配置」，而是连它自己的默认地址
    // http://localhost:8080。落在这里说明这台主机压根没接集群，按「连不上」报会给用户
    // 一个永远点不通的重试。
    if (API_FALLBACK_ADDR.test(e)) {
      return { code: 'K8S_NOT_DETECTED', ...ERROR_COPY.K8S_NOT_DETECTED, retryable: false }
    }
    return { code: 'K8S_NO_CLUSTER', ...ERROR_COPY.K8S_NO_CLUSTER, retryable: true }
  }
  if (/Error from server \(NotFound\)|^error: (no |resource )?".*" not found|not found$/i.test(first)) {
    return { code: 'K8S_NOT_FOUND_OBJ', ...ERROR_COPY.K8S_NOT_FOUND_OBJ, retryable: true }
  }
  if (API_REPLY_NOT_JSON.test(e)) {
    return { code: 'K8S_API_NOT_JSON', ...ERROR_COPY.K8S_API_NOT_JSON, retryable: false }
  }
  if (exitCode !== null && exitCode !== 0) {
    return { code: 'K8S_EXEC_FAILED', message: first || `kubectl 命令失败（退出码 ${exitCode}）`, retryable: true }
  }
  // exit-status 缺失（不知道成败）：不谎报成失败，交上层按 unknown 处理
  if (exitCode === null && e) return { code: 'K8S_UNKNOWN', message: first, retryable: true }
  return null
}

/** 传输层抛错（会话缺失 / 超时等）→ K8sError，与 describeThrownDockerError 同构 */
export function describeThrownK8sError(err: unknown): K8sError {
  const t = extractSSHError(err)
  if (t) {
    return { code: t.code, message: t.message || 'SSH 通道错误', hint: t.detail, retryable: t.retryable }
  }
  const msg = err instanceof Error ? err.message : String(err)
  return { code: 'EXEC_THROWN', message: msg || '命令执行失败', retryable: true }
}

/** 错误对象 → 上浮给错误带的文本（与 docker 的 errText 同口径） */
export const k8sErrText = (e: unknown): string => (e as K8sError)?.message || String(e)

/**
 * 「这台主机根本供不了 kubectl」的那批错误：未安装、kubeconfig 读不到、没有 context、
 * 连不上 API server、鉴权失败。它们会让五个分段同时失败且原因同一个，所以只在页顶
 * 说一次，五个列表各自重复同一句只是噪声。
 */
export const HOST_LEVEL_CODES = [
  'KUBECTL_NOT_FOUND', 'KUBECONFIG_UNREADABLE', 'K8S_CONTEXT_MISSING', 'K8S_NOT_DETECTED',
  'K8S_NO_CLUSTER', 'K8S_API_NOT_JSON', 'K8S_AUTH', 'NO_SESSION', 'EXEC_TIMEOUT', 'SESSION_NOT_FOUND',
]

export function isHostLevelError(code: string | undefined): boolean {
  return !!code && HOST_LEVEL_CODES.includes(code)
}

/**
 * 「这台主机上就没有 k8s」那一族：kubectl 没装、kubeconfig 读不到、没有 context、
 * 连的是 kubectl 自己的回落地址。页顶据此报「未检测到 k8s」而不是「集群出错」。
 * 连不上真地址 / 鉴权失败 / RBAC 无权不在这一族：集群在，只是这台主机读不了它。
 */
export const NOT_DETECTED_CODES = [
  'KUBECTL_NOT_FOUND', 'KUBECONFIG_UNREADABLE', 'K8S_CONTEXT_MISSING', 'K8S_NOT_DETECTED',
]

export function isNotDetectedError(code: string | undefined): boolean {
  return !!code && NOT_DETECTED_CODES.includes(code)
}

/**
 * 页顶那句到底怎么说。K8S_API_NOT_JSON 一个人定不了案：kubectl 找不到配置时会去连
 * 默认的 localhost:8080，那台机器上挂着的 Web 服务同样回一段网页过来——只有 context
 * 说得出口时，才谈得上「集群在、地址配错了」。
 */
export function isNotDetectedState(err: K8sError | undefined, contextName: string): boolean {
  if (!err) return false
  if (isNotDetectedError(err.code)) return true
  return err.code === 'K8S_API_NOT_JSON' && !contextName
}

/**
 * 页顶错误带最终显示的那两行。分类层归类时不知道 context 读没读到，而「回包是网页」
 * 的意思由它决定：读不到 context = 这台主机没有 kubeconfig（kubectl 去连 default 的
 * localhost:8080，那里的 Web 服务回了网页）；读得到 = 配置在，只是 server 地址指错了。
 */
export function hostErrorCopy(
  err: K8sError | undefined, contextName: string,
): { title: string; hint: string } | null {
  if (!err) return null
  if (err.code === 'K8S_API_NOT_JSON' && !contextName) {
    return { title: ERROR_COPY.K8S_NOT_DETECTED.message, hint: ERROR_COPY.K8S_NOT_DETECTED.hint }
  }
  return { title: err.message, hint: err.hint || '' }
}

/** 未检测到 k8s 时的执行现场（ENV_PROBE_CMD 的输出） */
export interface K8sEnvProbe {
  user: string
  home: string
  /** 非空说明 KUBECONFIG 被显式指向了别处 */
  kubeconfigEnv: string
  kubectl: string
  configReadable: boolean
}

export function parseEnvProbe(raw: string): K8sEnvProbe {
  const read = (key: string): string =>
    new RegExp(`^${key}=(.*)$`, 'm').exec(raw)?.[1].trim() || ''
  return {
    user: read('user'),
    home: read('home'),
    kubeconfigEnv: read('kubeconfig'),
    kubectl: read('kubectl'),
    configReadable: read('config_file') === 'readable',
  }
}

/**
 * 把现场翻成一句人话。用户在自己终端里跑得通、页面却读不到，原因通常就三样：
 * 执行账号不同、KUBECONFIG 指到别处、kubectl 不在 exec 的 PATH 里 —— 直接说出来，
 * 不让用户在两套 shell 之间猜。
 */
export function envProbeNote(p: K8sEnvProbe | null): string {
  if (!p) return ''
  const who = p.user ? `应用使用的 SSH 账号 ${p.user}（HOME=${p.home || '?'}）` : '应用使用的 SSH 账号'
  if (!p.kubectl) return `${who}的 PATH 里找不到 kubectl。`
  if (p.kubeconfigEnv) {
    return `${who}的 KUBECONFIG 指向 ${p.kubeconfigEnv}，那里读不到可用的集群配置。`
  }
  if (p.configReadable) {
    return `${who}能读到 ~/.kube/config，但里面没有可用的集群配置（证书文件可能不在这个账号下）。`
  }
  return `${who}下没有可读的 ~/.kube/config。`
}

/**
 * 渲染层的兜底闸门。主进程的 exec 自己会超时，但 IPC 回包一旦丢失，渲染层这边的
 * await 就再也不会结算——表现是页面永远停在「正在经 SSH 会话执行 …」，连刷新都被
 * loading 禁用。比主进程预算多 3 秒，正常路径永远轮不到它。
 */
export function withExecDeadline<T>(p: Promise<T>, timeoutSec: number): Promise<T> {
  const deadlineSec = timeoutSec + 3
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`命令 ${deadlineSec} 秒未返回`)), deadlineSec * 1000)
    p.then(
      (v) => { clearTimeout(timer); resolve(v) },
      (e) => { clearTimeout(timer); reject(e) },
    )
  })
}

/** 任何抛出的东西 → K8sError（requireParsableJson 抛的是纯对象，不是 Error） */
export function toK8sError(e: unknown): K8sError {
  const k = e as K8sError
  if (k && typeof k === 'object' && typeof k.code === 'string') return k
  if (e instanceof Error) return { code: 'EXEC_THROWN', message: e.message, retryable: true }
  return { code: 'EXEC_THROWN', message: String(e), retryable: true }
}
