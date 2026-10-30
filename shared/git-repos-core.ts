/**
 * Git 仓库管理 —— 纯逻辑（命令构造 / 输出解析 / 排序 / 拉取结果解释）。
 *
 * 位置对照 github-accel-core.ts：主进程（electron/git-repos.ts）与渲染层共用，
 * 无 Electron / Node 依赖（shared/ 同时被 tsconfig.web 收，那边没有 node 类型）。
 * 本地执行（execFile + `-C`，不经 shell）在 electron/git-repos.ts；
 * 远程执行由 GitReposPanel 构造本文件的命令后经 sshExec 下发。
 *
 * 取数口径：一仓一条命令拿全列表所需信息（status / 最近提交 / remote 配置），
 * 远程用 \x1e（RS）分段的复合命令，一次 sshExec 取回三段。
 *
 * 命令安全：SSH exec 通道没有 argv 形态，远程命令只能是字符串 —— 路径一律经
 * shSingleQuote（POSIX 单引号转义，与 git sq_quote 同法）包裹，登记侧再由
 * isValidRemoteRepoPath 做前置校验（拒绝引号与控制字符），双保险。
 */

import { shSingleQuote } from './sh-quote-core'
import { stripAnsi } from './ansi-core'

// ─── 类型 ───

export interface GitFileEntry {
  /** porcelain 的 XY 两字符（如 M / A / ??） */
  status: string
  /** 重命名已归一到新路径 */
  path: string
}

export interface GitCommitInfo {
  hash: string
  subject: string
  author: string
  /** 相对时间（%cr：2 hours ago…），展示原样 */
  relTime: string
}

export interface GitBranchInfo {
  name: string
  /** 上游（origin/main 等）；无上游为 null */
  upstream: string | null
  /** -vv 只在非零时标注 ahead/behind；未标注为 null（不折成 0） */
  ahead: number | null
  behind: number | null
  current: boolean
}

export interface RepoStatus {
  branch: string
  detached: boolean
  upstream: string | null
  ahead: number
  behind: number
  files: GitFileEntry[]
}

export type RepoRowKind = 'error' | 'dirty' | 'diverge' | 'behind' | 'ahead' | 'none' | 'clean'

export interface RepoRow {
  id: string
  scope: 'local' | 'remote'
  /** remote 行的宿主会话 id */
  sessionId?: string
  name: string
  path: string
  branch: string
  detached: boolean
  upstream: string | null
  ahead: number
  behind: number
  dirtyFiles: number
  originUrl: string | null
  /** 展示用远端的名字（pickRemote 挑选；push -u 用它兜底无上游场景） */
  remoteName?: string | null
  originHost: string | null
  originSlug: string | null
  lastCommit: GitCommitInfo | null
  /** 主仓 remote 名（primaryRemoteOf：GitCode 优先）；本地推送实际快进推到它 */
  primaryRemote?: string | null
  /** 全部远端名（字典序）：详情概要条要写清「获取」到底覆盖哪几家 */
  remoteNames?: string[]
  /** 单仓取数失败（列表尽力而为，按行上浮，不伪装成「没有仓库」） */
  error?: string
}

export interface RepoDetail {
  files: GitFileEntry[]
  branches: GitBranchInfo[]
  commits: GitCommitInfo[]
}

/** 本地仓库一次取数的结果（electron/git-repos.ts statusAll 的行结构） */
export interface LocalRepoStatusResult {
  path: string
  ok: boolean
  error?: string
  status?: RepoStatus
  lastCommit?: GitCommitInfo | null
  /** 展示用远端 URL（pickRemote 挑选：upstream 的远端 > origin > 字典序第一个） */
  originUrl?: string | null
  /** 同口径挑出的远端名字（push -u 兜底用） */
  remoteName?: string | null
  /** 全部远端（名 → fetch URL）；primaryRemote 由它推导 */
  remotes?: Record<string, string>
}

/** 注册表结构（userData/git-repos.json；类型两端共用，实现在 electron/git-repos.ts） */
export interface GitReposRegistry {
  version: 2
  /** 本地仓库绝对路径 */
  local: string[]
  /** 远程仓库路径：connectionId → 服务器上的仓库路径（执行经渲染层 sshExec） */
  remote: Record<string, string[]>
  /** 三仓同步配置：本地仓库路径 → 主仓/镜像划分（缺省按 deriveSyncConfig 推默认） */
  sync?: Record<string, RepoSyncConfig>
}

/** 三仓同步的单仓配置：主仓唯一可写，其余远端一律视为只读镜像 */
export interface RepoSyncConfig {
  /** 主仓 remote 名（推送与巡检比对的基准） */
  primary: string
  /** 只读镜像 remote 名（补齐 = 快进直推；分叉绝不覆盖） */
  mirrors: string[]
}

// ─── 三仓同步：端点三态 / ls-remote 与 rev-list 解析 / 默认配置推导 ───

/**
 * 远端端点相对本地 HEAD 的四态结论：
 * ok 一致 / behind 远端可快进（安全补齐）/ diverged 两边各有提交（必须显式合并）/
 * unknown 探测失败（网络 / 认证，state 与计数都不可信）。
 */
export type SyncEndpointState = 'ok' | 'behind' | 'diverged' | 'unknown'

/** 同步视图里的一个远端端点（探针结果行） */
export interface SyncEndpoint {
  name: string
  url: string
  role: 'primary' | 'mirror'
  state: SyncEndpointState
  /** 远端分支当前 SHA（ls-remote；探测失败为 null） */
  sha: string | null
  /** behind 态的补齐量（远端落后本地的提交数；其余态为 0） */
  behind: number
  /** unknown 态的失败原因（friendlyGitError 已翻好） */
  error?: string
}

