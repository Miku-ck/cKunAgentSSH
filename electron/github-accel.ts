/**
 * GitHub 加速 —— 主进程服务（github-accel.json 持久化 + 镜像测速 + git/ssh 配置管理）
 *
 * 持久化骨架照抄 tunnel-store.ts：读-改-写串行化 / tmp+rename 原子写 / 损坏隔离。
 * git 操作走 execFile（不经 shell，无注入面）；测速走 node https（TTFB 计时）。
 * 纯逻辑（配置校验 / insteadOf 参数 / ssh 托管块）在 shared/github-accel-core.ts。
 */
import { app } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink, copyFile } from 'fs/promises'
import { existsSync } from 'fs'
import { homedir } from 'os'
import { execFile } from 'child_process'
import { lookup } from 'dns/promises'
import https from 'https'
import type { Agent, ClientRequest } from 'http'
import { tcpLatency } from './utils/tcp-latency'
import { resolveGitExecutable } from './utils/git-exe'
import { detectSystemProxy, maskProxy, tunnelAgentFor, type ProxyDetection } from './utils/proxy'
import type { GhHostsService } from './gh-hosts'
import {
  AUTO_ADOPT_LIMIT,
  AUTO_OK_TARGET,
  applySshBlock,
  buildGitReadArgs,
  buildGitPushKeepArgs,
  buildGitPushSshArgs,
  buildGitPushUnsetSubsectionArgs,
  buildGitRestoreArgs,
  buildGitSetArgs,
  buildGitUnsetSubsectionArgs,
  buildProbeUrl,
  buildProbeUrlForKind,
  compareProbe,
  decodeHostsBytes,
  defaultPrivateTokens,
  encodeHostsBytes,
  filterEmptyTokens,
  isArchiveProbeOk,
  isManagedPushSubsection,
  isProxyProbeOk,
  isProxyProbeOkWithContentType,
  maskToken,
  nextFailStreak,
  parseMirrorAggregator,
  parseSshG,
  pickAutoAdopt,
  pickBestProbe,
  PROBE_RETIRE_STREAK,
  PROXY_LIST_LIMIT,
  pushSubsectionOf,
  sanitizeToken,
  shouldRetire,
  defaultGithubAccelConfig,
  findGithubInsteadOf,
  hasSshBlock,
  removeSshBlock,
  validateProxyPrefix,
  parseGitInsteadOf,
  ARCHIVE_PROBE_BYTES,
  GITHUB_BASE,
  GIT_SSH_PUSH_SUBSECTION,
  MIRROR_AGGREGATOR_URL,
  type AccelProxy,
  type GithubAccelConfig,
  type GitPrivateToken,
  type GitPushMode,
  type InsteadOfEntry,
  type MirrorCandidate,
  type ProbeKind,
} from '../shared/github-accel-core'

export type { AccelProxy, GithubAccelConfig, GitPrivateToken }
export { maskToken }

export interface ProbeResult {
  /** 探测目标（proxy id 或前缀本身） */
  target: string
  ok: boolean
  /** 首字节延迟（ms）；失败为 null */
  latencyMs: number | null
  /** 实测下行速度（B/s）：仅 archive 探测有值 */
  speedBps?: number | null
  /** archive（整仓下载）维度是否可用；raw 探测行才有 */
  archiveOk?: boolean
  status?: number
  error?: string
}

/** 候选实测行（仅 autoTune 内部使用） */
interface DiscoveredMirror extends MirrorCandidate {
  ok: boolean
  latencyMs: number | null
  error?: string
}

/**
 * 一键自动调优的结果。总控按钮只调这一个方法，返回值用来告诉用户
 * 「这次替你做了什么」（换了哪个镜像、淘汰了几个、自动补了几个）。
 */
export interface AutoTuneResult {
  /** 实测最优的可用镜像；一个可用都没有时为 null（上层据此报错） */
  best: { id: string; name: string; prefix: string } | null
  /** 本轮实测行（形状同 probeAll，不含直连对照） */
  probed: Array<ProbeResult & { id: string; name: string }>
  /** 连续失效被淘汰的镜像：内置只停用，自建/自动采纳的移除 */
  retired: Array<{ id: string; name: string; action: 'disabled' | 'removed' }>
  /** 自动发现并加入清单的新镜像 */
  adopted: Array<{ id: string; name: string; prefix: string }>
  /** 聚合接口给出的新候选数（0 = 接口不可用或没有新候选） */
  discovered: number
}

export type SelfCheckKey = 'git' | 'proxy' | 'ssh443' | 'hosts'

export interface SelfCheckRow {
  key: SelfCheckKey
  /** skip = 该措施未开启 / 环境不具备（灰显，不算失败） */
  state: 'ok' | 'fail' | 'skip'
  message: string
  latencyMs?: number
}

export interface HealthStatus {
  state: 'unknown' | 'off' | 'ok' | 'degraded'
  /** 当前生效的加速前缀（git insteadOf 子节反推） */
  activePrefix?: string
  activeName?: string
  /** 当前前缀的 RAW 探测首字节延迟 */
  latencyMs?: number
  /** 当前镜像失效原因（仅镜像；hosts 的问题见 hosts.message） */
  error?: string
  /** 当前镜像失效时，探测到的最快备用镜像（只提示，不自动切换） */
  fallback?: { prefix: string; name: string; latencyMs: number }
  /** hosts 托管块生效时的实时有效性；未写入不返回 */
  hosts?: { ok: boolean; message: string }
  /** 探测实际经过的代理（无代理为空）：探测不经代理时，结论与 git 真实体验可能不一致 */
  proxy?: string
  checkedAt: number
}

export interface GitInsteadOfStatus {
  /** git 可执行文件是否可用 */
  available: boolean
  /** 当前全部 url.*.insteadof 条目 */
  entries: InsteadOfEntry[]
  /** 是否存在对 github.com 生效的改写 */
  active: boolean
  /** 生效条目的子节（= 加速基地址，可反推前缀） */
  activeSubsection?: string
  /** 是否由本应用写入（子节以某个已知镜像前缀开头） */
  managed: boolean
  /** 接管前备份（关闭时还原） */
  backup: string | null
  /** 当前 push 目的地规则：none = 无、self = 本应用直连、ssh = 本应用改写 SSH、user = 用户自建 */
  pushRewrite: 'none' | GitPushMode | 'user'
  /** 当前 push 规则子节（仅 user 有值，便于 UI 说明“不接管”） */
  pushSubsection?: string
  message?: string
}

export interface Ssh443Status {
  applied: boolean
  path: string
  exists: boolean
  message?: string
}

const GIT_TIMEOUT_MS = 8000
const PROBE_TIMEOUT_MS = 6000
const SELF_CHECK_GIT_TIMEOUT_MS = 15000
const SSH_G_TIMEOUT_MS = 8000

/** 巡检节奏：启动 15s 首检（避开启动高峰），之后 30 分钟一次；配置变更后 2s 复检 */
const HEALTH_FIRST_DELAY_MS = 15_000
const HEALTH_INTERVAL_MS = 30 * 60_000
const HEALTH_RECHECK_DELAY_MS = 2_000

function getStorePath(): string {
  return join(app.getPath('userData'), 'github-accel.json')
}

function getSshConfigPath(): string {
  return join(homedir(), '.ssh', 'config')
}

