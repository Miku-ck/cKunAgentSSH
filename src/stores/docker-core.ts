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
import { formatBytesCompact } from '../utils/format'

import { extractSSHError } from '@/types/ssh-error-types'

// ── 数据类型 ──

export type CState = 'running' | 'exited' | 'stopped'
export type HealthState = 'healthy' | 'unhealthy' | 'starting'

export interface ContainerRow {
  id: string
  name: string
  repo: string
  tag: string
  /** docker ps 的 IMAGE 原始串：判断「镜像是否被容器引用」时按它做跨形态匹配 */
  imageRef: string
  state: CState
  /** docker ps 的 Status 里带的健康标记；无 healthcheck / 无标记时缺省 */
  health?: HealthState
  stateText: string
  ports: string
  cmd: string
  created: string
}

/** 容器 inspect 的结构化投影：详情弹窗只消费这一层，不直接摸原始 JSON */
export interface ContainerInspect {
  id: string
  name: string
  image: string
  imageId: string
  imageDigest?: string
  created: string
  platform: string
  state: {
    status: string
    running: boolean
    paused: boolean
    restarting: boolean
    exitCode: number
    oomKilled: boolean
    startedAt: string
    finishedAt: string
    health?: HealthState
    failingStreak?: number
    lastHealth?: { exitCode: number; output: string; end: string }
  }
  restartCount: number
  restartPolicy: string
  entrypoint: string[]
  cmd: string[]
  workingDir: string
  user: string
  env: { key: string; value: string }[]
  labels: Record<string, string>
  healthcheck?: {
    test: string
    interval?: string
    timeout?: string
    startPeriod?: string
    retries: number
  }
  ports: { container: string; host: string }[]
  networks: { name: string; ip: string; gateway: string; mac: string }[]
  mounts: { type: string; name: string; source: string; destination: string; rw: boolean }[]
  resources: { memory?: string; cpus?: string; pidsLimit?: number }
  compose?: { project: string; service: string; configFiles: string }
}

/** docker stats --no-stream 的原始显示串，解析层不重新格式化 */
export interface ContainerStats {
  cpuPerc: string
  memUsage: string
  memPerc: string
  netIO: string
  blockIO: string
  pids: string
}
export interface ImageRow { id: string; repo: string; tag: string; size: string; created: string }
/**
 * 卷行：Size/LinkCount 不在列表接口里（docker volume ls 没有 --verbose；
 * API 的 UsageData 只随 GET /system/df 返回），故列表命令不取，由
 * VOLUME_USAGE_CMD 异步富集；labels 来自 `{{json .}}` 的逗号拼接串。
 */
export interface VolumeRow {
  driver: string
  name: string
  size?: string
  linkCount?: number
  labels?: Record<string, string>
}
/** system df -v 富集出的卷用量；两个字段都可能单独缺失 */
export interface VolumeUsage { size?: string; linkCount?: number }
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
// 卷：采用 `{{json .}}` 逐行 dump volumeContext。docker volume ls 没有 --verbose，
// 列表本身不含用量（UsageData 仅 GET /system/df 返回），Size/Links 缺失时显示「—」，
// 由下方的 VOLUME_USAGE_CMD 异步富集。
export const VOLUMES_CMD =
  `docker volume ls --format '{{json .}}'`
// 用量富集：只有在 -v 的自定义格式里才能拿到 .Volumes 的 UsageData；
// --format 在旧版 docker 上可能不存在，失败就保持「—」，不影响主列表。
export const VOLUME_USAGE_CMD =
  `docker system df -v --format '{{range .Volumes}}{{printf "%s\\t%s\\t%s" .Name .Links .Size}}{{"\\n"}}{{end}}'`
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
// 默认不强制：确认框承诺「有容器（含已停止）正在使用时会失败」，带上 -f 会强删
// 并让引用它的容器变成悬空引用 —— 与用户确认的语义相反。
export const rmiCmd = (ref: string, force = false): string => `docker rmi${force ? ' -f' : ''} ${sq(ref)}`
export const execCmd = (name: string): string => `docker exec -it ${sq(name)} sh`
export const volumeRmCmd = (name: string): string => `docker volume rm ${sq(name)}`
export const networkRmCmd = (name: string): string => `docker network rm ${sq(name)}`
// prune -f：跳过交互确认；调用方须先经 UI 确认框（无 -f 时非 tty 环境直接失败）
export const volumePruneCmd = (): string => `docker volume prune -f`
export const networkPruneCmd = (): string => `docker network prune -f`
// inspect：一次拿一个对象的完整元数据，用于详情弹窗；docker 返回数组，取首元素
export const volumeInspectCmd = (name: string): string => `docker volume inspect ${sq(name)}`
export const networkInspectCmd = (name: string): string => `docker network inspect ${sq(name)}`
export const containerInspectCmd = (name: string): string => `docker container inspect ${sq(name)}`
// --no-stream：取一次快照而不是持续刷屏；{{json .}} 的键为 CPUPerc/MemUsage/... 原始显示串
export const containerStatsCmd = (name: string): string =>
  `docker stats --no-stream --format '{{json .}}' ${sq(name)}`
