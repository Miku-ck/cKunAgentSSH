/**
 * Docker 管理 — 纯核心（无 vue/pinia 依赖，可独立单测）
 *
 * 依托远程 SSH 会话执行 docker 命令：这里只放「命令构造 + 输出解析 + 错误归类 +
 * shell 引用」这些纯函数，与 server-monitor-core.ts 同构。真正的 sshExec 编排与
 * 状态管理在 DockerView.vue 里做。
 *
 * 取数约定：docker 各 list 子命令用 `--format '{{printf "%s\t%s" ...}}'` 输出，
 * Go 模板的 printf 会把 \t 渲染成真实制表符（而非字面反斜杠 t），字段以 \t 分隔，
 * 解析层按 \t 拆行。字段内部（名称/镜像/命令）不含制表符，故拆分安全。
 */

import { extractSSHError } from '../ssh-error-types'

// ── 数据类型 ──

export type CState = 'running' | 'exited' | 'stopped'

export interface ContainerRow {
  id: string
  name: string
  repo: string
  tag: string
  state: CState
  stateText: string
  ports: string
  cmd: string
  created: string
}
export interface ImageRow { id: string; repo: string; tag: string; size: string; created: string }
/**
 * 卷行：Size/LinkCount 仅新版 docker 加 --verbose 才输出，而老版本（<23）
 * 根本不识别该 flag（unknown flag 直接失败），故列表命令不带 --verbose，
 * 对应字段常缺失，为可选；labels 来自 `json .Labels`，同样可能缺失。
 */
export interface VolumeRow {
  driver: string
  name: string
  size?: string
  linkCount?: number
  labels?: Record<string, string>
}
/**
 * 网络行：`.ID .Driver .Name .Scope` 是 docker network ls 的原生四列；
 * labels 为增强列（Docker 17.06+ 支持 json 函数）。
 */
export interface NetworkRow {
  id: string
  driver: string
  name: string
  scope: string
  labels?: Record<string, string>
}

/** 卷 inspect 结构化投影：Mountpoint/CreatedAt/Options/Labels/Usage 归一 */
export interface VolumeInspect {
  name: string
  driver: string
  mountpoint: string
  createdAt: string
  labels: Record<string, string>
  options: Record<string, string>
  usage?: { size?: number; refCount?: number }
}
/** 网络 inspect 结构化投影：IPAM/连接容器/特性标记 */
export interface NetworkInspect {
  name: string
  id: string
  driver: string
  scope: string
  internal: boolean
  attachable: boolean
  ingress: boolean
  enableIPv6: boolean
  ipam: { subnet: string; gateway: string; range: string }[]
  containers: { name: string; ipv4: string; mac: string }[]
  labels: Record<string, string>
  options: Record<string, string>
}

/** Docker 操作错误归类结果：UI 据 message/hint 渲染错误带，retryable 决定是否给重试 */
export interface DockerError {
  code: string
  message: string
  hint?: string
  retryable: boolean
}

// ── 采集命令（真取数，非样例） ──

// 字段顺对齐 docker ps 原生列序：CONTAINER ID · IMAGE · COMMAND · CREATED · STATUS · PORTS · NAMES
export const PS_CMD =
  `docker ps -a --format '{{printf "%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s" .ID .Image .Command .RunningFor .Status .Ports .Names}}'`
export const IMAGES_CMD =
  `docker images --format '{{printf "%s\\t%s\\t%s\\t%s\\t%s" .ID .Repository .Tag .Size .CreatedSince}}'`
// 卷：不带 --verbose——老版本（<23）docker volume ls 无该 flag，参数解析阶段就报
// unknown flag，整条命令失败。采用 `{{json .}}` 逐行 dump volumeContext，
// 客户端按存在字段取值；Size/LinkCount 缺失时列表显示「—」，
// 完整用量从详情弹窗的 `docker volume inspect`（Usage 字段）获取。
export const VOLUMES_CMD =
  `docker volume ls --format '{{json .}}'`
// 网络：同策略，json dump 避免 .Labels 在老版本不存在时模板失败
export const NETWORKS_CMD =
  `docker network ls --format '{{json .}}'`

// ── 操作命令构造（名称一律 shell 引用，容忍含空格的名字） ──