/** 端点状态 → 圆点色档（返回值直接当样式类名用：ok / behind / diverged / off） */
export function syncDotClass(ep: SyncEndpoint): 'ok' | 'behind' | 'diverged' | 'off' {
  if (ep.state === 'ok') return 'ok'
  if (ep.state === 'behind') return 'behind'
  if (ep.state === 'diverged') return 'diverged'
  return 'off'
}

/** 端点状态文案：列表行、远端条、远端页签共用同一套说法（含 behind 的补齐量） */
export function syncStateText(ep: SyncEndpoint): string {
  if (ep.state === 'ok') return '一致'
  if (ep.state === 'behind') return `落后 ${ep.behind || 1}`
  if (ep.state === 'diverged') return '分叉'
  return '探测失败'
}

/** 单仓一次同步探测的结果（electron/git-repos.ts syncProbe 的行结构） */
export interface SyncProbeResult {
  path: string
  ok: boolean
  error?: string
  branch: string
  headSha: string
  /** 主仓名（sync 配置或推导默认） */
  primary: string
  endpoints: SyncEndpoint[]
  /** 探测时刻（Date.now()，渲染层算「N 分钟前」用） */
  probedAt: number
}

/** 补齐回包里的单远端结论 */
export interface SyncPushItem {
  remote: string
  outcome: 'pushed' | 'rejected' | 'failed' | 'skipped'
  /** pushed 之外的中文原因（skipped 说明为何跳过） */
  message: string
}

/** 一键补齐的回包（逐家回报，非原子） */
export interface SyncCatchupResult {
  path: string
  items: SyncPushItem[]
}

/** 解析 `git ls-remote <remote> <branch>` 输出：`<sha>\t<ref>` 行取首行 sha */
export function parseLsRemoteSha(raw: string): string | null {
  for (const line of raw.split('\n')) {
    const m = line.match(/^([0-9a-f]{40})\t/)
    if (m) return m[1]
  }
  return null
}

/**
 * 解析 `git rev-list --left-right --count HEAD...<sha>`：输出 `<左>\t<右>`，
 * 左 = 仅本地有的提交数（远端落后量），右 = 仅远端有的提交数。
 * 形态不符返回 null（当作「远端有本地未见的提交」处理，见 classifySyncEndpoint）。
 */
export function parseRevListCount(raw: string): { left: number; right: number } | null {
  const m = raw.trim().match(/^(\d+)\t(\d+)$/)
  return m ? { left: Number(m[1]), right: Number(m[2]) } : null
}

/**
 * 端点三态判定：
 * - remoteSha 为 null（ls-remote 失败）→ unknown；
 * - 与本地一致 → ok；
 * - counts 为 null（远端 SHA 本地不认识：没 fetch 过的远端独有提交）→ diverged；
 * - 右为 0 且左 > 0 → behind（快进补齐左个提交）；
 * - 其余（右 > 0）→ diverged。
 */
export function classifySyncEndpoint(
  localSha: string,
  remoteSha: string | null,
  counts: { left: number; right: number } | null,
): SyncEndpointState {
  if (!remoteSha) return 'unknown'
  if (remoteSha === localSha) return 'ok'
  if (!counts) return 'diverged'
  if (counts.right === 0 && counts.left > 0) return 'behind'
  return 'diverged'
}

/** GitCode 主仓域名（host 命中即视为平台主仓，其余远端按只读镜像处理） */
const GITCODE_HOSTS = ['gitcode.com', 'gitcode.net']

/**
 * 从远端 URL 提取 host（小写）：兼容 https / ssh / git 协议与 scp 形式
 * `git@gitcode.com:owner/repo.git`。无法识别返回 null。
 */
export function remoteHostOf(url: string): string | null {
  const u = (url || '').trim()
  if (!u) return null
  // 协议形式：直接用 URL 解析
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(u)) {
    try {
      const host = new URL(u).hostname
      return host ? host.toLowerCase() : null
    } catch {
      return null
    }
  }
  // scp 形式：[user@]host:path（host 不含 / 与 :，path 可有 /）
  const scp = u.match(/^(?:[^@\s]+@)?([^:/@\s]+):/)
  if (scp) return scp[1].toLowerCase()
  return null
}

/**
 * 主仓 remote 名推导（仓库 / 同步两模块共用，唯一判据）：
 * 1) host 命中 GitCode 的远端优先（应用只推主仓，GitHub/Gitee 走平台镜像）；
 * 2) 回退 branch.<name>.remote 指向的远端；
 * 3) 回退 origin；
 * 4) 回退字典序第一个。
 * 无远端返回 null。
 */
export function primaryRemoteOf(remotes: Record<string, string>, branchRemote: string | null): string | null {
  const names = Object.keys(remotes)
  if (!names.length) return null
  const sorted = names.slice().sort()
  for (const host of GITCODE_HOSTS) {
    const hit = sorted.find(n => remoteHostOf(remotes[n]) === host)
    if (hit) return hit
  }
  if (branchRemote && names.includes(branchRemote)) return branchRemote
  if (remotes.origin) return 'origin'
  return sorted[0]
}

/**
 * 默认主仓推导（无手动配置时，也是唯一判据）：按固定优先级选主仓，其余远端全部为镜像。
 * 优先级：gitcode → gitee → github → 其余任意远端（首个）；无远端则为空。
 * 推送 / 镜像划分都以此为准。
 */
const PRIMARY_PRIORITY = ['gitcode', 'gitee', 'github']
export function deriveSyncConfig(remotes: Record<string, string>): RepoSyncConfig {
  const names = Object.keys(remotes)
  if (!names.length) return { primary: '', mirrors: [] }
  const primary = PRIMARY_PRIORITY.find(n => names.includes(n)) ?? names.find(n => !PRIMARY_PRIORITY.includes(n)) ?? ''
  return { primary, mirrors: names.filter(n => n !== primary) }
}

/** 本地 git 命令的原样回包（fetch/pull 由渲染层 interpretPull 解释） */
export interface GitRawResult {
  code: number
  stdout: string
  stderr: string
}