// --quiet：非 TTY 下不吐进度条，只留结果/错误，避免 1MB 截断；耗时由调用方放长预算
export const pullCmd = (ref: string): string => `docker pull --quiet ${sq(ref)}`
// 不带 -a：只清 dangling（无标签层），不动任何带标签镜像
export const imagePruneCmd = (): string => `docker image prune -f`
// -a：连同带标签但未被任何容器（含已停止）引用的镜像一起清；破坏面更大，
// 调用方必须把「带标签镜像也在范围内」写进确认框
export const imagePruneAllCmd = (): string => `docker image prune -a -f`

// ── 镜像导出 / 加载（纯远端执行）──
// tar 只落在远端主机上：docker save 的 stdout 是二进制流，走不了 sshExec 的文本通道
// （1MB 截断 + UTF-8 解码），所以必须落盘；加载侧对称地读盘。
// 两端都不经过本地，所以不需要传输链路，也没有本地副本。
//
// 导出可选 gzip。既不能「先 -o 落到同名 .tar 再 gzip」（`app_v1.4.2.tar` 正是同目录
// 里真实可能存在的旧备份，那一步会把它静默覆盖），也不能只写管道：POSIX 管道的退出码
// 只反映最后一个命令，docker save 中途死掉时 gzip 照样退出 0，交出一份看起来完好、
// 实际截断的备份 —— 备份工具最不该有的失败模式。
//
// 所以走「同目录临时文件」：save 先落 .<名>.part，成功后 gzip 到目标，末尾用 $?
// 把两步的真实退出码带出来。临时文件放目标目录而不是 /tmp —— 后者常是 tmpfs，
// 多 GB 镜像会把内存吃穿。save 失败时 gzip 不跑，目标位置的旧备份原样保留。
export function saveTempPath(path: string): string {
  const i = path.lastIndexOf('/')
  if (i < 0) return `.${path}.part`
  const name = path.slice(i + 1) || 'image'
  return `${path.slice(0, i)}/.${name}.part`
}

const gzSaveCmd = (ref: string, remotePath: string): string => {
  const tmp = sq(saveTempPath(remotePath))
  return `docker save -o ${tmp} ${sq(ref)} && gzip -1 -c ${tmp} > ${sq(remotePath)}; rc=$?; rm -f ${tmp}; exit $rc`
}

/**
 * gz=false 时 docker save 原样落盘；true 时先落临时文件再 gzip，路径本身应以 .tar.gz 结尾。
 * 压缩级别取 -1：镜像层多是二进制，-6 省不下多少体积，但在 600s 预算里明显更慢。
 */
export const dockerSaveCmd = (ref: string, remotePath: string, gz = false): string =>
  gz ? gzSaveCmd(ref, remotePath) : `docker save -o ${sq(remotePath)} ${sq(ref)}`
// 加载不看后缀：docker load 按魔数识别 gzip / bzip2 / xz，.tar.gz 直接能读
export const dockerLoadCmd = (remotePath: string): string =>
  `docker load -i ${sq(remotePath)}`

/** 已知归档后缀：顺序从长到短，先到先删 —— 否则 .tar.gz 会被当成 .tar 只剥一层 */
const ARCHIVE_SUFFIX_RE = /(\.tar\.gz|\.tgz|\.tar|\.gz)$/i

/** 镜像引用里取文件名用的一段：去掉 namespace 前缀，只留仓库名末段 */
function repoTail(repo: string): string {
  return repo.split('/').pop() || repo
}

/** 把任意串清成可做文件名的字符集（Docker 标签允许 + 和 @，文件系统不欢迎） */
const fileSafe = (s: string): string => s.replace(/[^A-Za-z0-9._-]/g, '_')

