/**
 * GitHub 加速 —— hosts 优选 IP（M3 增强模式）
 *
 * 流程：双源候选（本地 DoH ∪ 社区维护的 GitHub520 列表）→ 候选 IP 并发 TCP connect:443
 * 筛可达到 → 对最快的几个做 TLS+SNI 证书校验（避免把域名钉到一张不属于它的证书上）
 * → 渲染 hosts 托管块（BEGIN/END 标记圈定，纯逻辑在 shared 核心）写入系统 hosts
 * → ipconfig /flushdns（Windows 有 DNS 缓存，不清理会“写了没用”）。
 *
 * 风险（UI 已标注）：① 写 hosts 需要管理员权限（EACCES 时给中文提示）；
 * ② 部分杀软对改 hosts 敏感；③ GitHub IP 段被重点干扰时优选可能全灭。
 * 写入前自动备份原 hosts 到 userData，移除托管块即还原。
 */
import { app } from 'electron'
import { join } from 'path'
import { readFile, writeFile, copyFile, mkdir, unlink } from 'fs/promises'
import { existsSync } from 'fs'
import { execFile } from 'child_process'
import https from 'https'
import { tcpLatency } from './utils/tcp-latency'
import { tlsProbe } from './utils/tls-latency'
import { resolvePowerShell } from './utils/powershell-path'
import {
  applyHostsBlock,
  COMMUNITY_HOSTS_SOURCE,
  decodeHostsBytes,
  encodeHostsBytes,
  githubHostsDomains,
  HOSTS_BLOCK_BROKEN_MESSAGE,
  hostsBlockState,
  hostsBlockAgeDays,
  hostsBlockGeneratedAt,
  isHostsBlockStale,
  isValidIPv4,
  mergeHostCandidates,
  parseCommunityHosts,
  removeHostsBlock,
  HOSTS_BLOCK_BEGIN,
  HOSTS_BLOCK_END,
} from '../shared/github-accel-core'

export interface HostsPickRow {
  domain: string
  /** 选中的最优 IP；解析失败为 null */
  ip: string | null
  /** TLS 握手延迟（证书校验通过才有值）；未测出为 null */
  latencyMs: number | null
  /** 候选总数（双源合并后） */
  candidates: number
  /** 选中结果是否通过了 TLS+SNI 证书校验 */
  verified: boolean
  error?: string
}

export interface HostsStatus {
  applied: boolean
  path: string
  writable: boolean
  /** 托管块内容（便于 UI 展示当前生效的优选结果） */
  entries: { domain: string; ip: string }[]
  /** 块的生成时间原文（ISO 串）；null = 块里没有可比对的 `# generated:` 行 */
  generatedAt: string | null
  /**
   * 块写入至今的天数；null = 块里没有可比对的生成时间戳（旧版本或手改的块）。
   * 优选 IP 是某一次测速的快照，写进系统 hosts 后不会自己更新，所以必须让 UI 能说
   * "这是 N 天前测的 / 时效未知"，否则 GitHub 轮换 IP 后加速变成阻断而无人知。
   */
  staleDays: number | null
  /** 是否该提示重选（超阈值或时效未知） */
  stale: boolean
  message?: string
}

function hostsPath(): string {
  return join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'drivers', 'etc', 'hosts')
}

/** 社区优选 IP 源的缓存时长：它只是候选项，能不能用/快不快仍由本地测速决定 */
const COMMUNITY_CACHE_MS = 30 * 60_000

/** 自动重选的最小间隔（巡检 30 分钟一轮，不能每轮都全量 DoH + TLS 测速） */
const AUTO_REFRESH_MIN_INTERVAL_MS = 60 * 60_000

/** TLS 握手超时：连不上的候选本来就该淘汰 */
const TLS_TIMEOUT_MS = 3000

/**
 * 写入 hosts 后清一次系统 DNS 缓存。
 * Windows 的 DNS Client 会缓存解析结果，改完 hosts 不清理，浏览器可能仍用旧 IP
 * 直到缓存过期 —— 用户看到的是「写了没用」。失败不阻断（最多延迟生效）。
 */
