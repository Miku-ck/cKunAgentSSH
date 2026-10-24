/**
 * Git 仓库管理 —— 纯逻辑（命令构造 / 输出解析 / 排序 / 拉取结果解释）。
 *
 * 位置对照 github-accel-core.ts：主进程（electron/git-repos.ts）与渲染层共用，
 * 无 Electron / Node 依赖（shared/ 同时被 tsconfig.web 收，那边没有 node 类型）。
 * 本地执行（execFile + `-C`，不经 shell）在 electron/git-repos.ts；
 * 远程执行由 GitReposPanel 构造本文件的命令后经 sshExec 下发。
 *
 * 取数口径：一仓一条命令拿全列表所需信息（status / 最近提交 / origin），
 * 远程用 \x1e（RS）分段的复合命令，一次 sshExec 取回三段。
 *
 * 命令安全：SSH exec 通道没有 argv 形态，远程命令只能是字符串 —— 路径一律经
 * shSingleQuote（POSIX 单引号转义，与 git sq_quote 同法）包裹，登记侧再由
 * isValidRemoteRepoPath 做前置校验（拒绝引号与控制字符），双保险。
 */

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

export type RepoRowKind = 'dirty' | 'diverge' | 'behind' | 'ahead' | 'none' | 'clean'

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
  originHost: string | null
  originSlug: string | null
  lastCommit: GitCommitInfo | null
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
  originUrl?: string | null
}

/** 注册表结构（userData/git-repos.json；类型两端共用，实现在 electron/git-repos.ts） */
export interface GitReposRegistry {
  version: 1
  /** 本地仓库绝对路径 */
  local: string[]
  /** 远程仓库路径：connectionId → 服务器上的仓库路径（执行经渲染层 sshExec） */
  remote: Record<string, string[]>
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

export function repoRowKind(r: Pick<RepoRow, 'dirtyFiles' | 'ahead' | 'behind' | 'originUrl'>): RepoRowKind {
  if (r.dirtyFiles > 0) return 'dirty'
  if (r.ahead > 0 && r.behind > 0) return 'diverge'
  if (r.behind > 0) return 'behind'
  if (r.ahead > 0) return 'ahead'
  if (!r.originUrl) return 'none'
  return 'clean'
}

/** 异常优先：脏区 > 分歧 > 落后 > 领先 > 无远端 > 干净；同档按名称 */
export const REPO_KIND_RANK: Record<RepoRowKind, number> = { dirty: 0, diverge: 1, behind: 2, ahead: 3, none: 4, clean: 5 }

export function sortRepoRows<T extends Pick<RepoRow, 'name' | 'dirtyFiles' | 'ahead' | 'behind' | 'originUrl'>>(rows: T[]): T[] {
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
    originHost: origin?.host ?? null,
    originSlug: origin?.slug ?? null,
    lastCommit: r.lastCommit ?? null,
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

/** POSIX 单引号转义：远程命令里的路径一律经此包裹（与 git sq_quote 同法） */
export function shSingleQuote(s: string): string {
  return `'${String(s).replace(/'/g, `'\\''`)}'`
}

/** 远程命令三段之间的记录分隔符（printf '\x1e' 输出） */
export const REMOTE_SEP = '\x1e'

export const GIT_LOG_FORMAT = '%H%x1f%s%x1f%an%x1f%cr'

/**
 * 远程一仓取数：status / 最近提交 / origin URL 三段，一次 sshExec 拿全。
 * 段间用 `;` 而非 `&&`——单段失败（比如空仓库 log 无输出）不该拖死其余段。
 */
export function buildRemoteRepoStatusCmd(path: string): string {
  const p = shSingleQuote(path)
  return [
    `git -C ${p} status --porcelain=v1 -b --ignore-submodules=dirty`,
    `printf '\\x1e'`,
    `git -C ${p} log -1 --format='${GIT_LOG_FORMAT}'`,
    `printf '\\x1e'`,
    `git -C ${p} config --get remote.origin.url`,
  ].join(' ; ')
}

/** 远程仓库详情：工作区文件 / 分支表 / 最近 20 条提交，三段一次取回 */
export function buildRemoteRepoDetailCmd(path: string): string {
  const p = shSingleQuote(path)
  return [
    `git -C ${p} status --porcelain=v1`,
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

export type RemoteGitAction = 'fetch' | 'pull'

export function buildRemoteGitActionCmd(path: string, action: RemoteGitAction): string {
  const p = shSingleQuote(path)
  return action === 'fetch'
    ? `git -C ${p} fetch --all --prune`
    : `git -C ${p} pull --ff-only`
}

// ─── 远程回包解析 ───

export function splitBySep(raw: string): string[] {
  return raw.split(REMOTE_SEP)
}

export function firstLine(s: string): string {
  return (s || '').split('\n').map(l => l.trim()).find(Boolean) || ''
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
    originUrl: null, originHost: null, originSlug: null, lastCommit: null,
  }
  const sections = splitBySep(stdout)
  if (sections.length < 3) {
    return { ...base, error: firstLine(stderr) || '远端取数失败（回包分段缺失）' }
  }
  const [statusRaw, logRaw, originRaw] = sections
  const status = parseGitStatusPorcelain(statusRaw)
  if (!status.branch && !status.files.length && stderr.trim()) {
    return { ...base, error: firstLine(stderr) }
  }
  const originUrl = originRaw.trim() || null
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
  return { kind: 'failed', message: firstLine(stderr) || firstLine(stdout) || `退出码 ${exitCode ?? 'null'}` }
}