function execFileAsync(
  exe: string,
  args: string[],
  opts: { timeoutMs?: number; env?: NodeJS.ProcessEnv } = {},
): Promise<{ code: number; stdout: string; stderr: string; timedOut?: boolean }> {
  return new Promise((resolvePromise) => {
    execFile(
      exe,
      args,
      {
        timeout: opts.timeoutMs ?? GIT_TIMEOUT_MS,
        windowsHide: true,
        env: opts.env ? { ...process.env, ...opts.env } : process.env,
      },
      (err, stdout, stderr) => {
        if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
          resolvePromise({ code: -1, stdout: '', stderr: 'git-not-found' })
          return
        }
        // execFile 的 err.code 对非零退出是数字退出码；超时被杀时 code 为 null
        const code = err ? (typeof (err as any).code === 'number' ? (err as any).code : 1) : 0
        resolvePromise({ code, stdout: String(stdout ?? ''), stderr: String(stderr ?? ''), timedOut: !!(err as any)?.killed })
      },
    )
  })
}

/**
 * 执行 git 子命令。
 *
 * 直接 execFile('git') 在「从资源管理器启动」的 Electron 进程里经常 ENOENT：
 * Git for Windows 安装器写的是用户级 PATH，explorer.exe 的进程环境不会刷新，
 * 界面就把「找不到 git」报成「未安装 git」。先按候选路径解析一次（成功即缓存）。
 */
async function execGit(
  args: string[],
  opts: { timeoutMs?: number; env?: NodeJS.ProcessEnv } = {},
): Promise<{ code: number; stdout: string; stderr: string; timedOut?: boolean }> {
  const exe = await resolveGitExecutable()
  if (!exe) return { code: -1, stdout: '', stderr: 'git-not-found' }
  return execFileAsync(exe, args, opts)
}

/** 运行 ssh -G，仅取 stdout/stderr；未安装时 code=-1（与 execGit 同口径） */
function execSsh(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolvePromise) => {
    execFile('ssh', args, { timeout: SSH_G_TIMEOUT_MS, windowsHide: true }, (err, stdout, stderr) => {
      if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
        resolvePromise({ code: -1, stdout: '', stderr: 'ssh-not-found' })
        return
      }
      const code = err ? (typeof (err as any).code === 'number' ? (err as any).code : 1) : 0
      resolvePromise({ code, stdout: String(stdout ?? ''), stderr: String(stderr ?? '') })
    })
  })
}

// ─── 持久化 ───