// ─── 状态解析：git status --porcelain=v1 -b ───

/**
 * 解析 `git status --porcelain=v1 -b --ignore-submodules=dirty`。
 * 头行形态：`## main...origin/main [ahead 1, behind 2]` / `## HEAD (no branch)` /
 * `## No commits yet on main` / `## main`（无上游）。文件行 `XY PATH`，
 * 重命名 `R  old -> new` 归一到新路径。
 */
export function parseGitStatusPorcelain(raw: string): RepoStatus {
  const out: RepoStatus = { branch: '', detached: false, upstream: null, ahead: 0, behind: 0, files: [] }
  for (const line of raw.split('\n')) {
    if (!line) continue
    if (line.startsWith('## ')) {
      const header = line.slice(3)
      if (/^HEAD \(no branch\)/.test(header)) {
        out.detached = true
        out.branch = 'HEAD'
        continue
      }
      const noCommit = header.match(/^No commits yet on (.+)$/)
      if (noCommit) {
        out.branch = noCommit[1]
        continue
      }
      const bracket = header.match(/\[([^\]]*)\]\s*$/)
      if (bracket) {
        const ahead = bracket[1].match(/\bahead (\d+)/)
        const behind = bracket[1].match(/\bbehind (\d+)/)
        out.ahead = ahead ? Number(ahead[1]) : 0
        out.behind = behind ? Number(behind[1]) : 0
      }
      const headPart = (bracket ? header.slice(0, header.indexOf(bracket[0])) : header).trim()
      const dots = headPart.indexOf('...')
      if (dots >= 0) {
        out.branch = headPart.slice(0, dots)
        out.upstream = headPart.slice(dots + 3)
      } else {
        out.branch = headPart
      }
    } else if (line.length >= 4) {
      const p = line.slice(3)
      const arrow = p.indexOf(' -> ')
      out.files.push({ status: line.slice(0, 2).trim() || '?', path: arrow >= 0 ? p.slice(arrow + 4) : p })
    }
  }
  return out
}

// ─── 分支 / 提交解析 ───

/** 解析 `git branch -vv`：`* dev  a1b2c4d [origin/dev: ahead 1, behind 2] 主题` */
export function parseBranchVv(raw: string): GitBranchInfo[] {
  const out: GitBranchInfo[] = []
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue
    const m = line.match(/^([*+])?\s*(\S+)\s+([0-9a-f]{7,40})\s+(?:\[([^\]]+)\]\s*)?(.*)$/)
    if (!m) continue
    const info: GitBranchInfo = { name: m[2], upstream: null, ahead: null, behind: null, current: m[1] === '*' }
    if (m[4]) {
      // 方括号三种形态：`[up]`（已同步）/ `[up: ahead N, behind M]` / `[up: gone]`
      const sep = m[4].lastIndexOf(': ')
      const statusPart = sep >= 0 ? m[4].slice(sep + 2) : ''
      info.upstream = sep >= 0 ? m[4].slice(0, sep) : m[4]
      if (statusPart !== 'gone') {
        const ahead = statusPart.match(/\bahead (\d+)/)
        const behind = statusPart.match(/\bbehind (\d+)/)
        info.ahead = ahead ? Number(ahead[1]) : 0
        info.behind = behind ? Number(behind[1]) : 0
      }
      // gone：上游已删，ahead/behind 未知，保持 null（不折成 0）
    }
    out.push(info)
  }
  return out
}

/** 解析 `git log --format=%H%x1f%s%x1f%an%x1f%cr`（每行一条提交） */
export function parseGitLog(raw: string): GitCommitInfo[] {
  const out: GitCommitInfo[] = []
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue
    const parts = line.split('\x1f')
    if (parts.length < 4) continue
    out.push({ hash: parts[0], subject: parts[1], author: parts[2], relTime: parts[3] })
  }
  return out
}

// ─── 提交图谱（git log --graph --all 的输出解析 + SVG 车道布局） ───

/** 图谱行：一条提交一行（--graph 的 ASCII 图谱列与连接线行不保留——SVG 车道的拓扑由 parents 推导） */
export interface GraphCommitRow {
  commit: GitCommitInfo
  /** %D 的引用名（HEAD -> main / origin/main / tag: v1.2），空数组 = 无装饰 */
  refs: string[]
  /** %P 父提交全哈希（空格分隔；根提交为空数组）——SVG 车道图的连线依据 */
  parents: string[]
}

/**
 * 图谱格式串：\x01 领起提交字段（与前面的图谱字符列切分），\x1f 分字段，
 * 尾部 %D 内联引用名、%P 内联父哈希——一条命令拿全拓扑 + 提交 + 分支指针 +
 * 父子关系，不需要 for-each-ref。--graph 自带 topological order（git 约定），
 * 无需再指定排序。
 */
export const GIT_GRAPH_FORMAT = '%x01%H%x1f%s%x1f%an%x1f%cr%x1f%D%x1f%P'

/** 图谱条数上限：防御巨型仓库把输出顶爆（本地 4MB maxBuffer / 远程 1MB ssh 截断） */
export const GRAPH_MAX_LIMIT = 500

function clampGraphLimit(limit: number): number {
  const n = Math.floor(Number(limit))
  if (!Number.isFinite(n) || n < 1) return 30
  return Math.min(n, GRAPH_MAX_LIMIT)
}

/** 本地图谱取数参数（electron/git-repos.ts 经 execFile 数组下发，不经 shell） */
export function buildGraphLogArgs(limit: number): string[] {
  return ['log', '-n', String(clampGraphLimit(limit)), '--graph', '--all', `--format=${GIT_GRAPH_FORMAT}`]
}