/**
 * 导出时的默认文件名：仓库末段 + 标签，如 `app_v3.8.2.tar` / `app_v3.8.2.tar.gz`。
 * 悬空镜像（repo 或 tag 为 `<none>`）用短 ID —— 与表格里显示的那串保持一致，
 * 用户看到的和存下来的对得上。
 */
export function suggestTarName(repo: string, tag: string, id = '', gz = false): string {
  const suffix = gz ? '.tar.gz' : '.tar'
  if (!repo || repo === '<none>' || !tag || tag === '<none>') {
    return `${fileSafe(id || repoTail(repo) || 'image')}${suffix}`
  }
  return `${fileSafe(repoTail(repo))}_${fileSafe(tag)}${suffix}`
}

/**
 * 导出文件名校验：返回中文原因，合法返回 null。
 * 只校验主体 —— 后缀由格式开关说了算，ensureTarSuffix 会把 .tar / .tar.gz / .tgz
 * 统一归一到选中的那一种，所以用户敲什么后缀都不会落成「.gz.tar」这种四不像。
 */
export function validateSaveName(name: string): string | null {
  const n = String(name || '').trim().replace(ARCHIVE_SUFFIX_RE, '')
  if (!n || /^\.+$/.test(n)) return '文件名不能为空'
  if (n.length > 200) return '文件名过长'
  if (/[\/\\]/.test(n)) return '文件名不能包含 /'
  if (n.includes('..')) return '文件名不能包含 ..'
  if (n.startsWith('-')) return '文件名不能以 - 开头'
  if (/[\x00-\x1f\x7f]/.test(n)) return '文件名不能包含控制字符'
  return null
}

/** 把名字归一到选中的归档格式；空串原样返回（不造出 .tar 这种隐形文件名） */
export function ensureTarSuffix(name: string, gz = false): string {
  const n = String(name || '').trim()
  if (!n) return n
  // 先削尾点再剥后缀：`backup.` 直接拼会造出 `backup..tar`，而 validateSaveName
  // 明令禁止 `..` —— 校验说合法、预览里却出现一个它自己会拒的名字
  const body = n.replace(/\.+$/, '').replace(ARCHIVE_SUFFIX_RE, '')
  return body ? `${body}${gz ? '.tar.gz' : '.tar'}` : n
}

/**
 * 远端目录校验：必须是绝对路径。
 * `docker save -o relative.tar` 会落在 sshd 的默认工作目录（通常是家目录），
 * 用户以为存在 /tmp 里，回头找不到文件。
 */
export function validateRemoteDir(dir: string): string | null {
  const d = String(dir || '').trim()
  if (!d) return '目录不能为空'
  if (!d.startsWith('/')) return '必须是绝对路径（以 / 开头）'
  if (/[\x00-\x1f\x7f]/.test(d)) return '目录不能包含控制字符'
  return null
}

/** 目录 + 文件名拼绝对路径：尾斜杠不重复，根目录不多加一道 */
export function joinRemotePath(dir: string, name: string): string {
  const d = String(dir || '').trim()
  const n = String(name || '').trim()
  const base = d ? d.replace(/\/+$/, '') || '/' : '/'
  if (!n) return base
  return base === '/' ? `/${n}` : `${base}/${n}`
}

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
  // docker ps 对暂停容器输出 "Up About an hour (Paused)"：必须先于 Up 判定，
  // 否则会落到 running（绿色），状态计数与异常置顶一并失真。
  if (/\(paused\)/i.test(s) || /^paused\b/i.test(s)) return 'stopped'
  if (/^up\b/i.test(s)) return 'running'
  if (/^exited \(0\)/i.test(s)) return 'stopped'
  return 'exited'
}

/**
 * docker ps 的 Status 里 healthcheck 以括号后缀出现：
 * "Up 2 hours (healthy)" / "Up 2 hours (unhealthy)" / "Up 3s (health: starting)"。
 * 它不影响 running 状态本身，但 unhealthy 必须能被上层看见并计为异常。
 */