async function flushDns(): Promise<void> {
  if (process.platform !== 'win32') return
  // 绝对路径：本应用的 PATH 不可靠（同一类问题导致过 powershell.exe ENOENT），
  // 而 flushDns 的失败是静默的 —— 裸名找不到就等于“写了没用”且无人知
  const exe = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'ipconfig.exe')
  await new Promise<void>((resolvePromise) => {
    execFile(exe, ['/flushdns'], { timeout: 5000, windowsHide: true }, () => resolvePromise())
  })
}

/** 两份托管条目是否完全一致（用于避免无意义的重写） */
function sameEntries(
  a: { domain: string; ip: string }[],
  b: { domain: string; ip: string }[],
): boolean {
  if (a.length !== b.length) return false
  const want = new Map(b.map(e => [e.domain, e.ip]))
  return a.every(e => want.get(e.domain) === e.ip)
}

/**
 * PowerShell 单引号字符串字面量。
 * 路径里的单引号是合法字符（用户名可含 `'`），不翻倍转义会把命令拼成语法错误。
 */
function psLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`
}

/** HTTPS GET 文本（DoH / 社区 hosts），带超时与单次重定向 */
function fetchBody(url: string): Promise<string> {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false
    const done = (v: string) => { if (!settled) { settled = true; resolvePromise(v) } }
    const fail = (e: unknown) => { if (!settled) { settled = true; rejectPromise(e) } }
    const go = (target: string, redirects: number) => {
      const req = https.get(target, { timeout: 6000, headers: { 'User-Agent': 'cKunAgentSSH-ghaccel-hosts' } }, (res) => {
        if (res.statusCode! >= 300 && res.statusCode! < 400 && res.headers.location && redirects > 0) {
          res.resume()
          // Location 畸形时 new URL 会在 'response' 回调里抛出，promise 既不
          // resolve 也不 reject，DoH 查询就永久悬挂
          let next: string
          try {
            next = new URL(res.headers.location, target).toString()
          } catch (e) {
            return fail(e)
          }
          go(next, redirects - 1)
          return
        }
        const chunks: Buffer[] = []
        res.on('data', (c: Buffer) => chunks.push(c))
        res.on('end', () => done(Buffer.concat(chunks).toString('utf-8')))
        res.on('error', fail)
      })
      req.once('timeout', () => { req.destroy(); fail(new Error('DoH 请求超时')) })
      req.once('error', fail)
    }
    go(url, 1)
  })
}

/** HTTPS GET JSON（DoH 应答） */
async function fetchJson(url: string): Promise<unknown> {
  return JSON.parse(await fetchBody(url))
}

/** 从 DoH JSON 应答提取 A 记录 IP 列表 */
function extractARecords(json: any): string[] {
  const answers: any[] = Array.isArray(json?.Answer) ? json.Answer : []
  const out = answers
    .filter(a => a && a.type === 1 && typeof a.data === 'string' && isValidIPv4(a.data))
    .map(a => a.data as string)
  return [...new Set(out)]
}

/**
 * 向公共 DoH 解析取并集，国内源优先：
 * 1. 先并发 alidns / doh.pub（国内直连可达，实测 <1s 返回）——拿到 ≥1 个 IP 立即返回，
 *    不等海外源；否则每个域名都要白等 dns.google 超时 / cloudflare ECONNRESET 的 6s，
 *    8 个域名并行下整体 pick 仍被最慢源拖到 6s+。
 * 2. 国内源全挂（无结果）才退 dns.google / cloudflare（海外备用）。
 */
async function resolveViaDoh(domain: string): Promise<string[]> {
  const domestic = [
    `https://dns.alidns.com/resolve?name=${domain}&type=A`,
    `https://doh.pub/dns-query?name=${domain}&type=A`,
  ].map(u => fetchJson(u))
  // 海外源仍然并发发起（真要走兜底时不该多等一次冷启动的 6s），但先挂一个吞掉
  // 失败的 handler：国内源命中时下面会直接 return，这一批 promise 再没人 await，
  // 它们的 ECONNRESET / 超时就会落到 main.ts 的 unhandledRejection 上，把"真出了
  // 没人管的问题"埋进一堆本来就不打算采用的失败里。挂 handler 只改变是否惊动全局，
  // 值与拒绝原因原样保留，兜底路径的 allSettled 判定不受影响。
  const unimportant = (p: Promise<unknown>): Promise<unknown> => {
    p.catch(() => { /* 结果未被采用即无所谓失败 */ })
    return p
  }
  const overseas = [
    `https://dns.google/resolve?name=${domain}&type=A`,
    `https://cloudflare-dns.com/dns-query?name=${domain}&type=A`,
  ].map(u => unimportant(fetchJson(u)))

  const collect = async (jobs: Promise<unknown>[]): Promise<Set<string>> => {
    const results = await Promise.allSettled(jobs)
    const ips = new Set<string>()
    for (const r of results) {
      if (r.status === 'fulfilled') for (const ip of extractARecords(r.value)) ips.add(ip)
    }
    return ips
  }

  const fast = await collect(domestic)
  if (fast.size > 0) return [...fast]
  const slow = await collect(overseas)
  return [...slow]
}