/** POSIX 单引号转义：把值安全地包进单引号，内部单引号用 '\'' 序列脱出 */
export function sq(s: string): string {
  return `'` + s.replace(/'/g, `'\\''`) + `'`
}

export const logsCmd = (name: string, tail = 200): string =>
  `docker logs --tail ${tail} ${sq(name)} 2>&1`
export const startCmd = (name: string): string => `docker start ${sq(name)}`
export const stopCmd = (name: string): string => `docker stop ${sq(name)}`
export const restartCmd = (name: string): string => `docker restart ${sq(name)}`
export const rmCmd = (name: string): string => `docker rm -f ${sq(name)}`
export const rmiCmd = (ref: string, force = true): string => `docker rmi${force ? ' -f' : ''} ${sq(ref)}`
export const execCmd = (name: string): string => `docker exec -it ${sq(name)} sh`
export const volumeRmCmd = (name: string): string => `docker volume rm ${sq(name)}`
export const networkRmCmd = (name: string): string => `docker network rm ${sq(name)}`
// prune -f：跳过交互确认；调用方须先经 UI 确认框（无 -f 时非 tty 环境直接失败）
export const volumePruneCmd = (): string => `docker volume prune -f`
export const networkPruneCmd = (): string => `docker network prune -f`
// inspect：一次拿一个对象的完整元数据，用于详情弹窗；docker 返回数组，取首元素
export const volumeInspectCmd = (name: string): string => `docker volume inspect ${sq(name)}`
export const networkInspectCmd = (name: string): string => `docker network inspect ${sq(name)}`

// ── 解析 ──

/** 按 \t 拆行；过滤掉不含制表符的行（空行 / 无 --format 支持的降级输出） */
function rows(raw: string, minFields: number): string[][] {
  return raw
    .split('\n')
    .map(l => l.replace(/\r$/, ''))
    .filter(l => l.includes('\t'))
    .map(l => l.split('\t'))
    .filter(f => f.length >= minFields)
}

/**
 * Status → 状态枚举：Up 运行；Exited (0)/paused 停止（可逆语义）；
 * 其余异常——含非 0 退出、Created，以及配了 --restart 策略却陷入崩溃重启循环的
 * Restarting（旧版误判为 running 绿色，掩盖了 crash-loop，故归到异常红色并置顶）。
 */
export function stateFromStatus(status: string): CState {
  const s = status.trim()
  if (/^up\b/i.test(s)) return 'running'
  if (/^exited \(0\)/i.test(s) || /paused/i.test(s)) return 'stopped'
  return 'exited'
}

/**
 * Image 字段拆 repo:tag：末个冒号后若无 `/` 才当作 tag；
 * 若末个冒号后含 `/`（如 registry 带端口 `localhost:5000/nginx`），那是端口而非 tag，整串作 repo。
 */
function splitImage(img: string): { repo: string; tag: string } {
  const i = img.lastIndexOf(':')
  if (i <= 0 || img.slice(i + 1).includes('/')) return { repo: img, tag: 'latest' }
  return { repo: img.slice(0, i), tag: img.slice(i + 1) }
}

export function parsePs(raw: string): ContainerRow[] {
  return rows(raw, 7).map(f => {
    const { repo, tag } = splitImage(f[1])
    return {
      id: f[0], repo, tag, name: f[6] || '',
      state: stateFromStatus(f[4]), stateText: f[4],
      ports: f[5] || '', cmd: f[2] || '', created: f[3] || '',
    }
  })
}

/**
 * docker 各版本给出的 ID 形状不一：新版 CLI 是 12 位短 ID，老版 CLI 与 `{{json .}}`
 * 直出结构体时是完整 `sha256:<64>`（或裸 64 位十六进制）。统一归一成 12 位短 ID：
 * 它既是展示列的稳定宽度，也是能直接交给 `docker rmi` 的合法句柄 —— 原先裸
 * `slice(0, 12)` 遇到带前缀的形态会截出 `sha256:8a1f2` 这种谁也不认识的引用，
 * 删悬空镜像时必然报 No such image。非十六进制的值（`<none>`）原样透传。
 */
export function shortId(raw: unknown): string {
  const s = String(raw ?? '').trim()
  const i = s.lastIndexOf(':')
  const tail = i >= 0 ? s.slice(i + 1) : s
  return (/^[0-9a-f]{12,}$/i.test(tail) ? tail : s).slice(0, 12)
}

export function parseImages(raw: string): ImageRow[] {
  return rows(raw, 5).map(f => ({
    id: shortId(f[0]), repo: f[1] || '<none>', tag: f[2] || '<none>',
    size: f[3] || '', created: f[4] || '',
  }))
}

/**
 * 镜像主标识（对齐新版 docker images 的 IMAGE 列）：悬空镜像无 repo:tag 时回落到短 ID。
 * repo 与 tag 任一为 `<none>` 都要回落 —— 按摘要拉取（`nginx@sha256:…`）的那一行
 * 是 repo 有名、tag 为 `<none>`，此时 `repo:<none>` 既不是可删的引用，也没有信息量。
 */
export function imageLabel(im: ImageRow): string {
  return im.repo === '<none>' || im.tag === '<none>' ? im.id : `${im.repo}:${im.tag}`
}

/** 镜像行的过滤串：展示句柄 + 原始 repo/tag 都算命中（回落短 ID 后仍能按仓库名搜到） */
export function imageHaystack(im: ImageRow): string {
  return `${imageLabel(im)} ${im.repo}:${im.tag}`.toLowerCase()
}

/**
 * docker 的 `{{json .}}` 将每行 dump 成一个 JSON 对象，字段因版本而异：
 *   所有版本：Name / Driver / Labels(map) / Scope / ID
 *   新版加 --verbose 才追加：Size(bytes) / LinkCount（列表命令为兼容不发该 flag）
 * 旧行解析（以 \t 拆列的降级入口）也支持，便于真实环境下 json 函数不可用时不黑屏。
 */
function jsonlRows(raw: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = []
  for (const line of raw.split('\n')) {
    const t = line.trim()
    if (!t.startsWith('{')) continue
    try { out.push(JSON.parse(t) as Record<string, unknown>) } catch { /* 跳坏行 */ }
  }
  return out
}

/** 行内 map → Record<string,string>；非对象/空 → {} */
function asStrMap(v: unknown): Record<string, string> {
  if (!v || typeof v !== 'object') return {}
  const out: Record<string, string> = {}
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    if (val !== null && val !== undefined) out[k] = String(val)
  }
  return out
}