/** 远程一仓图谱：单命令取回（复用现有 sshExec 通道） */
export function buildRemoteGraphCmd(path: string, limit: number): string {
  return `git -C ${shSingleQuote(path)} log -n ${clampGraphLimit(limit)} --graph --all --format='${GIT_GRAPH_FORMAT}'`
}

/**
 * 解析 `git log --graph --all --format=<GIT_GRAPH_FORMAT>` 输出，只保留提交行：
 * `<图谱字符>\x01<hash>\x1f<subject>\x1f<author>\x1f<relTime>\x1f<refs>\x1f<parents>`。
 * \x01 之前是 --graph 的 ASCII 图谱列（仍要保留 --graph 参数：它带来的
 * topological order 保证父提交总在子提交之后，是车道布局的前提），解析时丢弃。
 * 连接线行（只有图谱字符的行）同样丢弃——其拓扑已完整编码在 parent 边里。
 */
export function parseGraphLog(raw: string): GraphCommitRow[] {
  const rows: GraphCommitRow[] = []
  for (const line of raw.split('\n')) {
    if (!line) continue
    const sep = line.indexOf('\x01')
    if (sep < 0) continue
    const fields = line.slice(sep + 1).split('\x1f')
    if (fields.length < 4) continue
    const refs = (fields[4] || '').split(',').map(s => s.trim()).filter(Boolean)
    rows.push({
      commit: { hash: fields[0], subject: fields[1], author: fields[2], relTime: fields[3] },
      refs,
      parents: (fields[5] || '').split(' ').filter(Boolean),
    })
  }
  return rows
}

/** 引用徽章的着色档：HEAD/当前分支、本地分支、远端引用、标签 */
export type GraphRefKind = 'head' | 'local' | 'remote' | 'tag'

export function graphRefKind(ref: string): GraphRefKind {
  if (ref.startsWith('HEAD')) return 'head'
  if (ref.startsWith('tag:')) return 'tag'
  if (ref.includes('/')) return 'remote'
  return 'local'
}

/** 这次取回的提交数是否打满了 limit（「加载更多」的可见性判据；触到 500 上限则不再给） */
export function graphHasMore(rows: GraphCommitRow[], limit: number): boolean {
  const capped = clampGraphLimit(limit)
  return capped < GRAPH_MAX_LIMIT && rows.length === capped
}

// ─── SVG 车道图布局（由 parents 推车道与连线） ───

/** 车道图的一条提交行：lane = 车道号，edges = 指向各父提交（行号 + 车道）的连线 */
export interface GraphLaneRow {
  row: GraphCommitRow
  lane: number
  edges: Array<{ toRow: number; toLane: number }>
}

export interface GraphLaneLayout {
  rows: GraphLaneRow[]
  laneCount: number
}

/**
 * 由提交行（含 parents）推车道布局，替代 --graph 的 ASCII 文字档：
 * 从最新到最旧单趟扫描，lanes[i] 记「下一个期望出现在车道 i 的哈希」。
 * 首父继承当前车道（主干延续），其余父提交占空闲车道或汇入既有车道。
 * 父提交不在本次加载范围（截断 / -n 上限）时该边跳过，车道在无后续边时释放。
 */
export function layoutGraphLanes(rows: GraphCommitRow[]): GraphLaneLayout {
  const rowIndex = new Map<string, number>()
  rows.forEach((r, i) => { if (!rowIndex.has(r.commit.hash)) rowIndex.set(r.commit.hash, i) })
  const lanes: Array<string | null> = []
  const out: GraphLaneRow[] = []
  const takeLane = (hash: string): number => {
    const hit = lanes.indexOf(hash)
    if (hit >= 0) return hit
    const free = lanes.indexOf(null)
    if (free >= 0) { lanes[free] = hash; return free }
    lanes.push(hash)
    return lanes.length - 1
  }
  for (const r of rows) {
    const h = r.commit.hash
    const lane = takeLane(h)
    const edges: GraphLaneRow['edges'] = []
    r.parents.forEach((p, pi) => {
      const toRow = rowIndex.get(p)
      if (toRow === undefined) return
      let toLane: number
      if (pi === 0) {
        lanes[lane] = p
        toLane = lane
      } else {
        toLane = takeLane(p)
      }
      edges.push({ toRow, toLane })
    })
    if (!edges.length) lanes[lane] = null
    out.push({ row: r, lane, edges })
  }
  return { rows: out, laneCount: Math.max(1, lanes.length) }
}

// ─── origin URL → host / slug ───

/** https / scp / ssh 带端口三种写法都归一到 { host, slug }；识别不了返回 null */
export function parseOriginUrl(url: string): { host: string; slug: string } | null {
  const u = url.trim().replace(/\/+$/, '')
  if (!u) return null
  let m = u.match(/^[a-z][a-z0-9+.-]*:\/\/(?:[^@/\s]+@)?([^/:?\s]+)(?::\d+)?\/(.+)$/i)
  if (m) return { host: m[1], slug: m[2].replace(/\.git$/, '') }
  m = u.match(/^([^@\s]+)@([^:\s]+):(.+)$/)
  if (m) return { host: m[2], slug: m[3].replace(/\.git$/, '') }
  return null
}

// ─── remote 配置解析与展示挑选 ───

/**
 * 解析 `git config --get-regexp '^remote\..*\.url$'` 输出（remote 名 → fetch URL）。
 * 远端不叫 origin（gitee / upstream / 自定义名）同样取得到；同名多值取第一条
 * （git fetch 即用第一条 url）。
 */
export function parseRemoteConfig(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const sep = line.indexOf(' ')
    if (sep <= 0) continue
    const m = line.slice(0, sep).match(/^remote\.(.+)\.url$/)
    if (m?.[1] && !(m[1] in out)) out[m[1]] = line.slice(sep + 1).trim()
  }
  return out
}