export function healthFromStatus(status: string): HealthState | undefined {
  const m = status.match(/\((healthy|unhealthy|health:\s*starting)\)/i)
  if (!m) return undefined
  const v = m[1].toLowerCase()
  if (v === 'healthy') return 'healthy'
  if (v === 'unhealthy') return 'unhealthy'
  return 'starting'
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
      // 保留原串：repo/tag 拆分丢掉了 ID / digest 形态，引用匹配需要它
      imageRef: f[1] || '',
      state: stateFromStatus(f[4]), health: healthFromStatus(f[4]), stateText: f[4],
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
 * 容器的 IMAGE 串是否指向该镜像。docker ps 的 IMAGE 列随创建方式变形：
 * `repo:tag`、latest 的裸 `repo`、短/完整 ID，以及按摘要创建时的 `repo@sha256:…`。
 * 拿不准时按「在用」处理 —— 多算引用只会少删，不会把在用的镜像误判成可删。
 */
export function imageRefMatches(im: ImageRow, ref: string): boolean {
  const r = String(ref ?? '').trim().toLowerCase()
  if (!r) return false
  const id = im.id.toLowerCase()
  // ID 形态统一剥掉算法前缀再比前缀：短 ID、`sha256:<64>`、裸 64 位 hex 都能命中
  const bare = r.startsWith('sha256:') ? r.slice(7) : r
  if (id.length >= 12 && /^[0-9a-f]{12,}$/.test(bare) && bare.startsWith(id)) return true
  // 悬空镜像没有仓库名，只剩 ID 可认
  if (im.repo === '<none>') return false
  const repo = im.repo.toLowerCase()
  // 按摘要创建/拉取：容器记 `repo@sha256:…`，列表里同一镜像的 tag 为 <none>
  if (r.startsWith(repo + '@')) return true
  if (im.tag === '<none>') return false
  const tag = im.tag.toLowerCase()
  if (r === `${repo}:${tag}`) return true
  // Docker 对 latest 的显示会省略 `:latest`
  return tag === 'latest' && r === repo
}

/** 引用该镜像的容器数（按容器 ID 去重）；容器列表不完整时由调用方降级为「—」 */
export function imageUsageCount(im: ImageRow, containers: ContainerRow[]): number {
  const seen = new Set<string>()
  for (const c of containers) {
    if (imageRefMatches(im, c.imageRef)) seen.add(c.id || c.name)
  }
  return seen.size
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
  if (m) {
    const out: Record<string, string> = {}
    for (const pair of m[1].split(' ')) {
      const i = pair.indexOf(':')
      if (i > 0) out[pair.slice(0, i)] = pair.slice(i + 1)
    }
    return out
  }
  // Docker formatter 把 Labels 拼成 "k=v,k2=v2"（无 map/json 包装）；
  // 只在「逗号后紧跟下一个 key=」处切，容忍值内部含逗号。
  if (!t.startsWith('{') && t.includes('=')) {
    const out: Record<string, string> = {}
    for (const pair of t.split(/,(?=[^,=]+=)/)) {
      const i = pair.indexOf('=')
      if (i > 0) out[pair.slice(0, i)] = pair.slice(i + 1)
    }
    return out
  }
  return {}
}

/** JSON 行的 Labels 可能是 map（inspect）或逗号串（list formatter），两种都接 */
function jsonLabels(v: unknown): Record<string, string> {
  return typeof v === 'string' ? parseLabels(v) : asStrMap(v)
}

/**
 * Size 人读化：docker 给的是 bytes（int64）或已包好的字符串，两种都能接。
 * 采用 1024 进制，保留 1 小数位，对齐 docker 自己的习惯。
 * ARCH-010：实现收归 utils/format.ts 单一源（formatBytesCompact），
 * docker 域的字符串直通 / undefined 兜底语义由其原生提供。
 */

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
      // 真实列表输出里 Size/Links 是字符串（无 UsageData 时为 "N/A"）；
      // 数字/数字串都归一，人读大小直通，N/A 视为缺失。
      const sz = formatBytesCompact(o.Size)
      if (sz && sz !== 'N/A') r.size = sz
      const links = o.Links ?? o.LinkCount
      if (typeof links === 'number' && Number.isInteger(links) && links >= 0) {
        r.linkCount = links
      } else if (typeof links === 'string' && /^\d+$/.test(links.trim())) {
        r.linkCount = Number(links.trim())
      }
      const lb = jsonLabels(o.Labels)
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

/**
 * `docker system df -v` 自定义格式的卷明细：Name \t Links \t Size。
 * 该格式在旧版 docker 上可能不支持；调用方按尽力而为处理，失败保持缺失。
 */
export function parseVolumeUsage(raw: string): Map<string, VolumeUsage> {
  const out = new Map<string, VolumeUsage>()
  for (const f of rows(raw, 3)) {
    const name = f[0].trim()
    if (!name) continue
    const u: VolumeUsage = {}
    const links = f[1].trim()
    if (/^\d+$/.test(links)) u.linkCount = Number(links)
    const size = f[2].trim()
    if (size && size !== 'N/A') u.size = size
    if (u.size !== undefined || u.linkCount !== undefined) out.set(name, u)
  }
  return out
}

/** 按卷名把用量并进列表行；未命中的行保持原对象（避免无谓重渲染） */
export function mergeVolumeUsage(
  list: VolumeRow[], usage: Map<string, VolumeUsage>,
): VolumeRow[] {
  if (!usage.size) return list
  return list.map(v => {
    const u = usage.get(v.name)
    return u ? { ...v, ...u } : v
  })
}

/**
 * 详情弹窗的用量口径：inspect 的 UsageData 优先，缺失时回落到列表富集值。
 * 标准 volume inspect 不返回 UsageData，所以多数情况下实际走回落。
 */
export function volumeUsageSummary(
  row: VolumeRow,
  inspect?: Pick<VolumeInspect, 'usage'> | null,
): { refCount?: number; size?: string } {
  const u = inspect?.usage
  return {
    refCount: u?.refCount ?? row.linkCount,
    size: u?.size !== undefined ? formatBytesCompact(u.size) : row.size,
  }
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
      const lb = jsonLabels(o.Labels)
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
 * prune 输出 = 可选表头（Deleted Volumes:/Deleted Networks:）+ 每行一个名称
 * + 卷才有的 Total reclaimed space。只数名称行，表头与汇总行都不算。
 */
export function countPrunedLines(out: string): number {
  return out
    .split('\n')
    .map(s => s.trim())
    .filter(s => s && !/^deleted (volumes|networks|images):$/i.test(s) && !/^total reclaimed space:/i.test(s))
    .length
}

/**
 * 异常容器判据：非 0 退出的行左侧打警示带，并在排序里顶到最前。
 * healthcheck 失败优先于 running —— 容器还在跑，但已经在报错。
 */
export function isAbnormal(c: Pick<ContainerRow, 'state' | 'health'>): boolean {
  return c.state === 'exited' || c.health === 'unhealthy'
}

/** Docker 预置且拒绝删除的三张网络；自定义网络不能与它们重名，故按名称判定即可 */
export function isBuiltinNetwork(name: string): boolean {
  return name === 'bridge' || name === 'host' || name === 'none'
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
  // 标准 inspect 不含 UsageData（它只随 GET /system/df 返回）；字段名仍是 UsageData，
  // 兼容 daemon 明确返回该字段时的解析。
  const usage = v.UsageData as Record<string, unknown> | undefined
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

/** inspect 的纳秒时长 → 人读；0/缺失表示未设置，不要显示成 0s */
export function formatNsDuration(ns: unknown): string | undefined {
  if (typeof ns !== 'number' || !Number.isFinite(ns) || ns <= 0) return undefined
  if (ns >= 60e9) {
    const m = Math.floor(ns / 60e9)
    const s = Math.round((ns % 60e9) / 1e9)
    return s ? `${m}m${s}s` : `${m}m`
  }
  if (ns >= 1e9) return `${Math.round(ns / 1e9)}s`
  return `${Math.round(ns / 1e6)}ms`
}

function asRec(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
}
function asStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}
function asStrArr(v: unknown): string[] {
  return Array.isArray(v) ? v.map(x => String(x)) : []
}
function asNum(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}
/** Docker 用 0001-01-01 表示「从未发生」（未启动/未结束），展示层不应把它当时间 */
function inspectTime(v: unknown): string {
  const s = asStr(v)
  return s && !s.startsWith('0001-01-01') ? s : ''
}

/**
 * 容器 inspect → 详情投影。字段全部宽容回落：老版本/不同驱动的 inspect 形状有差异，
 * 缺项只留空，不让 UI 崩。
 */
export function parseContainerInspect(raw: string): ContainerInspect | null {
  let arr: unknown
  try { arr = JSON.parse(raw) } catch { return null }
  const v = (Array.isArray(arr) ? arr[0] : arr) as Record<string, unknown> | undefined
  if (!v || typeof v !== 'object') return null

  const cfg = asRec(v.Config)
  const hc = asRec(v.HostConfig)
  const st = asRec(v.State)
  const ns = asRec(v.NetworkSettings)
  const labels = asStrMap(cfg.Labels)

  const healthRec = asRec(st.Health)
  const healthRaw = asStr(healthRec.Status).toLowerCase()
  const health: HealthState | undefined =
    healthRaw === 'healthy' || healthRaw === 'unhealthy' || healthRaw === 'starting'
      ? healthRaw : undefined
  const healthLog = Array.isArray(healthRec.Log) ? healthRec.Log : []
  const last = healthLog.length ? asRec(healthLog[healthLog.length - 1]) : null

  const rp = asRec(hc.RestartPolicy)
  const rpName = asStr(rp.Name)
  const rpMax = asNum(rp.MaximumRetryCount) ?? 0
  const restartPolicy = rpName === 'on-failure' && rpMax > 0 ? `${rpName}:${rpMax}` : rpName

  const env = asStrArr(cfg.Env).map(s => {
    const i = s.indexOf('=')
    return i < 0 ? { key: s, value: '' } : { key: s.slice(0, i), value: s.slice(i + 1) }
  })

  const hcCfg = asRec(cfg.Healthcheck)
  const hcTest = asStrArr(hcCfg.Test)
  const healthcheck = hcTest.length ? {
    test: hcTest.join(' '),
    interval: formatNsDuration(hcCfg.Interval),
    timeout: formatNsDuration(hcCfg.Timeout),
    startPeriod: formatNsDuration(hcCfg.StartPeriod),
    retries: asNum(hcCfg.Retries) ?? 0,
  } : undefined

  // 实际绑定优先（含运行时端口映射）；没有时回落 HostConfig 的声明
  const bound = asRec(ns.Ports)
  const portsRaw = Object.keys(bound).length ? bound : asRec(hc.PortBindings)
  const ports: ContainerInspect['ports'] = []
  for (const [container, val] of Object.entries(portsRaw)) {
    if (!Array.isArray(val) || !val.length) { ports.push({ container, host: '' }); continue }
    for (const b of val) {
      const br = asRec(b)
      const ip = asStr(br.HostIp)
      const port = asStr(br.HostPort)
      ports.push({ container, host: port ? (ip ? `${ip}:${port}` : port) : '' })
    }
  }

  const networks = Object.entries(asRec(ns.Networks)).map(([name, val]) => {
    const n = asRec(val)
    return { name, ip: asStr(n.IPAddress), gateway: asStr(n.Gateway), mac: asStr(n.MacAddress) }
  })

  const mounts = Array.isArray(v.Mounts) ? v.Mounts.map(m => {
    const mr = asRec(m)
    return {
      type: asStr(mr.Type), name: asStr(mr.Name), source: asStr(mr.Source),
      destination: asStr(mr.Destination), rw: !!mr.RW,
    }
  }) : []

  const nano = asNum(hc.NanoCpus) ?? 0
  const pidsLimit = asNum(hc.PidsLimit)
  const project = labels['com.docker.compose.project']
  const digest = Array.isArray(v.RepoDigests) ? asStr(v.RepoDigests[0]) : ''

  return {
    id: shortId(v.Id),
    name: asStr(v.Name).replace(/^\/+/, ''),
    image: asStr(cfg.Image),
    imageId: shortId(v.Image),
    imageDigest: digest || undefined,
    created: asStr(v.Created),
    platform: asStr(v.Platform),
    state: {
      status: asStr(st.Status),
      running: !!st.Running,
      paused: !!st.Paused,
      restarting: !!st.Restarting,
      exitCode: asNum(st.ExitCode) ?? 0,
      oomKilled: !!st.OOMKilled,
      startedAt: inspectTime(st.StartedAt),
      finishedAt: inspectTime(st.FinishedAt),
      health,
      failingStreak: health ? asNum(healthRec.FailingStreak) : undefined,
      lastHealth: last ? {
        exitCode: asNum(last.ExitCode) ?? 0,
        output: asStr(last.Output),
        end: asStr(last.End),
      } : undefined,
    },
    restartCount: asNum(v.RestartCount) ?? 0,
    restartPolicy,
    entrypoint: asStrArr(cfg.Entrypoint),
    cmd: asStrArr(cfg.Cmd),
    workingDir: asStr(cfg.WorkingDir),
    user: asStr(cfg.User),
    env,
    labels,
    healthcheck,
    ports,
    networks,
    mounts,
    resources: {
      memory: formatBytesCompact(hc.Memory),
      cpus: nano > 0 ? String(Number((nano / 1e9).toFixed(2))) : undefined,
      pidsLimit: pidsLimit && pidsLimit > 0 ? pidsLimit : undefined,
    },
    compose: project ? {
      project,
      service: labels['com.docker.compose.service'] || '',
      configFiles: labels['com.docker.compose.project.config_files'] || '',
    } : undefined,
  }
}

/** docker stats --no-stream 的一行 JSON；无输出（容器未运行）或坏行 → null */
export function parseContainerStats(raw: string): ContainerStats | null {
  const o = jsonlRows(raw)[0]
  if (!o) return null
  const s = (k: string): string => (o[k] === null || o[k] === undefined ? '' : String(o[k]))
  return {
    cpuPerc: s('CPUPerc'),
    memUsage: s('MemUsage'),
    memPerc: s('MemPerc'),
    netIO: s('NetIO'),
    blockIO: s('BlockIO'),
    pids: s('PIDs'),
  }
}

// 主进程在单次 exec 输出达到上限后会追加这条说明（electron/ssh-manager.ts 的
// TRUNCATED_NOTE，当前文案为「[输出已截断，超过 1M 字符上限]」）。
// 这里只做标记识别，不发命令；列表解析对它天然无感，但要让用户知道结果不完整。
// 判据与那句文案现收在 shared/exec-outcome-core.ts（主进程追加、渲染层识别同一份字面量），
// 页签的 parse 里仍要显式调用它 —— 「列表少了 N 行」不会让解析报错。

/** 日志原文 → 行数组（去 CR、剥尾部单个空行；保留内部空行以贴合真实输出） */
export function parseLogs(raw: string): string[] {
  const arr = raw.split('\n').map(l => l.replace(/\r$/, ''))
  if (arr.length && arr[arr.length - 1] === '') arr.pop()
  return arr
}

// ── 错误归类 ──

// exec 三态判据（`exitCode === null` 是「不知道」，不是失败）已收到
// shared/exec-outcome-core.ts：主进程 ssh-manager 与渲染层共用一份，长任务
// （镜像 save/load）必须按三态报，写完文件却没回 exit-status 是真实存在的。

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
      message: '未检测到 Docker',
      hint: '这台主机没有安装 Docker，或当前账号找不到它；装上或修复安装后再试',
      retryable: false,
    }
  }
  // 权限：socket 拒绝 / 守护进程连不上
  if (/permission denied.*docker|docker\.sock|Cannot connect to the Docker daemon|Is the docker daemon running/i.test(e)) {
    return {
      code: 'DOCKER_PERM',
      message: '无权访问 Docker',
      hint: '让管理员把当前账号加入 docker 组，或确认 Docker 服务已启动',
      retryable: false,
    }
  }
  // 镜像仓库鉴权：pull 失败最常见且修法非显然（不是本机 docker.sock 权限）
  if (/unauthorized|authentication required|denied: requested access|no basic auth credentials/i.test(e)) {
    return {
      code: 'DOCKER_AUTH',
      message: '镜像仓库认证失败',
      hint: '先在目标主机登录镜像仓库（docker login），或确认镜像是否公开，再重试拉取',
      retryable: false,
    }
  }
  // Docker Hub 匿名拉取限流：等一段时间或登录后重试
  if (/toomanyrequests|pull rate limit/i.test(e)) {
    return {
      code: 'DOCKER_RATE_LIMIT',
      message: '镜像仓库限流',
      hint: '稍后重试；在目标主机执行 docker login 后重新拉取可提高额度',
      retryable: true,
    }
  }
  // 目标已不存在（多因列表未刷新）
  if (/No such (container|image|network|volume)/i.test(e)) {
    return {
      code: 'DOCKER_NOT_FOUND_OBJ',
      message: '这个资源已经不在了',
      hint: '可能已被删除；刷新列表再试',
      retryable: true,
    }
  }
  // 卷/网络仍被容器占用：rm 会失败，须先停并删关联容器；prune 只清理未被引用项
  if (/volume is in use|being used by some container|network .* has active endpoints|network is not empty/i.test(e)) {
    return {
      code: 'DOCKER_IN_USE',
      message: '资源仍被容器使用',
      hint: '先停止并删除引用它的容器，再重试；或用「清理未使用」回收未引用的卷 / 网络',
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