/**
 * json 函数不可用时 docker 会直接打 Go map 形式 `map[k:v k2:v2]`，兼容解析。
 * 空串 / `<no value>` / 非对象 → {}。
 */
export function parseLabels(s: string | undefined | null): Record<string, string> {
  if (!s) return {}
  const t = s.trim()
  if (!t || t === '<no value>') return {}
  if (t.startsWith('{')) {
    try {
      const obj = JSON.parse(t) as Record<string, unknown>
      const out: Record<string, string> = {}
      for (const [k, v] of Object.entries(obj)) if (v !== null && v !== undefined) out[k] = String(v)
      return out
    } catch { /* fall through */ }
  }
  const m = t.match(/^map\[(.*)\]$/)
  if (!m) return {}
  const out: Record<string, string> = {}
  for (const pair of m[1].split(' ')) {
    const i = pair.indexOf(':')
    if (i > 0) out[pair.slice(0, i)] = pair.slice(i + 1)
  }
  return out
}

/**
 * Size 人读化：docker 给的是 bytes（int64）或已包好的字符串，两种都能接。
 * 采用 1024 进制，保留 1 小数位，对齐 docker 自己的习惯。
 */
export function humanSize(v: unknown): string | undefined {
  if (v === null || v === undefined) return undefined
  if (typeof v === 'string') return v || undefined
  const n = Number(v)
  if (!Number.isFinite(n) || n < 0) return undefined
  if (n === 0) return '0B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let x = n
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++ }
  return (i === 0 ? x.toFixed(0) : x.toFixed(1)) + units[i]
}

/**
 * 卷/网络标签列的一级线索：compose 项目名优先，其次标签条数，无标签 → 「—」。
 * 两类资源的这一列形状相同，判据只留这一处（视图侧曾各抄一份完全一样的实现）。
 */
export function labelHint(labels?: Record<string, string>): string {
  const proj = labels?.['com.docker.compose.project']
  if (proj) return 'compose:' + proj
  const n = labels ? Object.keys(labels).length : 0
  return n ? `${n} 项` : '—'
}