export class GhHostsService {
  /** 域名 → 上次优选结果（内存缓存，重启后从 hosts 块里读） */
  private lastPicks: HostsPickRow[] = []

  /**
   * 整文件读-改-写串行队列（Q-E3，对齐 github-accel.ts 的 enqueue 纪律）：
   * apply/remove 的「读 hosts → 改托管块 → 写回」跨多个 await，并发两路会各自
   * 握着同一份旧内容再写回，后写者整块覆写先写者的托管块。链上每个 op 拿到的
   * 都是前一个 op 写完后的文件态。队列本身永不 reject（错误只透传给当次调用方），
   * 失败不阻塞后续操作。
   */
  private writeQueue: Promise<unknown> = Promise.resolve()

  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(op, op)
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }

  /** 社区源解析结果（域名 → IP 列表）缓存；失败不缓存，下一轮巡检会重试 */
  private communityCache: { at: number; byDomain: Map<string, string[]> } | null = null

  private async communityIps(domains: string[]): Promise<Map<string, string[]>> {
    if (this.communityCache && Date.now() - this.communityCache.at < COMMUNITY_CACHE_MS) {
      return this.communityCache.byDomain
    }
    const byDomain = new Map<string, string[]>()
    try {
      const text = await fetchBody(COMMUNITY_HOSTS_SOURCE)
      for (const { domain, ip } of parseCommunityHosts(text, domains)) {
        const list = byDomain.get(domain) ?? []
        if (!list.includes(ip)) list.push(ip)
        byDomain.set(domain, list)
      }
      this.communityCache = { at: Date.now(), byDomain }
    } catch (e) {
      // 社区源挂了只会少一路候选：DoH 仍能出结果，不升级成 pick 失败
      console.warn('[GhHosts] 社区优选 IP 源不可用:', e instanceof Error ? e.message : e)
    }
    return byDomain
  }

  /**
   * 优选：双源候选合并 → TCP 筛可达 → TLS/SNI 证书校验 → 取 TLS 最快的那个。
   *
   * 为什么必须校验证书：hosts 把域名钉到某个 IP 后，浏览器与 git 都会校验证书。
   * 只看 TCP 可达，会把“能握手但拿着别人的证书”的 IP（运营商/内网拦截）当成优选，
   * 写进去就是从加速器变成阻断器。
   */
  async pick(): Promise<HostsPickRow[]> {
    const domains = githubHostsDomains()
    const community = await this.communityIps(domains)
    const rows: HostsPickRow[] = []
    // 每域名解析 + 测速串行化不划算；域名间并行，单域名内部先解析后测速
    await Promise.all(domains.map(async domain => {
      try {
        const doh = await resolveViaDoh(domain)
        const ips = mergeHostCandidates(doh, community.get(domain) ?? [])
        if (ips.length === 0) {
          rows.push({ domain, ip: null, latencyMs: null, candidates: 0, verified: false, error: '本地 DoH 与社区源均无候选' })
          return
        }
        // 先用 TCP 把明显不可达的筛掉（便宜），再对最快的几个做 TLS 校验（贵）
        const probed = ips.slice(0, 8)
        const tested = await Promise.all(probed.map(ip => tcpLatency(ip)))
        const reachable = probed
          .map((ip, i) => ({ ip, tcpMs: tested[i] }))
          .filter((x): x is { ip: string; tcpMs: number } => x.tcpMs !== null)
          .sort((a, b) => a.tcpMs - b.tcpMs)
          .slice(0, 4)
        if (reachable.length === 0) {
          rows.push({ domain, ip: null, latencyMs: null, candidates: ips.length, verified: false, error: '候选 IP 均不可达（443 端口）' })
          return
        }
        const verified = await Promise.all(reachable.map(x => tlsProbe(x.ip, domain, TLS_TIMEOUT_MS)))
        let bestIp: string | null = null
        let bestMs: number | null = null
        reachable.forEach((x, i) => {
          const tls = verified[i]
          if (!tls.ok || tls.latencyMs === null) return
          if (bestMs === null || tls.latencyMs < bestMs) { bestIp = x.ip; bestMs = tls.latencyMs }
        })
        if (!bestIp) {
          rows.push({
            domain, ip: null, latencyMs: null, candidates: ips.length, verified: false,
            error: '候选 TCP 可达但证书校验未通过（可能被劫持或中间设备拦截）',
          })
          return
        }
        rows.push({ domain, ip: bestIp, latencyMs: bestMs, candidates: ips.length, verified: true })
      } catch (e) {
        rows.push({ domain, ip: null, latencyMs: null, candidates: 0, verified: false, error: e instanceof Error ? e.message : String(e) })
      }
    }))
    rows.sort((a, b) => domains.indexOf(a.domain) - domains.indexOf(b.domain))
    this.lastPicks = rows
    return rows
  }

  /** 上次自动重选时间（进程内即可：重启后重新计算一次也没坏处） */
  private lastAutoRefreshAt = 0

  /**
   * 自动重选（由巡检调用，默认关闭）。
   * - 节流 1 小时：巡检 30 分钟一轮，不能每轮都全量 DoH + TLS 测速
   * - 永不提权：后台弹 UAC 是最差的体验，那种情况留给红灯与手动按钮
   * - 结果与现有托管块一致就不重写，避开无意义的 flushdns
   */
  async autoRefresh(): Promise<{ changed: boolean; message?: string }> {
    if (Date.now() - this.lastAutoRefreshAt < AUTO_REFRESH_MIN_INTERVAL_MS) {
      return { changed: false, message: '距上次自动重选不足 1 小时' }
    }
    this.lastAutoRefreshAt = Date.now()
    const status = await this.status()
    if (!status.applied) return { changed: false }
    const picks = await this.pick()
    const entries = picks.filter(r => r.ip).map(r => ({ domain: r.domain, ip: r.ip! }))
    if (!entries.length) return { changed: false, message: '本轮优选未得到可用 IP' }
    if (!status.stale && sameEntries(status.entries, entries)) return { changed: false }
    try {
      await this.apply(entries, { allowElevate: false })
      return { changed: true, message: `已重新优选 ${entries.length} 个域名` }
    } catch (e) {
      return { changed: false, message: e instanceof Error ? e.message : String(e) }
    }
  }

  async status(): Promise<HostsStatus> {
    const path = hostsPath()
    try {
      const content = decodeHostsBytes(await readFile(path))
      const state = hostsBlockState(content)
      const applied = state === 'ok'
      return {
        applied,
        path,
        writable: true,
        entries: applied ? parseBlockEntries(content) : [],
        generatedAt: applied ? hostsBlockGeneratedAt(content) : null,
        staleDays: applied ? hostsBlockAgeDays(content) : null,
        stale: applied ? isHostsBlockStale(content) : false,
        message: state === 'broken' ? HOSTS_BLOCK_BROKEN_MESSAGE : undefined,
      }
    } catch (e: any) {
      // 读不到 hosts 就没有"托管块现状"可报，时效一律 null + 不提示过期
      // （没有块的时候催用户重选是噪声；applied=false 已经把状态说清了）
      const base = { applied: false, path, writable: false, entries: [], generatedAt: null, staleDays: null, stale: false }
      if (e?.code === 'ENOENT') return { ...base, message: 'hosts 文件不存在' }
      if (e?.code === 'EACCES' || e?.code === 'EPERM') {
        return { ...base, message: '无权限读取 hosts（需要管理员权限）' }
      }
      return { ...base, message: e instanceof Error ? e.message : String(e) }
    }
  }

  /**
   * 写入托管块：entries 为空则复用内存里上次优选结果。
   * 直接写失败（EACCES/EPERM，未以管理员身份运行）时走方案 A：按需 UAC 提权，
   * 由提权的 PowerShell 子进程完成写入，主应用不重启。
   * 首次写入前备份原 hosts 到 userData（还原托管块不依赖备份，但防意外）。
   */
  async apply(entries?: { domain: string; ip: string }[], opts: { allowElevate?: boolean } = {}): Promise<HostsStatus> {
    // 整文件读-改-写进串行队列（Q-E3）：并发 apply/remove 的读-改-写不再互相覆写
    return this.enqueue(async (): Promise<HostsStatus> => {
      const use = (entries && entries.length ? entries : this.lastPicks.filter(r => r.ip).map(r => ({ domain: r.domain, ip: r.ip! })))
      if (use.length === 0) throw new Error('没有可用的优选结果，请先执行「优选 IP」')

      const path = hostsPath()
      let existing: string
      try {
        existing = existsSync(path) ? decodeHostsBytes(await readFile(path)) : ''
      } catch (e: any) {
        if (e?.code === 'EACCES' || e?.code === 'EPERM') throw new Error('无权限读取 hosts（需要管理员权限运行应用）')
        throw e
      }
      const state = hostsBlockState(existing)
      if (state === 'broken') throw new Error(HOSTS_BLOCK_BROKEN_MESSAGE)
      if (state === 'none') {
        await this.backupHosts(path)
      }
      const next = applyHostsBlock(existing, use)
      try {
        await writeFile(path, encodeHostsBytes(next))
      } catch (e: any) {
        if (e?.code === 'EACCES' || e?.code === 'EPERM') {
          // 方案 A：按需提权写入（弹 UAC，用户确认后由提权子进程落盘）
          // 自动重选路径（allowElevate=false）在后台跑，绝不弹窗
          if (opts.allowElevate === false) {
            throw new Error('无权限写入 hosts（自动重选已跳过；请在界面上手动写入并授权）')
          }
          await this.elevatedWrite(next)
        } else {
          throw e
        }
      }
      await flushDns()
      // 注意：status() 只读不写，不需要也不应占用写队列（提权校验等读操作在
      // 队列外跑，避免与自己同队列的下一 op 死等）
      return this.status()
    })
  }

  /** 移除托管块（还原为写入前状态；首次写入的备份文件仍保留在 userData 供极端情况恢复） */
  async remove(): Promise<HostsStatus> {
    // 整文件读-改-写进串行队列（Q-E3），与 apply 同一条纪律
    return this.enqueue(async (): Promise<HostsStatus> => {
      const path = hostsPath()
      let existing: string
      try {
        existing = decodeHostsBytes(await readFile(path))
      } catch (e: any) {
        if (e?.code === 'EACCES' || e?.code === 'EPERM') {
          throw new Error('无权限读取 hosts（需要以管理员权限运行应用）')
        }
        if (e?.code === 'ENOENT') return { applied: false, path, writable: true, entries: [], generatedAt: null, staleDays: null, stale: false }
        throw e
      }
      const state = hostsBlockState(existing)
      if (state === 'broken') throw new Error(HOSTS_BLOCK_BROKEN_MESSAGE)
      if (state === 'none') return this.status()
      const next = removeHostsBlock(existing)
      if (next !== existing) {
        try {
          await writeFile(path, encodeHostsBytes(next))
        } catch (e: any) {
          if (e?.code === 'EACCES' || e?.code === 'EPERM') {
            await this.elevatedWrite(next, 'no-block')
          } else {
            throw e
          }
        }
        await flushDns()
      }
      return this.status()
    })
  }

  /**
   * 方案 A：UAC 按需提权写 hosts。
   * 内容已在主进程算好（含标记块），提权子进程只做"整文件写入"这一个动作；
   * payload 走临时 JSON 文件，避免命令行长内容与引号转义地狱。
   * expect：'block'=写入后应存在托管块（apply 场景）；'no-block'=写入后应不存在（remove 场景）。
   */
  private async elevatedWrite(content: string, expect: 'block' | 'no-block' = 'block'): Promise<void> {
    const path = hostsPath()
    const tmpDir = join(app.getPath('userData'), 'tmp')
    await mkdir(tmpDir, { recursive: true })
    const stamp = Date.now()
    const payloadPath = join(tmpDir, `ghaccel-hosts-${stamp}.json`)
    // 提权子进程的"真实结果"回写这里（OK / FAIL: 原因）：仅靠退出码分不清
    // 「内层写入失败」「UAC 被取消」「子进程根本没起来」
    const resultPath = join(tmpDir, `ghaccel-hosts-${stamp}.result`)
    // 内容按 latin1 的原始字节转 base64：hosts 可能是 ANSI/GBK，若以 UTF-8 字符串
    // 传给提权进程，高位字节会被重新编码，块外内容又回到"UTF-8 解码再写回"的破坏路径。
    const contentBase64 = Buffer.from(encodeHostsBytes(content)).toString('base64')
    await writeFile(payloadPath, JSON.stringify({ path, contentBase64 }), 'utf-8')
    // SEC-002：脚本不再落盘。原实现把 .ps1 写到当前用户可写的 userData/tmp，
    // 再让提权后的 PowerShell -File 执行它 —— 同一用户的低权限进程可在写入
    // 与执行之间替换脚本内容（TOCTOU），让 UAC 弹窗后的管理员进程执行任意
    // 代码。改用 -EncodedCommand（Base64/UTF-16LE 内联）后磁盘只剩 payload
    // 数据文件，不再是代码通道。
    // SEC-002b：但 payload 仍是用户可写目录里的**数据**，管理员进程读它之前可能被改。
    // 早先的实现把写入目标也交给 payload.path，于是"被篡改最多改写 hosts 内容"并不
    // 成立——改掉 path 就是拿着管理员权限往任意路径写任意字节，仍是提权。现在目标
    // 路径钉在 -EncodedCommand 里（命令一旦拼好就不再变），payload 只携带 base64 内容；
    // 自述的 path 与钉死值不一致即判定被改，直接失败，不落盘。
    const script = [
      "$ErrorActionPreference = 'Stop'",
      `$target = ${psLiteral(path)}`,
      `$result = ${psLiteral(resultPath)}`,
      // 写结果文件：Start-Process -Wait 并不把子进程退出码交给父进程，提权子进程里
      // 真正抛的错会全丢，界面上只剩一个毫无信息的"退出码 1"
      'try {',
      `  $payload = Get-Content -LiteralPath ${psLiteral(payloadPath)} -Raw -Encoding UTF8 | ConvertFrom-Json`,
      // 文案里不许出现下面取消判据的那几个词（取消/拒绝/提升/无法执行）：本函数用
      // stderr 匹配它们来识别"用户在 UAC 弹窗上按了取消"，而路径不符是被篡改，
      // 撞上同一组词就会被报成"已取消管理员授权"，把一次攻击提示说成用户自己放弃了。
      "  if ($payload.path -cne $target) { throw 'payload 目标路径与命令内钉死的路径不符，写入已中止' }",
      '  $bytes = [Convert]::FromBase64String($payload.contentBase64)',
      '  [System.IO.File]::WriteAllBytes($target, $bytes)',
      "  Set-Content -LiteralPath $result -Value 'OK' -Encoding UTF8",
      '} catch {',
      "  Set-Content -LiteralPath $result -Value ('FAIL: ' + $_.Exception.Message) -Encoding UTF8",
      '  exit 1',
      '}',
    ].join('\r\n')
    const encoded = Buffer.from(script, 'utf-16le').toString('base64')

    // 必须用绝对路径启动：本应用的 PATH 常被裁剪（实测 Windows 上裸名
    // powershell.exe 直接 ENOENT，被上层映射成"退出码 1"，看起来像提权失败）
    const ps = resolvePowerShell()
    if (ps !== 'powershell' && !existsSync(ps)) {
      throw new Error(`未找到 Windows PowerShell（${ps}），无法提权写 hosts`)
    }

    const result = await new Promise<{ code: number | string; killed: boolean; stdout: string; stderr: string }>((resolvePromise) => {
      execFile(
        ps,
        [
          '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command',
          // -PassThru 才能把子进程退出码交给父进程；不能写成
          // `Start-Process ... | ForEach-Object { exit $_.ExitCode }`
          // （实测管道里的 exit 会把整个管道吞掉：退出码恒 0，子进程也没跑）
          `$proc = Start-Process -FilePath ${psLiteral(ps)} -Verb RunAs -Wait -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','${encoded}'; exit $proc.ExitCode`,
        ],
        { windowsHide: true, timeout: 120000 }, // UAC 弹窗可能等用户很久，60s 不够
        (err, stdout, stderr) => {
          const code = err ? (err as any).code : 0
          resolvePromise({
            code: typeof code === 'number' || typeof code === 'string' ? code : 1,
            killed: !!(err as any)?.killed,
            stdout: String(stdout ?? ''),
            stderr: String(stderr ?? ''),
          })
        },
      )
    })

    // 清理临时文件（提权子进程已结束，删得掉；删不掉也无害）
    try { await unlink(payloadPath) } catch { /* ignore */ }

    // 提权子进程写回来的真实结果（PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM）
    const outcome = await readFile(resultPath, 'utf-8')
      .then(s => s.replace(/^\uFEFF/, '').trim())
      .catch(() => '')
    try { await unlink(resultPath) } catch { /* ignore */ }

    if (outcome.startsWith('FAIL:')) {
      throw new Error(`提权写入 hosts 失败: ${outcome.slice(5).trim() || '未知原因'}`)
    }
    if (result.killed) throw new Error('等待管理员授权超时（120s），hosts 未修改')

    if (outcome !== 'OK') {
      const text = `${result.stdout}\n${result.stderr}`
      // UAC 取消 / 拒绝提升：给明确的中文取消文案，而不是一串英文。
      // 中英文系统的报错文案都要覆盖（英文 canceled/cancelled/elevation；中文 取消/拒绝/提升/无法执行）。
      if (/canceled|cancelled|The operation was canceled|取消|拒绝|提升|无法执行/i.test(text)) {
        throw new Error('已取消管理员授权，hosts 未修改')
      }
      if (result.code === 'ENOENT') throw new Error(`未找到 Windows PowerShell（${ps}），无法提权写 hosts`)
      throw new Error(`提权写入 hosts 失败: ${(result.stderr || result.stdout).trim() || `退出码 ${result.code}`}`)
    }

    // 提权子进程退出码 0 不等于写成功（用户也可能在 UAC 弹窗后仍失败）——回读校验
    const status = await this.status()
    const matched = expect === 'block' ? status.applied : !status.applied
    if (!matched) throw new Error('提权后 hosts 校验未通过，写入可能未生效')
  }

  private async backupHosts(path: string): Promise<void> {
    try {
      const dir = join(app.getPath('userData'), 'backups')
      await mkdir(dir, { recursive: true })
      await copyFile(path, join(dir, `hosts-backup-${Date.now()}`))
    } catch (e) {
      // 备份失败不阻断写入，但留日志
      console.warn('[GhHosts] hosts 备份失败（继续写入）:', e)
    }
  }
}

function parseBlockEntries(content: string): { domain: string; ip: string }[] {
  const begin = content.indexOf(HOSTS_BLOCK_BEGIN)
  const end = content.indexOf(HOSTS_BLOCK_END)
  if (begin === -1 || end === -1 || end <= begin) return []
  const lines = content.slice(begin, end).split(/\r?\n/)
  const entries: { domain: string; ip: string }[] = []
  for (const line of lines) {
    const t = line.replace(/#.*$/, '').trim()
    if (!t) continue
    const parts = t.split(/\s+/)
    if (parts.length === 2 && isValidIPv4(parts[0])) {
      entries.push({ ip: parts[0], domain: parts[1].toLowerCase() })
    }
  }
  return entries
}