/**
 * 展示用远端挑选：当前分支 upstream 指向的远端（最长名匹配，容忍 remote 名含 `/`）
 * > origin（惯例默认）> 字典序第一个。fetch/pull/push 一律 --all / --ff-only /
 * 显式 remote 名，不依赖这里的取舍（这里只决定列表展示哪一列）。
 */
export function pickRemote(remotes: Record<string, string>, upstream: string | null): { name: string; url: string } | null {
  const names = Object.keys(remotes)
  if (!names.length) return null
  if (upstream) {
    const hit = names.filter(n => upstream.startsWith(`${n}/`)).sort((a, b) => b.length - a.length)[0]
    if (hit) return { name: hit, url: remotes[hit] }
  }
  if (remotes.origin) return { name: 'origin', url: remotes.origin }
  const name = [...names].sort()[0]
  return { name, url: remotes[name] }
}

// ─── 行组装与排序 ───

/** 仓库名 = 路径最后一段（Windows / POSIX 分隔符都认） */
export function repoNameOf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

/** 行 id：local:路径 / remote:会话:路径 —— 表格 :key 与弹窗定位用 */
export function repoRowId(scope: 'local' | 'remote', sessionId: string | undefined, path: string): string {
  return scope === 'local' ? `local:${path}` : `remote:${sessionId ?? ''}:${path}`
}

/** 状态判定输入：error 可缺省（纯状态对象也能判档） */
type RepoKindInput = Pick<RepoRow, 'dirtyFiles' | 'ahead' | 'behind' | 'originUrl'> & { error?: string }

export function repoRowKind(r: RepoKindInput): RepoRowKind {
  if (r.error) return 'error'
  if (r.dirtyFiles > 0) return 'dirty'
  if (r.ahead > 0 && r.behind > 0) return 'diverge'
  if (r.behind > 0) return 'behind'
  if (r.ahead > 0) return 'ahead'
  if (!r.originUrl) return 'none'
  return 'clean'
}

/** 异常优先：取数失败 > 脏区 > 分歧 > 落后 > 领先 > 无远端 > 干净；同档按名称 */
export const REPO_KIND_RANK: Record<RepoRowKind, number> = {
  error: 0, dirty: 1, diverge: 2, behind: 3, ahead: 4, none: 5, clean: 6,
}

export function sortRepoRows<T extends Pick<RepoRow, 'name' | 'dirtyFiles' | 'ahead' | 'behind' | 'originUrl'> & { error?: string }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
    const ra = REPO_KIND_RANK[repoRowKind(a)]
    const rb = REPO_KIND_RANK[repoRowKind(b)]
    if (ra !== rb) return ra - rb
    return a.name.localeCompare(b.name, 'zh-Hans-CN')
  })
}

/** 本地取数结果 → 行（electron/git-repos.ts 的返回直接喂这里） */
export function toLocalRepoRow(r: LocalRepoStatusResult): RepoRow {
  const origin = r.originUrl ? parseOriginUrl(r.originUrl) : null
  const remotes = r.remotes ?? {}
  const primaryRemote = deriveSyncConfig(remotes).primary || null
  return {
    id: repoRowId('local', undefined, r.path),
    scope: 'local',
    name: repoNameOf(r.path),
    path: r.path,
    branch: r.status?.branch ?? '',
    detached: r.status?.detached ?? false,
    upstream: r.status?.upstream ?? null,
    ahead: r.status?.ahead ?? 0,
    behind: r.status?.behind ?? 0,
    dirtyFiles: r.status?.files.length ?? 0,
    originUrl: r.originUrl ?? null,
    remoteName: r.remoteName ?? null,
    originHost: origin?.host ?? null,
    originSlug: origin?.slug ?? null,
    lastCommit: r.lastCommit ?? null,
    primaryRemote: primaryRemote || null,
    remoteNames: Object.keys(remotes).sort(),
    error: r.error,
  }
}

// ─── 远程路径校验与命令构造 ───

/**
 * 远程仓库路径的登记门槛：绝对 POSIX 路径，不含引号 / 反斜杠 / 控制字符，
 * 长度 ≤ 512。引号字符是刻意拒绝的 —— build*Cmd 的单引号转义已能安全处理它，
 * 但远程路径携带引号没有任何正当场景，登记侧直接挡掉让转义层永远用不上。
 */