export function parseVolumes(raw: string): VolumeRow[] {
  const json = jsonlRows(raw)
  if (json.length) {
    return json.map(o => {
      const r: VolumeRow = {
        driver: String(o.Driver || ''),
        name: String(o.Name || ''),
      }
      const sz = humanSize(o.Size)
      if (sz) r.size = sz
      if (typeof o.LinkCount === 'number') r.linkCount = o.LinkCount
      const lb = asStrMap(o.Labels)
      if (Object.keys(lb).length) r.labels = lb
      return r
    })
  }
  // 降级：json 函数不可用时 docker 会因模板失败而输出空；旧行拆分入口兼容手写 printf 输出
  return rows(raw, 2).map(f => {
    const r: VolumeRow = { driver: f[0] || '', name: f[1] || '' }
    if (f[2]) r.size = f[2]
    if (f[3] && f[3] !== '<no value>' && !Number.isNaN(Number(f[3]))) r.linkCount = Number(f[3])
    const lb = parseLabels(f[4])
    if (Object.keys(lb).length) r.labels = lb
    return r
  })
}

export function parseNetworks(raw: string): NetworkRow[] {
  const json = jsonlRows(raw)
  if (json.length) {
    return json.map(o => {
      const r: NetworkRow = {
        id: shortId(o.ID),
        driver: String(o.Driver || ''),
        name: String(o.Name || ''),
        scope: String(o.Scope || ''),
      }
      const lb = asStrMap(o.Labels)
      if (Object.keys(lb).length) r.labels = lb
      return r
    })
  }
  return rows(raw, 4).map(f => {
    const r: NetworkRow = {
      id: shortId(f[0]), driver: f[1] || '', name: f[2] || '', scope: f[3] || '',
    }
    const lb = parseLabels(f[4])
    if (Object.keys(lb).length) r.labels = lb
    return r
  })
}

/**
 * docker volume/network inspect 输出为 JSON 数组；空数组或解析失败返回 null（上层据此显示「无详情」）。
 * 保持宽容：字段缺失时回落默认值，不让 UI 崩溃。
 */
export function parseVolumeInspect(raw: string): VolumeInspect | null {
  let arr: unknown
  try { arr = JSON.parse(raw) } catch { return null }
  const v = (Array.isArray(arr) ? arr[0] : arr) as Record<string, unknown> | undefined
  if (!v || typeof v !== 'object') return null
  const usage = v.Usage as Record<string, unknown> | undefined
  return {
    name: String(v.Name || ''),
    driver: String(v.Driver || ''),
    mountpoint: String(v.Mountpoint || ''),
    createdAt: String(v.CreatedAt || ''),
    labels: (v.Labels && typeof v.Labels === 'object') ? v.Labels as Record<string, string> : {},
    options: (v.Options && typeof v.Options === 'object') ? v.Options as Record<string, string> : {},
    usage: usage
      ? {
          size: typeof usage.Size === 'number' ? usage.Size : undefined,
          refCount: typeof usage.RefCount === 'number' ? usage.RefCount : undefined,
        }
      : undefined,
  }
}

export function parseNetworkInspect(raw: string): NetworkInspect | null {
  let arr: unknown
  try { arr = JSON.parse(raw) } catch { return null }
  const v = (Array.isArray(arr) ? arr[0] : arr) as Record<string, unknown> | undefined
  if (!v || typeof v !== 'object') return null
  const ipamCfg = (v.IPAM as Record<string, unknown> | undefined)?.Config
  const ipam = Array.isArray(ipamCfg)
    ? ipamCfg.map((c: Record<string, unknown>) => ({
        subnet: String(c?.Subnet || ''),
        gateway: String(c?.Gateway || ''),
        range: String(c?.IPRange || ''),
      }))
    : []
  const containersMap = (v.Containers && typeof v.Containers === 'object') ? v.Containers as Record<string, Record<string, unknown>> : {}
  const containers = Object.values(containersMap).map(c => ({
    name: String(c?.Name || ''),
    // IPv4Address 常写作 "172.17.0.2/16"，剥掉掩码更宜读
    ipv4: String(c?.IPv4Address || '').split('/')[0] || '',
    mac: String(c?.MacAddress || ''),
  }))
  return {
    name: String(v.Name || ''),
    id: shortId(v.Id),
    driver: String(v.Driver || ''),
    scope: String(v.Scope || ''),
    internal: !!v.Internal,
    attachable: !!v.Attachable,
    ingress: !!v.Ingress,
    enableIPv6: !!v.EnableIPv6,
    ipam,
    containers,
    labels: (v.Labels && typeof v.Labels === 'object') ? v.Labels as Record<string, string> : {},
    options: (v.Options && typeof v.Options === 'object') ? v.Options as Record<string, string> : {},
  }
}