async function readConfigFromDisk(): Promise<GithubAccelConfig> {
  const path = getStorePath()
  if (!existsSync(path)) return defaultGithubAccelConfig()
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // 同 tunnel-store 口径：读失败向上抛，防止后续写以出厂配置覆写用户配置
    throw new Error(`读取 GitHub 加速配置失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object' || !Array.isArray(obj.proxies)) {
      await quarantineCorruptFile(path, raw)
      return defaultGithubAccelConfig()
    }
    return normalizeConfig(obj)
  } catch (e) {
    if (e instanceof SyntaxError) {
      console.warn('[GithubAccel] github-accel.json 损坏（JSON 解析失败），已隔离原文件')
      await quarantineCorruptFile(path, raw)
      return defaultGithubAccelConfig()
    }
    throw e
  }
}

/**
 * 与出厂配置合并：内置条目不落盘也始终存在（用户只能停用不能删）；
 * 用户自增条目按 id 保留；落盘里的内置条目只取 enabled 状态（改名/换址由出厂值兜底）。
 */
function normalizeConfig(obj: any): GithubAccelConfig {
  const defaults = defaultGithubAccelConfig()
  const savedProxies: any[] = Array.isArray(obj.proxies) ? obj.proxies : []

  const proxies: AccelProxy[] = defaults.proxies.map(d => {
    const s = savedProxies.find(x => x && x.id === d.id)
    return { ...d, enabled: s ? !!s.enabled : d.enabled }
  })
  for (const s of savedProxies) {
    if (!s || typeof s !== 'object' || typeof s.id !== 'string' || s.builtin) continue
    if (typeof s.prefix !== 'string' || validateProxyPrefix(s.prefix)) continue
    if (proxies.some(p => p.id === s.id)) continue
    proxies.push({ id: s.id, name: String(s.name || s.prefix), prefix: s.prefix, builtin: false, enabled: !!s.enabled })
  }

  const gitBackup = obj.gitBackup && typeof obj.gitBackup === 'object' && typeof obj.gitBackup.prefix === 'string'
    ? {
        prefix: obj.gitBackup.prefix,
        previousValue: typeof obj.gitBackup.previousValue === 'string' ? obj.gitBackup.previousValue : null,
        // 旧版本只存了布尔（且仅 direct 模式）；读盘时翻译成三态，保住「关闭时能撤销」
        pushRewrite: (obj.gitBackup.pushRewrite === 'ssh' || obj.gitBackup.pushRewrite === 'direct'
          ? obj.gitBackup.pushRewrite
          : obj.gitBackup.pushInsteadOfAdded === true ? 'direct' : 'none') as 'none' | GitPushMode,
      }
    : null

  // 令牌随用户配置保留（整条按 id 保留），出厂模板只在用户从未配置过时注入——
  // 这样「出厂模板带空 token」不会覆盖用户已填的真实值。
  const existingTokens: any[] = Array.isArray(obj.privateTokens) ? obj.privateTokens : []
  let tokens: GitPrivateToken[] = existingTokens
    .filter(t => t && typeof t === 'object' && typeof t.id === 'string')
    .map(t => sanitizeToken(t, t.id).token)
    .filter((t): t is GitPrivateToken => !!t)
  if (tokens.length === 0) {
    tokens = defaultPrivateTokens()
  } else {
    // 把缺失的出厂模板补上（enabled=off 语义用 token 为空表示未启用），不覆盖已有。
    const have = new Set(tokens.map(t => t.id))
    for (const tpl of defaultPrivateTokens()) {
      if (!have.has(tpl.id)) tokens.push(tpl)
    }
  }

  return {
    proxies,
    gitBackup,
    lastProxyPrefix: typeof obj.lastProxyPrefix === 'string' ? obj.lastProxyPrefix : undefined,
    pushMode: obj.pushMode === 'ssh' ? 'ssh' : 'direct',
    hostsAutoRefresh: obj.hostsAutoRefresh === true,
    probeStats: sanitizeProbeStats(obj.probeStats),
    privateTokens: tokens,
  }
}

/**
 * 连续失败计数读盘清洗：只留合法的正数（封顶到淘汰阈值）。
 * 不保留就会每次读盘都清零，阈值永远凑不满 —— 自动淘汰会静默失效。
 */
function sanitizeProbeStats(raw: any): Record<string, number> {
  const out: Record<string, number> = {}
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  for (const [id, v] of Object.entries(raw)) {
    if (!id || typeof v !== 'number' || !Number.isFinite(v) || v <= 0) continue
    out[id] = Math.min(Math.floor(v), PROBE_RETIRE_STREAK)
  }
  return out
}

async function quarantineCorruptFile(path: string, raw: string): Promise<void> {
  try {
    const bak = `${path}.corrupt-${Date.now()}.bak`
    try {
      await rename(path, bak)
    } catch {
      await writeFile(bak, raw, 'utf-8')
    }
    console.warn(`[GithubAccel] 损坏文件已备份到: ${bak}`)
  } catch (e) {
    console.warn('[GithubAccel] 备份损坏文件失败:', e)
  }
}

async function writeConfigToDisk(config: GithubAccelConfig): Promise<void> {
  const path = getStorePath()
  await mkdir(dirname(path), { recursive: true })
  const tmpPath = path + '.tmp'
  const data = JSON.stringify(config, null, 2)
  await writeFile(tmpPath, data, 'utf-8')
  // Windows rename 到已存在目标可能 EPERM/EBUSY：重试一次后回退直写（与 tunnel-store 同款）
  try {
    await rename(tmpPath, path)
  } catch (err: any) {
    if (err && (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY')) {
      await new Promise(r => setTimeout(r, 200))
      try {
        await rename(tmpPath, path)
        return
      } catch {
        await writeFile(path, data, 'utf-8')
        try { await unlink(tmpPath) } catch { /* ignore */ }
      }
    } else {
      throw err
    }
  }
}

// ─── 测速 ───

/** 镜像聚合接口的超时与体积上限（第三方接口，不能信它返回多大） */
const AGGREGATOR_TIMEOUT_MS = 8000
/** 一键启动路径的聚合接口超时：每次启动都会拉一次候选池，接口被墙时不能跟着干等 8s */
const STARTUP_AGGREGATOR_TIMEOUT_MS = 4000
const AGGREGATOR_MAX_BYTES = 512 * 1024

interface ProbeOptions {
  kind: ProbeKind
  /** 还能跟几次重定向（ghproxy 类偶尔 302 到 CDN） */
  redirectsLeft?: number
  /** >0 时按字节数收满即断，用于量带宽（archive 维度） */
  maxBytes?: number
  /** 走代理隧道（探测链路与真实 git 一致） */
  agent?: Agent
}

/**
 * 单次 HTTPS 探测：计首字节时间；需要带宽时读满 maxBytes 后主动断开。
 * 判据按 kind 分流：raw/archive 要求 2xx 且非 HTML（死而不僵的镜像对任意 URL
 * 都回 200 广告页）；web 只验站点可达（根地址对爬虫常回 403）。
 */
function probeOnce(url: string, opts: ProbeOptions): Promise<ProbeResult> {
  const { kind, redirectsLeft = 1, maxBytes = 0, agent } = opts
  return new Promise((resolvePromise) => {
    const started = Date.now()
    let settled = false
    let firstByteAt = 0
    let received = 0
    const done = (r: ProbeResult) => {
      if (settled) return
      settled = true
      resolvePromise(r)
    }
    // 带宽只算「首字节之后」的窗口，否则 TLS 建连时间会被摊进速率里
    const speedOf = (): number | null => {
      if (!maxBytes || !firstByteAt) return null
      const elapsed = Date.now() - firstByteAt
      return elapsed > 0 ? Math.round((received * 1000) / elapsed) : null
    }
    let req: ClientRequest
    try {
      req = https.get(url, {
        timeout: PROBE_TIMEOUT_MS,
        headers: { 'User-Agent': 'cKunAgentSSH-ghaccel-probe' },
        ...(agent ? { agent } : {}),
      }, (res) => {
        const status = res.statusCode ?? 0
        // 重定向跟一次（ghproxy 类偶尔 302 到 CDN）
        if (status >= 300 && status < 400 && res.headers.location && redirectsLeft > 0) {
          res.resume()
          // 同 gh-download：Location 畸形时 new URL 抛出会吞掉 done()，该次探测
          // 永不 settle，probeAll 整体挂住，镜像排序界面停在转圈
          let next: string
          try {
            next = new URL(res.headers.location, url).toString()
          } catch (e: any) {
            return done({ target: url, ok: false, latencyMs: null, error: e?.message ?? '重定向地址非法' })
          }
          probeOnce(next, { kind, redirectsLeft: redirectsLeft - 1, maxBytes, agent })
            .then(r => done({ ...r, target: url }))
          return
        }
        const contentType = res.headers['content-type']
        const ok = kind === 'archive'
          ? isArchiveProbeOk(status, contentType)
          : isProxyProbeOkWithContentType(status, contentType)
        // 2xx 但响应体是 HTML 的镜像按失效处理（详见核心层判据），错误归到镜像头上
        const htmlError = isProxyProbeOk(status) && !ok
          ? '返回 HTML 页面而非文件内容（镜像可能已失效或被劫持）'
          : undefined
        const settle = (latencyMs: number | null) => done({
          target: url,
          ok,
          latencyMs,
          status,
          error: htmlError,
          ...(maxBytes ? { speedBps: speedOf() } : {}),
        })

        if (maxBytes > 0) {
          // 收满即断：量的是带宽，不是把整包拖下来
          res.on('data', (chunk: Buffer) => {
            if (!firstByteAt) firstByteAt = Date.now()
            received += chunk.length
            if (received >= maxBytes) {
              settle(firstByteAt - started)
              res.destroy()
            }
          })
          res.on('end', () => settle(firstByteAt ? firstByteAt - started : null))
          res.on('error', () => {
            if (firstByteAt) settle(firstByteAt - started)
            else done({ target: url, ok: false, latencyMs: null, error: '读取中断' })
          })
          return
        }

        res.once('data', () => {
          res.resume()
          settle(Date.now() - started)
        })
        res.once('end', () => settle(Date.now() - started))
        res.once('error', (e) => done({ target: url, ok: false, latencyMs: null, error: e.message }))
      })
    } catch (e: unknown) {
      done({ target: url, ok: false, latencyMs: null, error: e instanceof Error ? e.message : String(e) })
      return
    }
    req.once('timeout', () => {
      req.destroy()
      done({ target: url, ok: false, latencyMs: null, error: `超时（>${PROBE_TIMEOUT_MS}ms）` })
    })
    req.once('error', (e: Error) => done({ target: url, ok: false, latencyMs: null, error: e.message }))
  })
}

/** HTTPS 抓文本（镜像聚合接口）。非 2xx / 超时 / 超体积一律 reject。 */
function fetchText(url: string, opts: { agent?: Agent; timeoutMs?: number } = {}): Promise<string> {
  const timeout = opts.timeoutMs ?? AGGREGATOR_TIMEOUT_MS
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false
    const fail = (e: unknown) => {
      if (settled) return
      settled = true
      rejectPromise(e)
    }
    const req = https.get(url, {
      timeout,
      headers: { 'User-Agent': 'cKunAgentSSH-ghaccel-probe', Accept: 'application/json' },
      ...(opts.agent ? { agent: opts.agent } : {}),
    }, (res) => {
      const status = res.statusCode ?? 0
      if (status < 200 || status >= 300) {
        res.resume()
        return fail(new Error(`HTTP ${status}`))
      }
      const chunks: Buffer[] = []
      let size = 0
      res.on('data', (c: Buffer) => {
        size += c.length
        if (size > AGGREGATOR_MAX_BYTES) {
          req.destroy()
          fail(new Error('响应体过大'))
          return
        }
        chunks.push(c)
      })
      res.on('end', () => {
        if (settled) return
        settled = true
        resolvePromise(Buffer.concat(chunks).toString('utf-8'))
      })
      res.on('error', fail)
    })
    req.once('timeout', () => { req.destroy(); fail(new Error(`超时（>${timeout}ms）`)) })
    req.once('error', fail)
  })
}

// ─── 服务 ───

export class GithubAccelService {
  /** 读-改-写串行化（同 tunnel-store：防并发写互相覆写丢更新） */
  private writeQueue: Promise<unknown> = Promise.resolve()

  /** hosts 服务（自检/巡检读取托管块与其条目；不参与写入） */
  constructor(private readonly hosts?: Pick<GhHostsService, 'status' | 'autoRefresh'>) {}

  // ─── 巡检内存态（不持久化：重启后由首检恢复） ───
  private lastHealth: HealthStatus | null = null
  private healthInFlight: Promise<HealthStatus> | null = null
  private healthSender: ((status: HealthStatus) => void) | null = null
  private healthTimer: NodeJS.Timeout | null = null
  private healthFirstTimer: NodeJS.Timeout | null = null
  private healthRecheckTimer: NodeJS.Timeout | null = null

  /** 启动后台巡检：15s 后首检，之后每 30 分钟。重复调用只更新推送通道。 */
  startHealthWatch(send: (status: HealthStatus) => void): void {
    this.healthSender = send
    if (this.healthTimer) return
    this.healthFirstTimer = setTimeout(() => {
      this.healthFirstTimer = null
      void this.refreshHealth()
    }, HEALTH_FIRST_DELAY_MS)
    this.healthFirstTimer.unref?.()
    this.healthTimer = setInterval(() => { void this.refreshHealth() }, HEALTH_INTERVAL_MS)
    this.healthTimer.unref?.()
  }

  stopHealthWatch(): void {
    if (this.healthFirstTimer) clearTimeout(this.healthFirstTimer)
    if (this.healthTimer) clearInterval(this.healthTimer)
    if (this.healthRecheckTimer) clearTimeout(this.healthRecheckTimer)
    this.healthFirstTimer = null
    this.healthTimer = null
    this.healthRecheckTimer = null
  }

  /** 配置变动后的去抖复检：同一时刻只保留最后一次安排 */
  refreshHealthSoon(): void {
    if (!this.healthTimer) return
    if (this.healthRecheckTimer) clearTimeout(this.healthRecheckTimer)
    this.healthRecheckTimer = setTimeout(() => {
      this.healthRecheckTimer = null
      void this.refreshHealth()
    }, HEALTH_RECHECK_DELAY_MS)
    this.healthRecheckTimer.unref?.()
  }

  /** 最近一次巡检结果；从未巡检过返回 unknown */
  async healthStatus(): Promise<HealthStatus> {
    return this.lastHealth ?? { state: 'unknown', checkedAt: 0 }
  }

  /** 立即跑一轮巡检（并发调用共享同一轮），结果写内存并推送 */
  async refreshHealth(): Promise<HealthStatus> {
    if (this.healthInFlight) return this.healthInFlight
    this.healthInFlight = this.computeHealth().then((status) => {
      this.lastHealth = status
      this.healthSender?.(status)
      return status
    }).finally(() => {
      this.healthInFlight = null
    })
    return this.healthInFlight
  }

  private async computeHealth(): Promise<HealthStatus> {
    // 没有 git 就不谈 git 加速；hosts 未写入也不在巡检范围内
    const git = await this.gitStatus()
    const config = await this.getConfig()
    let hostsStatus = this.hosts ? await this.hosts.status().catch(() => null) : null
    const hostsApplied = !!hostsStatus?.applied
    const gitActive = git.available && git.active
    const proxyInfo = await this.currentProxy()

    if (!gitActive && !hostsApplied) {
      return { state: 'off', checkedAt: Date.now() }
    }

    let degraded = false
    let error: string | undefined
    let activePrefix: string | undefined
    let activeName: string | undefined
    let latencyMs: number | undefined
    let fallback: HealthStatus['fallback']

    if (gitActive && git.activeSubsection) {
      const active = this.resolveActiveProxy(git.activeSubsection, config)
      activePrefix = active.prefix
      activeName = active.name
      const probe = await this.probePrefix(active.prefix)
      if (probe.ok && probe.latencyMs !== null) latencyMs = probe.latencyMs
      if (!probe.ok) {
        degraded = true
        error = probe.error ?? `HTTP ${probe.status ?? '无响应'}`
        // 失效才探测其余镜像：取最快可用者作“切换建议”，绝不自动改配置
        const others = config.proxies.filter(p => p.enabled && p.prefix !== active.prefix)
        if (others.length) {
          const results = await this.probe(others.map(p => ({ id: p.id, url: buildProbeUrl(p.prefix), kind: 'raw' as const })))
          const best = results
            .filter(r => r.ok && typeof r.latencyMs === 'number')
            .sort((a, b) => (a.latencyMs ?? 0) - (b.latencyMs ?? 0))[0]
          const bestProxy = best ? others.find(p => p.id === best.id) : undefined
          if (best && bestProxy) {
            fallback = { prefix: bestProxy.prefix, name: bestProxy.name, latencyMs: best.latencyMs ?? 0 }
          }
        }
      }
    }

    let hosts: HealthStatus['hosts']
    if (hostsApplied && hostsStatus) {
      let check = await this.checkHostsEntries(hostsStatus.entries)
      // hosts 失效（解析未命中/不可达）或快照过期时自动重选（默认关闭）：
      // 服务内部只在「不用提权就能写」时动手，需要 UAC 的一律只亮红灯，
      // 绝不在后台弹窗
      if ((!check.ok || hostsStatus.stale) && config.hostsAutoRefresh && this.hosts?.autoRefresh) {
        const refreshed = await this.hosts.autoRefresh().catch(() => null)
        if (refreshed?.changed) {
          hostsStatus = await this.hosts.status().catch(() => hostsStatus)
          if (hostsStatus?.applied) check = await this.checkHostsEntries(hostsStatus.entries)
        }
      }
      hosts = { ok: check.ok, message: check.message }
      // hosts 失败只置 degraded、不写 error —— error 专指镜像失效（UI 依据它区分两种红灯）
      if (!check.ok) degraded = true
    }

    const proxyText = proxyInfo
      ? proxyInfo.spec.protocol === 'http'
        ? maskProxy(proxyInfo.spec)
        : `${maskProxy(proxyInfo.spec)}（SOCKS，探测未走代理）`
      : undefined

    return {
      state: degraded ? 'degraded' : 'ok',
      activePrefix,
      activeName,
      latencyMs,
      error,
      fallback,
      hosts,
      proxy: proxyText,
      checkedAt: Date.now(),
    }
  }

  /** 生效子节（`<prefix>https://github.com/`）反推前缀与显示名 */
  private resolveActiveProxy(subsection: string, config: GithubAccelConfig): { prefix: string; name: string } {
    const hit = config.proxies.find(p => `${p.prefix}${GITHUB_BASE}` === subsection)
    if (hit) return { prefix: hit.prefix, name: hit.name }
    const prefix = subsection.endsWith(GITHUB_BASE) ? subsection.slice(0, -GITHUB_BASE.length) : subsection
    return { prefix, name: prefix }
  }

  /**
   * hosts 托管块实时有效性：最多取 3 个域名（github.com 优先），系统解析必须命中
   * 托管块里的 IP 且该 IP 的 443 可达（至少一条）。
   */
  private async checkHostsEntries(entries: { domain: string; ip: string }[]): Promise<{ ok: boolean; message: string }> {
    const ordered = entries.filter(e => e.domain === 'github.com')
      .concat(entries.filter(e => e.domain !== 'github.com'))
      .slice(0, 3)
    if (!ordered.length) return { ok: false, message: '托管块内没有可用条目' }

    let firstFailure = ''
    for (const entry of ordered) {
      let addresses: string[]
      try {
        addresses = (await lookup(entry.domain, { all: true })).map(a => a.address)
      } catch (e) {
        firstFailure ||= `${entry.domain} 系统解析失败`
        continue
      }
      if (!addresses.includes(entry.ip)) {
        firstFailure ||= `系统把 ${entry.domain} 解析为 ${addresses[0] ?? '无结果'}（托管块为 ${entry.ip}）`
        continue
      }
      const ms = await tcpLatency(entry.ip, 443)
      if (ms === null) {
        firstFailure ||= `${entry.domain} 解析命中但 ${entry.ip} 的 443 不可达`
        continue
      }
      return { ok: true, message: `${entry.domain} → ${entry.ip}（${ms}ms）` }
    }
    return {
      ok: false,
      message: `${firstFailure}，可能被 DNS 缓存或其他软件覆盖`,
    }
  }

  /**
   * 端到端自检：只读，不写配置、不弹 UAC。
   * 措施未开启或环境不具备的项返回 skip（UI 灰显，不算失败）。
   */
  async selfCheck(): Promise<SelfCheckRow[]> {
    const [git, ssh, hostsStatus] = await Promise.all([
      this.gitStatus(),
      this.ssh443Status(),
      this.hosts ? this.hosts.status().catch(() => null) : Promise.resolve(null),
    ])
    const config = await this.getConfig()
    const rows: SelfCheckRow[] = []

    // git：ls-remote 真实走一遍 insteadOf 改写后的地址（15s 超时，禁止凭据提示挂住）
    if (!git.available) {
      rows.push({ key: 'git', state: 'skip', message: git.message || '未检测到 git' })
    } else if (!git.active) {
      rows.push({ key: 'git', state: 'skip', message: 'git 加速未生效' })
    } else {
      const started = Date.now()
      const r = await execGit(
        ['ls-remote', `${GITHUB_BASE}git/git`, 'HEAD'],
        { timeoutMs: SELF_CHECK_GIT_TIMEOUT_MS, env: { GIT_TERMINAL_PROMPT: '0' } },
      )
      const latencyMs = Date.now() - started
      if (r.code === 0 && r.stdout.trim()) {
        const name = git.activeSubsection ? this.resolveActiveProxy(git.activeSubsection, config).name : '当前镜像'
        rows.push({ key: 'git', state: 'ok', message: `经 ${name} 读取成功`, latencyMs })
      } else {
        const firstLine = r.timedOut
          ? `超时（>${SELF_CHECK_GIT_TIMEOUT_MS / 1000}s 无响应）`
          : ((r.stderr.trim() || r.stdout.trim()).split(/\r?\n/)[0] || `退出码 ${r.code}`)
        rows.push({ key: 'git', state: 'fail', message: `git 读取失败：${firstLine}`, latencyMs })
      }
    }

    // proxy：与巡检同一判据（2xx）再探一次原始前缀，用于把“git 失败”归因到镜像
    if (git.available && git.active && git.activeSubsection) {
      const active = this.resolveActiveProxy(git.activeSubsection, config)
      const probe = await this.probePrefix(active.prefix)
      if (probe.ok) {
        rows.push({ key: 'proxy', state: 'ok', message: `镜像 ${active.name} 返回 ${probe.status}`, latencyMs: probe.latencyMs ?? undefined })
      } else {
        rows.push({
          key: 'proxy',
          state: 'fail',
          message: probe.error ? `镜像 ${active.name} 不可用：${probe.error}` : `镜像 ${active.name} 返回 HTTP ${probe.status}`,
        })
      }
    } else {
      rows.push({ key: 'proxy', state: 'skip', message: 'git 加速未生效' })
    }

    // ssh443：托管块生效才验；Windows 无 OpenSSH 时 skip 而不是 fail
    if (!ssh.applied) {
      rows.push({ key: 'ssh443', state: 'skip', message: 'SSH 443 未开启' })
    } else {
      const r = await execSsh(['-G', 'github.com'])
      if (r.code === -1) {
        rows.push({ key: 'ssh443', state: 'skip', message: '未检测到 OpenSSH 客户端（ssh 命令不可用）' })
      } else if (r.code !== 0) {
        rows.push({ key: 'ssh443', state: 'fail', message: `ssh -G 失败：${(r.stderr.trim().split(/\r?\n/)[0]) || `退出码 ${r.code}`}` })
      } else {
        const parsed = parseSshG(r.stdout)
        if (parsed.hostname !== 'ssh.github.com' || parsed.port !== 443) {
          rows.push({
            key: 'ssh443',
            state: 'fail',
            message: `github.com 当前解析为 ${parsed.hostname ?? '?'}:${parsed.port ?? '?'}，未命中 443 托管块`,
          })
        } else {
          const ms = await tcpLatency('ssh.github.com', 443)
          if (ms === null) {
            rows.push({ key: 'ssh443', state: 'fail', message: 'ssh.github.com:443 不可达' })
          } else {
            rows.push({ key: 'ssh443', state: 'ok', message: 'github.com → ssh.github.com:443', latencyMs: ms })
          }
        }
      }
    }

    // hosts：托管块生效才验系统解析是否真的用上了优选 IP
    if (!hostsStatus?.applied) {
      rows.push({ key: 'hosts', state: 'skip', message: 'hosts 加速未开启' })
    } else {
      const check = await this.checkHostsEntries(hostsStatus.entries)
      rows.push({ key: 'hosts', state: check.ok ? 'ok' : 'fail', message: check.message })
    }

    return rows
  }

  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(op, op)
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }

  async getConfig(): Promise<GithubAccelConfig> {
    try {
      return await readConfigFromDisk()
    } catch (e) {
      console.error('[GithubAccel] 读取配置失败，降级为出厂配置:', e)
      return defaultGithubAccelConfig()
    }
  }

  /**
   * 保存代理清单（UI 整表提交）。内置条目的 id/prefix 不可改（防 UI 篡改
   * 出厂值），只允许翻转 enabled；用户条目需通过格式校验。
   */
  async saveLists(proxies: AccelProxy[]): Promise<GithubAccelConfig> {
    return this.enqueue(async () => {
      if (!Array.isArray(proxies)) throw new Error('镜像清单格式无效')
      const defaults = defaultGithubAccelConfig()
      const nextProxies: AccelProxy[] = []
      for (const p of proxies) {
        if (!p || typeof p !== 'object' || typeof p.id !== 'string') throw new Error('镜像条目无效')
        const builtin = defaults.proxies.find(d => d.id === p.id)
        if (p.builtin || builtin) {
          if (!builtin) throw new Error(`未知的内置镜像: ${p.id}`)
          nextProxies.push({ ...builtin, enabled: !!p.enabled })
          continue
        }
        const err = validateProxyPrefix(p.prefix)
        if (err) throw new Error(`镜像「${p.name || p.prefix}」：${err}`)
        if (nextProxies.some(x => x.prefix === p.prefix)) throw new Error(`镜像前缀重复: ${p.prefix}`)
        nextProxies.push({ id: p.id, name: String(p.name || p.prefix).slice(0, 40), prefix: p.prefix.trim(), builtin: false, enabled: !!p.enabled })
      }
      const current = await readConfigFromDisk()
      const next: GithubAccelConfig = { ...current, proxies: nextProxies }
      await writeConfigToDisk(next)
      // 镜像清单可能停用了当前生效前缀或恢复了它：去抖复检一次，红灯状态要跟着变
      this.refreshHealthSoon()
      return next
    })
  }

  // ─── 出站代理（探测链路与 git 一致） ───

  /** 代理探测缓存 5 分钟：一轮测速十几个目标，不该各查一次注册表/子进程 */
  private proxyCache: { at: number; value: ProxyDetection | null } | null = null

  private async currentProxy(): Promise<ProxyDetection | null> {
    const ttl = 5 * 60_000
    if (this.proxyCache && Date.now() - this.proxyCache.at < ttl) return this.proxyCache.value
    const exe = await resolveGitExecutable()
    const value = await detectSystemProxy(exe).catch(() => null)
    this.proxyCache = { at: Date.now(), value }
    return value
  }

  /**
   * 探测用 Agent。开着代理的机器上 Node 的 https.get 不会自动走代理，直连探测的
   * 结论与实际 clone 体验是两回事；HTTP(S) 代理走 CONNECT 隧道，SOCKS 不实现隧道
   * （只上报给 UI，说明「探测未走代理」）。
   */
  private async probeAgent(): Promise<Agent | undefined> {
    const p = await this.currentProxy()
    if (!p || p.spec.protocol !== 'http') return undefined
    return tunnelAgentFor(p.spec)
  }

  /** 探一个镜像前缀的 raw 目标（巡检/自检/切换建议共用同一判据） */
  private async probePrefix(prefix: string): Promise<ProbeResult> {
    return probeOnce(buildProbeUrl(prefix), { kind: 'raw', agent: await this.probeAgent() })
  }

  /** 对传入的地址列表逐个探测（并发），UI 用结果给镜像排序/标死活 */
  async probe(targets: { id: string; url: string; kind?: ProbeKind; maxBytes?: number }[]): Promise<(ProbeResult & { id: string })[]> {
    if (!Array.isArray(targets)) return []
    const agent = await this.probeAgent()
    const jobs = targets.slice(0, 32).map(async t => {
      // 只探测 http(s)，防 file:// 等协议注入
      if (typeof t.url !== 'string' || !/^https:\/\//i.test(t.url)) {
        return { id: String(t.id), target: String(t.url ?? ''), ok: false, latencyMs: null, error: '仅支持 https 探测目标' }
      }
      const kind = t.kind ?? 'raw'
      const r = await probeOnce(t.url, {
        kind,
        maxBytes: t.maxBytes ?? (kind === 'archive' ? ARCHIVE_PROBE_BYTES : 0),
        agent,
      })
      return { ...r, id: t.id }
    })
    return Promise.all(jobs)
  }

  /**
   * 单镜像双维探测：raw 判活 + 首字节，archive 量带宽。
   * 两维后端不同（raw.githubusercontent vs codeload），raw 快不代表 clone 快。
   */
  private async probeProxyDims(id: string, prefix: string, agent?: Agent): Promise<ProbeResult & { id: string }> {
    const [raw, archive] = await Promise.all([
      probeOnce(buildProbeUrlForKind(prefix, 'raw'), { kind: 'raw', agent }),
      probeOnce(buildProbeUrlForKind(prefix, 'archive'), { kind: 'archive', maxBytes: ARCHIVE_PROBE_BYTES, agent }),
    ])
    return {
      ...raw,
      id,
      // archive 失败不推翻 raw 的「可用」判定，只是拿不到带宽
      speedBps: archive.ok ? archive.speedBps ?? null : null,
      archiveOk: archive.ok,
    }
  }

  /** 全部启用镜像的探测结果（含直连对照，代理双维） */
  async probeAll(): Promise<(ProbeResult & { id: string; name: string })[]> {
    const config = await this.getConfig()
    const enabled = config.proxies.filter(p => p.enabled)
    const agent = await this.probeAgent()

    const [direct, proxyRows] = await Promise.all([
      // 直连对照：空前缀 = 原地址，衡量「不开加速」的基线
      this.probeProxyDims('__direct__', '', agent),
      Promise.all(enabled.map(p => this.probeProxyDims(p.id, p.prefix, agent))),
    ])

    // 速度优先、延迟破平：TTFB 快但只能跑 20KB/s 的线不该被推上「最快」
    proxyRows.sort(compareProbe)
    return [
      { ...direct, name: '直连对照' },
      ...proxyRows.map(r => ({ ...r, name: enabled.find(p => p.id === r.id)?.name ?? r.id })),
    ]
  }

  /**
   * 拉聚合清单，只取候选不探测。接口不可用（被墙/超时/非 2xx）返回空数组 ——
   * 调用方（尤其一键启动）不能被第三方接口卡住。
   */
  private async fetchMirrorCandidates(opts: { agent?: Agent; timeoutMs?: number } = {}): Promise<MirrorCandidate[]> {
    try {
      const agent = opts.agent ?? (await this.probeAgent())
      const raw = await fetchText(MIRROR_AGGREGATOR_URL, { agent, timeoutMs: opts.timeoutMs })
      return parseMirrorAggregator(JSON.parse(raw))
    } catch (e) {
      console.warn('[GithubAccel] 镜像聚合接口不可用:', e instanceof Error ? e.message : e)
      return []
    }
  }

  /** 对候选逐条 raw 实测（排除清单已有的前缀），按快慢排序 */
  private async probeMirrorCandidates(
    candidates: MirrorCandidate[],
    known: Set<string>,
  ): Promise<DiscoveredMirror[]> {
    const fresh = candidates.filter(c => !known.has(c.prefix))
    if (!fresh.length) return []
    const results = await this.probe(fresh.map(c => ({
      id: c.prefix,
      url: buildProbeUrlForKind(c.prefix, 'raw'),
      kind: 'raw' as ProbeKind,
    })))
    const byId = new Map(results.map(r => [r.id, r]))
    return fresh
      .map(c => {
        const r = byId.get(c.prefix)
        return { prefix: c.prefix, name: c.name, ok: !!r?.ok, latencyMs: r?.latencyMs ?? null, error: r?.error }
      })
      .sort((a, b) => compareProbe({ ok: a.ok, latencyMs: a.latencyMs }, { ok: b.ok, latencyMs: b.latencyMs }))
  }

  /**
   * 一键自动调优（总控按钮的唯一入口）：
   * 1) 实测全部启用镜像（raw 判活 + archive 量带宽）；
   * 2) 累计连续失败次数，达阈值的淘汰（内置停用、自建移除）；
   * 3) 每次启动都刷新一次候选池，可用不足 AUTO_OK_TARGET 时实测候选并自动采纳；
   * 4) 返回实测最优项，交给上层写 git insteadOf。
   *
   * 镜像清单全自动维护：采纳只收本地实测拿到真实内容（非 HTML）的 https 前缀、
   * 一次最多两个；淘汰要求「连续」失败，单次网络抖动不会误杀。
   */
  async autoTune(): Promise<AutoTuneResult> {
    return this.enqueue(async () => {
      const config = await readConfigFromDisk()
      const agent = await this.probeAgent()
      const stats: Record<string, number> = { ...(config.probeStats ?? {}) }
      let proxies = config.proxies.slice()

      const probeList = (list: AccelProxy[]) =>
        Promise.all(list.map(p => this.probeProxyDims(p.id, p.prefix, agent)))

      let rows = await probeList(proxies.filter(p => p.enabled))
      for (const r of rows) stats[r.id] = nextFailStreak(stats[r.id], r.ok)

      const retired: AutoTuneResult['retired'] = []
      const dead = new Set(rows.filter(r => !r.ok && shouldRetire(stats[r.id])).map(r => r.id))
      if (dead.size) {
        proxies = proxies.flatMap(p => {
          if (!dead.has(p.id)) return [p]
          if (p.builtin) {
            // 内置镜像只停用：换个网络环境它可能又活了，用户也能手动改回来
            retired.push({ id: p.id, name: p.name, action: 'disabled' })
            return [{ ...p, enabled: false }]
          }
          retired.push({ id: p.id, name: p.name, action: 'removed' })
          return []
        })
        for (const id of dead) delete stats[id]
        rows = rows.filter(r => !dead.has(r.id))
        console.info(`[GithubAccel] 自动淘汰连续失效镜像: ${retired.map(r => r.name).join(', ')}`)
      }

      let discovered = 0
      const adopted: AutoTuneResult['adopted'] = []
      // 每次都刷新一次候选池：公共镜像半年内必死一批，认知不能停在安装那天。
      // 已够用时只拉清单（一个 GET，超时 4s），不为「可能更快」去实测十几个候选。
      const known = new Set(proxies.map(p => p.prefix))
      const pool = await this.fetchMirrorCandidates({ agent, timeoutMs: STARTUP_AGGREGATOR_TIMEOUT_MS })
      const fresh = pool.filter(c => !known.has(c.prefix))
      discovered = fresh.length
      const need = AUTO_OK_TARGET - rows.filter(r => r.ok).length
      if (need > 0 && fresh.length) {
        // 候选先测 raw，采纳后再补 archive 维度（raw 判据不变）
        const tested = await this.probeMirrorCandidates(fresh, known)
        const picks = pickAutoAdopt(tested, [...known], Math.min(AUTO_ADOPT_LIMIT, need, PROXY_LIST_LIMIT - proxies.length))
        const created: AccelProxy[] = picks.map(c => ({
          id: `auto-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
          name: c.name,
          prefix: c.prefix,
          builtin: false,
          enabled: true,
        }))
        if (created.length) {
          proxies = [...proxies, ...created]
          const freshRows = await probeList(created)
          for (const r of freshRows) stats[r.id] = nextFailStreak(stats[r.id], r.ok)
          rows = [...rows, ...freshRows]
          for (const p of created) adopted.push({ id: p.id, name: p.name, prefix: p.prefix })
          console.info(`[GithubAccel] 自动采纳新镜像: ${created.map(p => p.name).join(', ')}`)
        }
      }

      // 计数只保留仍在清单且有失败记录的条目，避免配置文件越攒越大
      const keptStats: Record<string, number> = {}
      for (const p of proxies) {
        const s = stats[p.id]
        if (typeof s === 'number' && s > 0) keptStats[p.id] = s
      }
      const proxiesChanged = retired.length > 0 || adopted.length > 0
      if (proxiesChanged || JSON.stringify(config.probeStats ?? {}) !== JSON.stringify(keptStats)) {
        await writeConfigToDisk({ ...config, proxies, probeStats: keptStats })
      }
      if (proxiesChanged) this.refreshHealthSoon()

      const byId = new Map(proxies.map(p => [p.id, p]))
      const enabledRows = rows.filter(r => byId.get(r.id)?.enabled)
      const top = pickBestProbe(enabledRows)
      const bestProxy = top ? byId.get(top.id) : undefined
      return {
        best: top && bestProxy ? { id: bestProxy.id, name: bestProxy.name, prefix: bestProxy.prefix } : null,
        probed: enabledRows.map(r => ({ ...r, name: byId.get(r.id)?.name ?? r.id })),
        retired,
        adopted,
        discovered,
      }
    })
  }

  /** 记住一键加速使用的镜像前缀，以及 push 策略 / hosts 自动重选开关 */
  async saveOptions(patch: { lastProxyPrefix?: string; pushMode?: GitPushMode; hostsAutoRefresh?: boolean }): Promise<GithubAccelConfig> {
    return this.enqueue(async () => {
      const current = await readConfigFromDisk()
      const next: GithubAccelConfig = {
        ...current,
        lastProxyPrefix: typeof patch.lastProxyPrefix === 'string' ? patch.lastProxyPrefix : current.lastProxyPrefix,
        pushMode: patch.pushMode === 'ssh' || patch.pushMode === 'direct' ? patch.pushMode : current.pushMode,
        hostsAutoRefresh: typeof patch.hostsAutoRefresh === 'boolean' ? patch.hostsAutoRefresh : current.hostsAutoRefresh,
      }
      await writeConfigToDisk(next)
      return next
    })
  }

  // ─── 私人令牌（PAT） ───

  /** 读全部令牌（原文；渲染层用 maskToken 脱敏展示） */
  async getTokens(): Promise<GitPrivateToken[]> {
    const config = await this.getConfig()
    return config.privateTokens ?? defaultPrivateTokens()
  }

  /**
   * 全量保存令牌清单（渲染层整表提交）。
   * - 入口校验含域名/空白字符，单条错误不会污染已落盘的数据（全部通过才写）
   * - 新增条目的 id 由前端 genId 生成；重复 id 视为同一条目更新
   * - 预设平台（gitcode/github/...）的 host 不接受篡改（要走改域名请切 custom）
   * - 返回清洗后的完整清单，store 直接用它覆盖本地缓存
   */
  async saveTokens(tokens: GitPrivateToken[]): Promise<GitPrivateToken[]> {
    if (!Array.isArray(tokens)) throw new Error('令牌清单格式无效')
    // 出厂模板 token 为空（占位）：filterEmptyTokens 过滤掉，避免「令牌不能为空」误报；
    // normalizeConfig 读盘时会按 id 重新注入缺失的出厂模板，UI 看到的空槽位不会丢。
    const { persist } = filterEmptyTokens(tokens)
    const cleaned: GitPrivateToken[] = []
    const seen = new Set<string>()
    for (const t of persist) {
      const { token, errors } = sanitizeToken(t, t.id || `tok-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)
      if (!token) throw new Error(`令牌「${t.label || t.host || '未命名'}」：${errors.join('；')}`)
      if (seen.has(token.id)) throw new Error(`令牌 id 重复：${token.id}`)
      seen.add(token.id)
      cleaned.push(token)
    }
    return this.enqueue(async () => {
      const current = await readConfigFromDisk()
      const next: GithubAccelConfig = { ...current, privateTokens: cleaned }
      await writeConfigToDisk(next)
      return cleaned
    })
  }

  // ─── git insteadOf ───

  async gitStatus(): Promise<GitInsteadOfStatus> {
    const r = await execGit(buildGitReadArgs())
    if (r.stderr === 'git-not-found') {
      return { available: false, entries: [], active: false, managed: false, backup: null, pushRewrite: 'none', message: '未检测到 git，请先安装 Git' }
    }
    // 无条目时 git 退出码为 1、输出为空 —— 正常情况
    if (r.code !== 0 && r.code !== 1) {
      return { available: true, entries: [], active: false, managed: false, backup: null, pushRewrite: 'none', message: `git config 查询失败: ${r.stderr.trim() || `退出码 ${r.code}`}` }
    }
    const entries = parseGitInsteadOf(r.stdout)
    const activeEntry = findGithubInsteadOf(entries)
    const config = await this.getConfig()
    const managed = !!activeEntry && config.proxies.some(p => activeEntry.subsection === `${p.prefix}${GITHUB_BASE}`)
    // push 规则归属：本应用只认自己写入的两个子节（direct / ssh），其余一律视为用户自建
    const pushEntries = entries.filter(e => e.push)
    const ours = pushEntries.find(e => isManagedPushSubsection(e.subsection))
    const userPush = pushEntries.find(e => !isManagedPushSubsection(e.subsection) && e.value === GITHUB_BASE)
    const pushRewrite: 'none' | GitPushMode | 'user' = ours
      ? (ours.subsection === GIT_SSH_PUSH_SUBSECTION ? 'ssh' : 'direct')
      : (userPush ? 'user' : 'none')
    return {
      available: true,
      entries,
      active: !!activeEntry,
      activeSubsection: activeEntry?.subsection,
      managed,
      backup: config.gitBackup?.previousValue ?? null,
      pushRewrite,
      pushSubsection: userPush?.subsection,
    }
  }

  /**
   * 开启 insteadOf 接管：git clone https://github.com/... 一律改写为 <prefix>https://github.com/...
   * 若用户已有其它 github.com 改写规则，先备份原值（关闭时还原；保留最早一份备份）。
   * 同时写自指 pushInsteadOf（github.com → github.com）抵消 insteadOf 对 push 的改写：
   * insteadOf 对 push 同样生效（git 实测确认），只读镜像会让 HTTPS push 必然失败。
   * 用户已有自己的 github.com pushInsteadOf（如 push 重定向到 ssh）时不叠加。
   */
  async gitEnable(prefix: string, pushMode?: GitPushMode): Promise<GitInsteadOfStatus> {
    const err = validateProxyPrefix(prefix)
    if (err) throw new Error(err)
    return this.enqueue(async () => {
      const before = await this.gitStatus()
      if (!before.available) throw new Error(before.message || 'git 不可用')
      const existing = before.activeSubsection
      const target = `${prefix}${GITHUB_BASE}`
      const config = await this.getConfig()
      const mode: GitPushMode = pushMode === 'ssh' || pushMode === 'direct'
        ? pushMode
        : (config.pushMode ?? 'direct')

      if (existing && existing !== target) {
        // 已有别的规则：备份原值（若还有未还原的旧备份，保留最早那份），再移除旧规则
        if (!config.gitBackup) config.gitBackup = { prefix, previousValue: existing }
        const unsetOld = await execGit(buildGitUnsetSubsectionArgs(existing))
        if (unsetOld.code !== 0) throw new Error(`移除旧规则失败: ${unsetOld.stderr.trim() || `退出码 ${unsetOld.code}`}`)
      } else if (!config.gitBackup) {
        // 原本没有规则（或接管凭证丢失）：备份标记 null，关闭时直接删除即可
        config.gitBackup = { prefix, previousValue: null }
      }
      config.gitBackup = { previousValue: config.gitBackup.previousValue, prefix }

      // 用户已有自己的 github.com push 规则（如 push 重定向到 ssh）就不叠加：
      // 同为 value 的双匹配胜者未定义，这时一律不接管 push
      const userPush = before.entries.some(e => e.push && !isManagedPushSubsection(e.subsection) && e.value === GITHUB_BASE)
      const rewrite: 'none' | GitPushMode = userPush ? 'none' : mode
      config.gitBackup.pushRewrite = rewrite

      // 备份先落盘再写新规则：上面已经 unset 掉用户的旧规则，若 set 抛错
      // （.gitconfig.lock 被并发 git 占住、8s 超时、home 只读）就直接退出，
      // 备份只存在于内存，用户的原规则从此无从还原（谁设置谁还原落空）。
      await writeConfigToDisk(config)

      // 先清掉两种模式里不用的那条：direct ↔ ssh 切换不残留第二条规则
      for (const sub of [GITHUB_BASE, GIT_SSH_PUSH_SUBSECTION]) {
        const owned = before.entries.some(e => e.push && e.subsection === sub && e.value === GITHUB_BASE)
        if (!owned) continue
        if (rewrite !== 'none' && sub === pushSubsectionOf(rewrite)) continue
        const unset = await execGit(buildGitPushUnsetSubsectionArgs(sub))
        // 退出码 5 = key 不存在（已被外部移除），视为成功
        if (unset.code !== 0 && unset.code !== 5) {
          throw new Error(`移除旧的 push 规则失败: ${unset.stderr.trim() || `退出码 ${unset.code}`}`)
        }
      }
      if (rewrite !== 'none') {
        const setPush = await execGit(rewrite === 'ssh' ? buildGitPushSshArgs() : buildGitPushKeepArgs())
        if (setPush.code !== 0) throw new Error(`写入 push 规则失败: ${setPush.stderr.trim() || `退出码 ${setPush.code}`}`)
      }
      const set = await execGit(buildGitSetArgs(prefix))
      if (set.code !== 0) throw new Error(`写入 git 配置失败: ${set.stderr.trim() || `退出码 ${set.code}`}`)
      this.refreshHealthSoon()
      return this.gitStatus()
    })
  }

  /**
   * 关闭接管：删除本应用写入的规则，并按备份还原用户原有规则（谁设置谁还原）。
   * 当前生效规则不是本应用写入、且没有接管凭证时拒绝操作（不动用户自己的配置）。
   */
  async gitDisable(): Promise<GitInsteadOfStatus> {
    return this.enqueue(async () => {
      const before = await this.gitStatus()
      if (!before.available) throw new Error(before.message || 'git 不可用')
      const config = await this.getConfig()

      if (before.active && !before.managed && !config.gitBackup) {
        throw new Error('当前改写规则不是本应用写入的，为避免误删未做改动；如需移除请手动执行 git config --global --unset')
      }

      if (before.activeSubsection) {
        // 按完整子节卸载（subsection 是什么就卸什么，与前缀推导无关）
        const unset = await execGit(buildGitUnsetSubsectionArgs(before.activeSubsection))
        // 退出码 5 = key 不存在（已被外部移除），视为成功
        if (unset.code !== 0 && unset.code !== 5) {
          throw new Error(`移除改写规则失败: ${unset.stderr.trim() || `退出码 ${unset.code}`}`)
        }
      }
      if (config.gitBackup?.previousValue) {
        const restore = await execGit(buildGitRestoreArgs(config.gitBackup.previousValue))
        if (restore.code !== 0) throw new Error(`还原原有规则失败: ${restore.stderr.trim() || `退出码 ${restore.code}`}`)
      }
      // 移除本应用写入的 push 规则（两种模式都清；用户自建规则一条不碰）
      const pushSubs = new Set<string>()
      if (before.pushRewrite === 'direct') pushSubs.add(GITHUB_BASE)
      if (before.pushRewrite === 'ssh') pushSubs.add(GIT_SSH_PUSH_SUBSECTION)
      const recorded = config.gitBackup?.pushRewrite
      if (recorded === 'direct') pushSubs.add(GITHUB_BASE)
      if (recorded === 'ssh') pushSubs.add(GIT_SSH_PUSH_SUBSECTION)
      for (const sub of pushSubs) {
        const unsetPush = await execGit(buildGitPushUnsetSubsectionArgs(sub))
        // 规则已不存在时退出码 5，视为成功
        if (unsetPush.code !== 0 && unsetPush.code !== 5) {
          throw new Error(`移除 push 规则失败: ${unsetPush.stderr.trim() || `退出码 ${unsetPush.code}`}`)
        }
      }
      config.gitBackup = null
      await writeConfigToDisk(config)
      this.refreshHealthSoon()
      return this.gitStatus()
    })
  }

  // ─── ~/.ssh/config 443 托管块 ───
  //
  // 读写按字节（latin1）往返，与 hosts 文件同一套方案：Windows 用户的 ssh config
  // 常有 GBK/ANSI 编码的注释（记事本产物），按 UTF-8 解码会把高位字节变成 U+FFFD
  // 再原样写回，块外的用户配置被永久改写。托管块本身是纯 ASCII，latin1 往返
  // 对它无感，hasSshBlock / applySshBlock / removeSshBlock 的判据不受影响。

  async ssh443Status(): Promise<Ssh443Status> {
    const path = getSshConfigPath()
    if (!existsSync(path)) return { applied: false, path, exists: false }
    try {
      const content = decodeHostsBytes(await readFile(path))
      return { applied: hasSshBlock(content), path, exists: true }
    } catch (e) {
      return { applied: false, path, exists: true, message: e instanceof Error ? e.message : String(e) }
    }
  }

  async ssh443Apply(): Promise<Ssh443Status> {
    // 与 git insteadOf 共用写队列：apply/remove/enable/disable 并发时读-改-写不互相覆写
    return this.enqueue(async () => {
      const path = getSshConfigPath()
      let existing = ''
      if (existsSync(path)) {
        existing = decodeHostsBytes(await readFile(path))
      }
      // 首次写入前备份 ~/.ssh/config（与 hosts/git 同款"改前留底"语义；托管块可精确移除，备份仅防意外）
      if (existing && !hasSshBlock(existing)) {
        try {
          const dir = join(app.getPath('userData'), 'backups')
          await mkdir(dir, { recursive: true })
          await copyFile(path, join(dir, `ssh-config-backup-${Date.now()}`))
        } catch (e) {
          console.warn('[GithubAccel] ~/.ssh/config 备份失败（继续写入）:', e)
        }
      }
      const next = applySshBlock(existing)
      await mkdir(dirname(path), { recursive: true })
      await writeFile(path, encodeHostsBytes(next))
      return { applied: true, path, exists: true }
    })
  }

  async ssh443Remove(): Promise<Ssh443Status> {
    return this.enqueue(async () => {
      const path = getSshConfigPath()
      if (!existsSync(path)) return { applied: false, path, exists: false }
      const existing = decodeHostsBytes(await readFile(path))
      const next = removeSshBlock(existing)
      if (next !== existing) await writeFile(path, encodeHostsBytes(next))
      return { applied: false, path, exists: true }
    })
  }
}