export function isValidRemoteRepoPath(path: string): boolean {
  return typeof path === 'string'
    && path.startsWith('/')
    && path.length <= 512
    && !/['"\\\x00-\x1f]/.test(path)
}

// POSIX 单引号转义的唯一实现在 shared/sh-quote-core.ts（ssh-manager / main 的补全
// 脚本同源）；这里 re-export 维持原 import 路径 —— GitReposPanel 与单测都从本文件拿。
export { shSingleQuote }

/** 远程命令三段之间的记录分隔符（printf '\x1e' 输出） */
export const REMOTE_SEP = '\x1e'

export const GIT_LOG_FORMAT = '%H%x1f%s%x1f%an%x1f%cr'

/**
 * 远程一仓取数：status / 最近提交 / 全部 remote URL 三段，一次 sshExec 拿全。
 * 段间用 `;` 而非 `&&`——单段失败（比如空仓库 log 无输出）不该拖死其余段。
 * status 带 core.quotePath=false：否则 porcelain 把中文文件名输出成
 * "\344\270\255" 八进制转义，列表里没法看；只影响读展示，fetch/pull 不掺。
 * remote 段用 get-regexp 取全部远端：远端不叫 origin 的仓库也能落到 host 徽章
 * （挑选规则见 pickRemote）。
 */
export function buildRemoteRepoStatusCmd(path: string): string {
  const p = shSingleQuote(path)
  return [
    `git -C ${p} -c core.quotePath=false status --porcelain=v1 -b --ignore-submodules=dirty`,
    `printf '\\x1e'`,
    `git -C ${p} log -1 --format='${GIT_LOG_FORMAT}'`,
    `printf '\\x1e'`,
    `git -C ${p} config --get-regexp '^remote\\..*\\.url$'`,
  ].join(' ; ')
}

/** 远程仓库详情：工作区文件 / 分支表 / 最近 20 条提交，三段一次取回 */
export function buildRemoteRepoDetailCmd(path: string): string {
  const p = shSingleQuote(path)
  return [
    `git -C ${p} -c core.quotePath=false status --porcelain=v1`,
    `printf '\\x1e'`,
    `git -C ${p} branch -vv`,
    `printf '\\x1e'`,
    `git -C ${p} log -20 --format='${GIT_LOG_FORMAT}'`,
  ].join(' ; ')
}

/** 远程仓库路径登记前的校验：是 git 工作区则输出 true */
export function buildRemoteRepoVerifyCmd(path: string): string {
  return `git -C ${shSingleQuote(path)} rev-parse --is-inside-work-tree`
}

// ─── 分支切换 / 提交 / 推送：校验、命令构造与结果解释 ───

/**
 * 本地分支名的登记门槛（switch 命令的参数面）：拒绝空 / 前导 `-`（防选项注入）/
 * 空白 / 引号 / 控制字符 / git 引用非法字符 `~^:?*[\` / `..` / `@{` / `.lock` 与
 * `/` 结尾，长度 ≤ 256。分支名来自 parseBranchVv 的本地分支列表，不经用户手输。
 */
export function isValidBranchName(name: string): boolean {
  return typeof name === 'string'
    && name.length > 0
    && name.length <= 256
    && !name.startsWith('-')
    && !/[\s'"`\\~^:?*[\]]/.test(name)
    && !name.includes('..')
    && !name.includes('@{')
    && !name.endsWith('.lock')
    && !name.endsWith('/')
}

/** 提交信息门槛：单行（由输入框保证），去空白后非空，≤ 200 字符，无控制字符 */
export function isValidCommitMsg(msg: string): boolean {
  const v = String(msg ?? '').trim()
  return v.length > 0 && v.length <= 200 && !/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(v)
}

/**
 * 工作区文件路径门槛（commit 的 pathspec 面）：porcelain 输出的仓库相对路径，
 * 拒绝空 / 绝对路径 / `..` 段 / 控制字符，单条 ≤ 1024，一次 ≤ 500 条。
 */
export function isValidCommitFiles(files: unknown): files is string[] {
  if (!Array.isArray(files) || !files.length || files.length > 500) return false
  return files.every(f => {
    if (typeof f !== 'string') return false
    const v = f.trim()
    if (!v || v.length > 1024 || v.startsWith('/') || /^[a-zA-Z]:/.test(v)) return false
    if (v.split('/').includes('..')) return false
    return !/[\x00-\x1f]/.test(v)
  })
}

export type RemoteGitAction = 'fetch' | 'pull'

// ─── 变更文件的自动识别：疑似临时产物 与 提交信息规则草稿 ───

/**
 * 疑似临时产物：untracked（??）文件命中这些模式时，提交弹窗默认不勾选
 * （可勾回）。.gitignore 覆盖的文件根本不会出现在 status 里，这里兜的是
 * 忘了写 .gitignore 的那部分——编辑器备份、系统缩略图、运行日志等。
 */
const NOISE_FILE_RE = /(^|[/\\])(?:\.DS_Store|Thumbs\.db)$|(?:(?:\.log|\.tmp|\.bak|\.swp|\.orig|\.rej)$)/i

export function looksLikeNoiseFile(path: string): boolean {
  return NOISE_FILE_RE.test(path)
}

/**
 * 由变更文件清单推一份 conventional-commits 风格的**规则草稿**（非 AI，
 * 确定性可复算）：全部是测试 → test，全部是文档 → docs，全部是配置/依赖
 * → chore，路径含 fix/bug/hotfix 信号 → fix，其余 → feat；单一顶层目录
 * 作 scope。给「留空自动生成」与 ✦ 按钮用，产出可编辑。
 */
export function draftCommitMessage(files: string[]): string {
  if (!files.length) return ''
  const isTest = (p: string): boolean => /(^|[/\\])(tests?|__tests__)[/\\]/.test(p) || /\.(test|spec)\.[a-z]+$/i.test(p)
  const isDoc = (p: string): boolean => /(^|[/\\])docs?[/\\]/.test(p) || /\.md$/i.test(p)
  const isConfig = (p: string): boolean =>
    /(^|[/\\])(?:\.gitignore|Dockerfile[^/\\]*|docker-compose[^/\\]*)$/.test(p)
    || /\.(?:ya?ml|toml|ini|cfg|json|lock)$/i.test(p)
    || /(^|[/\\])\.github[/\\]/.test(p)
  let type = 'feat'
  if (files.every(isTest)) type = 'test'
  else if (files.every(isDoc)) type = 'docs'
  else if (files.every(isConfig)) type = 'chore'
  else if (files.some(p => /fix|bug|hotfix|patch|crash/i.test(p))) type = 'fix'
  // scope = 全部文件的最深公共目录的最后一段（src/git/… → git）；无公共目录不带 scope
  const dirs = files.map(p => {
    const segs = p.split(/[\\/]/)
    segs.pop()
    return segs.filter(s => s && !s.startsWith('.'))
  })
  const common: string[] = []
  for (let i = 0; ; i++) {
    const seg = dirs[0]?.[i]
    if (!seg || !dirs.every(d => d[i] === seg)) break
    common.push(seg)
  }
  const scope = common.length ? common[common.length - 1] : ''
  const head = scope ? `${type}(${scope})` : type
  if (files.length === 1) {
    const base = files[0].split(/[\\/]/).pop() || files[0]
    return `${head}: 更新 ${base}`
  }
  return `${head}: 调整 ${files.length} 个文件`
}


export function buildRemoteGitActionCmd(path: string, action: RemoteGitAction): string {
  const p = shSingleQuote(path)
  return action === 'fetch'
    ? `git -C ${p} fetch --all --prune`
    : `git -C ${p} pull --ff-only`
}

/** 远程切换分支：DWIM（仅有远程跟踪引用的分支自动建本地分支） */
export function buildRemoteSwitchCmd(path: string, branch: string): string {
  return `git -C ${shSingleQuote(path)} switch ${shSingleQuote(branch)}`
}

/** 远程提交所选文件：add（覆盖未跟踪）+ commit -- pathspec（只提交所选） */
export function buildRemoteCommitCmd(path: string, files: string[], msg: string): string {
  const p = shSingleQuote(path)
  const paths = files.map(f => shSingleQuote(f.trim())).join(' ')
  return `git -C ${p} add -- ${paths} ; git -C ${p} commit -m ${shSingleQuote(msg.trim())} -- ${paths}`
}

/** 远程推送：有上游直接 push，无上游用 -u 建立跟踪 */
export function buildRemotePushCmd(path: string, opts: { remoteName: string; branch: string }): string {
  const p = shSingleQuote(path)
  return `git -C ${p} push -u ${shSingleQuote(opts.remoteName)} ${shSingleQuote(opts.branch)}`
}

// ─── 提交信息 AI 归纳的 diff 取数 ───

/** 远程单仓 diff：选中文件（pathspec）对 HEAD，-U2 收敛上下文（未跟踪文件不在此输出——
 *  git 语义如此，模型从清单的 ?? 状态知晓其为新增） */
export function buildRemoteDiffCmd(path: string, files: string[]): string {
  const p = shSingleQuote(path)
  const paths = files.map(f => shSingleQuote(f.trim())).join(' ')
  return `git -C ${p} -c core.quotePath=false diff HEAD -U2 -- ${paths}`
}

/** diff 文本收敛：超限按行截断并标注（控 LLM 输入；空仓库/无 HEAD 时调用侧拿到的
 *  非 0 退出码直接不发 diff，模型退回清单推断） */
export function clampDiffText(diff: string, maxChars = 6000): string {
  if (diff.length <= maxChars) return diff
  const cut = diff.slice(0, maxChars)
  const lastNl = cut.lastIndexOf('\n')
  return `${lastNl > 0 ? cut.slice(0, lastNl) : cut}\n…（diff 过长已截断）`
}

export type CommitOutcome =
  | { kind: 'committed'; message: string; hash: string | null }
  | { kind: 'failed'; message: string }

/** 解释 `git commit` 回包：退出 0 → 从 `[branch abc1234]` 提取分支与短哈希；失败 → stderr 友好化 */
export function interpretCommit(stdout: string, stderr: string, exitCode: number | null): CommitOutcome {
  if (exitCode === 0) {
    const all = `${stdout}\n${stderr}`
    const hash = all.match(/[ (]([0-9a-f]{7,40})\]/)?.[1] ?? null
    const branch = all.match(/\[([^\s[\]]+)[^\]]*[0-9a-f]{7,40}\]/)?.[1] ?? null
    const where = branch ? `${branch}${hash ? ` ${hash.slice(0, 7)}` : ''}` : (hash ? hash.slice(0, 7) : '')
    return { kind: 'committed', message: where ? `已提交到 ${where}` : '已提交', hash }
  }
  return { kind: 'failed', message: friendlyGitError(stderr) || firstLine(stdout) || `退出码 ${exitCode ?? 'null'}` }
}

export type PushOutcome =
  | { kind: 'pushed'; message: string }
  | { kind: 'rejected'; message: string }
  | { kind: 'failed'; message: string }

/** 解释 `git push` 回包：拒绝（非快进等）单独归类提示先拉取，不笼统报失败 */
export function interpretPush(stdout: string, stderr: string, exitCode: number | null): PushOutcome {
  const all = `${stdout}\n${stderr}`
  if (exitCode === 0) {
    if (/new branch|set.?up|upstream/i.test(all)) return { kind: 'pushed', message: '已推送并建立上游跟踪' }
    return { kind: 'pushed', message: '已推送，远程分支已更新' }
  }
  if (/rejected|fetch first|non-fast-forward/i.test(all)) {
    return { kind: 'rejected', message: '远端有你没有的提交：先拉取（pull）合并后再推送' }
  }
  return { kind: 'failed', message: friendlyGitError(stderr) || firstLine(stdout) || `退出码 ${exitCode ?? 'null'}` }
}


// ─── 远程回包解析 ───

export function splitBySep(raw: string): string[] {
  return raw.split(REMOTE_SEP)
}

export function firstLine(s: string): string {
  return (s || '').split('\n').map(l => l.trim()).find(Boolean) || ''
}

// ANSI 剥离的唯一强实现（CSI+OSC+Fe）在 shared/ansi-core.ts，这里 re-export 维持
// 原 import 路径（单测从本文件拿）。用途：托管端（Gitee 等）的 remote: 消息自带
// 颜色码，不剥掉原样进 toast 是乱码 —— friendlyGitError 匹配前必经这一步。
export { stripAnsi }

/**
 * 常见 git 失败 stderr → 中文指引（按特异性排序，首条命中即用）。对整段
 * stderr 匹配：认证失败常出现在 remote: 行之后的 fatal: 行。认不出的
 * 不硬翻——stderr 首行原文仍是最有诊断价值的信息。
 */
const GIT_ERROR_PATTERNS: ReadonlyArray<{ re: RegExp; msg: string }> = [
  { re: /access token is expired/i, msg: '远端访问令牌已过期：请在仓库所在机器重新生成令牌并更新 git 凭证后重试' },
  { re: /permission denied \(publickey\)|no supported authentication methods/i, msg: 'SSH 密钥认证被拒：请在仓库所在机器配置可用密钥，或改用 HTTPS 远程地址' },
  { re: /host key verification failed/i, msg: 'SSH 主机指纹校验失败：请在仓库所在机器手动 git 连接一次并确认指纹后重试' },
  { re: /authentication failed|authentication required|invalid username or password|http basic: access denied|\b401\b|unauthorized/i, msg: '远端认证失败：凭证（令牌 / 账号密码）无效或已过期，请更新 git 凭证后重试' },
  { re: /\b403\b|forbidden|access denied|not allowed/i, msg: '远端拒绝访问：当前账号缺少该仓库的权限，请检查账号授权' },
  { re: /repository not found|\b404\b/i, msg: '远端仓库不存在或无权访问：请检查 remote 地址与账号权限' },
  { re: /could not resolve host|failed to connect|connection timed out|connection refused|network is unreachable/i, msg: '无法连接远端：请检查网络、代理或 VPN 后重试' },
  { re: /ssl certificate problem|certificate verify failed/i, msg: 'TLS 证书校验失败：远端证书过期或不受本机信任，请更新证书 / CA 配置' },
  { re: /not a git repository/i, msg: '该路径不是 git 仓库（.git 缺失或已损坏）' },
  { re: /would be overwritten|please commit your changes or stash/i, msg: '本地有未提交变更会被覆盖：请先提交或 stash 后重试' },
]

/**
 * git stderr → 用户能直接懂的话（本地 execFile 与远程 sshExec 的失败回包共用）：
 * 剥 ANSI 色码 → 模式表给中文指引 → 认不出的保留首行原文。
 * 'git-not-found' 是 electron/git-repos.ts 的约定哨兵，同样转友好文案。
 */
export function friendlyGitError(stderr: string, fallback = ''): string {
  const clean = stripAnsi(String(stderr ?? ''))
  if (clean === 'git-not-found') return '未检测到 git（请安装 git 或加入 PATH 后重试）'
  const line = firstLine(clean)
  if (!line) return fallback
  const hit = GIT_ERROR_PATTERNS.find(p => p.re.test(clean))
  return hit ? hit.msg : line
}

/** fetch-all 汇总的失败明细：按仓库列出，最多 3 条防 toast 顶爆，余量收口成计数 */
export function formatFetchFailures(fails: string[]): string {
  if (!fails.length) return ''
  const shown = fails.slice(0, 3).join('；')
  return fails.length > 3 ? `${shown}（另有 ${fails.length - 3} 项）` : shown
}

/** 远程 status 回包 → 行（stdout 三段 + stderr；整体失败时置 error） */
export function toRemoteRepoRow(sessionId: string, path: string, stdout: string, stderr: string): RepoRow {
  const base: RepoRow = {
    id: repoRowId('remote', sessionId, path),
    scope: 'remote',
    sessionId,
    name: repoNameOf(path),
    path,
    branch: '', detached: false, upstream: null, ahead: 0, behind: 0, dirtyFiles: 0,
    originUrl: null, remoteName: null, originHost: null, originSlug: null, lastCommit: null,
  }
  const sections = splitBySep(stdout)
  if (sections.length < 3) {
    return { ...base, error: friendlyGitError(stderr, '远端取数失败（回包分段缺失）') }
  }
  const [statusRaw, logRaw, remotesRaw] = sections
  const status = parseGitStatusPorcelain(statusRaw)
  if (!status.branch && !status.files.length && stderr.trim()) {
    return { ...base, error: friendlyGitError(stderr, '远端取数失败') }
  }
  const remotes = parseRemoteConfig(remotesRaw)
  const pick = pickRemote(remotes, status.upstream)
  const originUrl = pick?.url ?? null
  const origin = originUrl ? parseOriginUrl(originUrl) : null
  const commits = parseGitLog(logRaw)
  return {
    ...base,
    branch: status.branch,
    detached: status.detached,
    upstream: status.upstream,
    ahead: status.ahead,
    behind: status.behind,
    dirtyFiles: status.files.length,
    originUrl,
    remoteName: pick?.name ?? null,
    remoteNames: Object.keys(remotes).sort(),
    originHost: origin?.host ?? null,
    originSlug: origin?.slug ?? null,
    lastCommit: commits[0] ?? null,
  }
}

/** 详情回包（status/branch -vv/log 三段）→ RepoDetail */
export function parseRepoDetailSections(statusRaw: string, branchRaw: string, logRaw: string): RepoDetail {
  return {
    files: parseGitStatusPorcelain(statusRaw).files,
    branches: parseBranchVv(branchRaw),
    commits: parseGitLog(logRaw),
  }
}

// ─── 拉取结果解释 ───

export type PullOutcome =
  | { kind: 'up-to-date'; message: string }
  | { kind: 'fast-forwarded'; message: string }
  | { kind: 'diverged'; message: string }
  | { kind: 'failed'; message: string }

/**
 * 解释 `git pull --ff-only` 的回包。分叉（not possible to fast-forward /
 * divergent branches）单独归类：那是本工具刻意的安全边界——不自动合并，
 * 提示用户手动处理，而不是笼统报「失败」。
 */
export function interpretPull(stdout: string, stderr: string, exitCode: number | null): PullOutcome {
  const all = `${stdout}\n${stderr}`
  if (exitCode === 0 || /Already up to date/.test(all)) {
    if (/Fast-forward/i.test(all)) return { kind: 'fast-forwarded', message: '已快进合并远端提交' }
    return { kind: 'up-to-date', message: '已是最新，无远端更新' }
  }
  if (/not possible to fast-forward|divergent branches|Not possible to fast-forward/i.test(all)) {
    return { kind: 'diverged', message: '本地与远端已分叉：本工具不做自动合并（--ff-only），请手动处理后再拉取' }
  }
  return { kind: 'failed', message: friendlyGitError(stderr) || firstLine(stdout) || `退出码 ${exitCode ?? 'null'}` }
}