/** 日志原文 → 行数组（去 CR、剥尾部单个空行；保留内部空行以贴合真实输出） */
export function parseLogs(raw: string): string[] {
  const arr = raw.split('\n').map(l => l.replace(/\r$/, ''))
  if (arr.length && arr[arr.length - 1] === '') arr.pop()
  return arr
}

// ── 错误归类 ──

/**
 * 依据 docker 命令的 stderr + 退出码归类错误。返回 null 表示无 docker 层错误。
 * sshExec 对非零退出码不抛异常（只在 SSH 传输层出错时抛），故须显式检查退出码。
 */
export function dockerErrorFromResult(stderr: string, exitCode: number | null, mergedStdout = ''): DockerError | null {
  // 退出码 0 视为成功：docker 偶尔把提示打到 stderr，不代表失败
  if (exitCode === 0) return null
  // 优先用 stderr；docker logs 用 2>&1 把 CLI 错误并到 stdout（stderr 为空），
  // 故 stderr 空时回落到合并输出，避免「No such container / 连不上守护进程」被降级成通用退出码错误
  const e = (stderr || '').trim() || (mergedStdout || '').trim()
  const first = e.split('\n')[0] || ''

  // 未安装 / 不在 PATH：命令找不到（127）或明确 not found
  if (exitCode === 127 || /docker:.*command not found|command not found: docker|No such file or directory.*docker/i.test(e)) {
    return {
      code: 'DOCKER_NOT_FOUND',
      message: '该主机未安装 Docker，或 docker 不在登录 shell 的 PATH 中',
      hint: '确认 Docker 已安装；若已安装，检查 sshd 给非交互 shell 的 PATH 是否含 docker 所在目录',
      retryable: false,
    }
  }
  // 权限：socket 拒绝 / 守护进程连不上
  if (/permission denied.*docker|docker\.sock|Cannot connect to the Docker daemon|Is the docker daemon running/i.test(e)) {
    return {
      code: 'DOCKER_PERM',
      message: '无权访问 Docker：当前用户无法连接 docker.sock，或 dockerd 未运行',
      hint: '把用户加入 docker 组（usermod -aG docker $USER，重新登录生效），或以有权限的用户连接',
      retryable: false,
    }
  }
  // 目标已不存在（多因列表未刷新）
  if (/No such (container|image|network|volume)/i.test(e)) {
    return {
      code: 'DOCKER_NOT_FOUND_OBJ',
      message: first || '目标已不存在',
      hint: '该资源可能已被删除，刷新列表后重试',
      retryable: true,
    }
  }
  // 卷/网络仍被容器占用：rm 会失败，须先停并删关联容器；prune 只清理未被引用项
  if (/volume is in use|being used by some container|network .* has active endpoints|network is not empty/i.test(e)) {
    return {
      code: 'DOCKER_IN_USE',
      message: first || '资源仍被容器使用',
      hint: '先停止并删除引用它的容器，再重试删除；或使用「清理未使用」一键回收未引用的卷/网络',
      retryable: false,
    }
  }
  // 其它非零退出：透首行，保守可重试
  if (exitCode !== null && exitCode !== 0) {
    return {
      code: 'DOCKER_EXEC_FAILED',
      message: first || `docker 命令失败（退出码 ${exitCode}）`,
      retryable: true,
    }
  }
  // exit-status 缺失（不知道成败）：不谎报，交由上层按 unknown 处理
  if (exitCode === null && e) {
    return { code: 'DOCKER_UNKNOWN', message: first, retryable: true }
  }
  return null
}

/** 传输层抛错（会话缺失 / 超时等）→ DockerError，与 describeMonitorError 同构 */
export function describeThrownDockerError(err: unknown): DockerError {
  const t = extractSSHError(err)
  if (t) {
    return {
      code: t.code,
      message: t.message || 'SSH 通道错误',
      hint: t.detail,
      retryable: t.retryable,
    }
  }
  const msg = err instanceof Error ? err.message : String(err)
  return { code: 'EXEC_THROWN', message: msg || '命令执行失败', retryable: true }
}
