/**
 * GitHub 加速 —— hosts 优选 IP（M3 增强模式）
 *
 * 流程：DoH（dns.google / cloudflare-dns）绕过本地污染解析 GitHub 系域名 →
 * 候选 IP 并发 TCP connect:443 测速取最优 → 渲染 hosts 托管块（BEGIN/END 标记圈定，
 * 纯逻辑在 shared 核心）写入系统 hosts。
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
import net from 'net'
import {
  applyHostsBlock,
  githubHostsDomains,
  hasHostsBlock,
  hostsBlockAgeDays,
  hostsBlockGeneratedAt,
  isHostsBlockStale,
  isValidIPv4,
  removeHostsBlock,
  HOSTS_BLOCK_BEGIN,
  HOSTS_BLOCK_END,
} from '../shared/github-accel-core'

export interface HostsPickRow {
  domain: string
  /** 选中的最优 IP；解析失败为 null */
  ip: string | null
  /** TCP connect 延迟；未测出为 null */
  latencyMs: number | null
  /** 解析到的候选总数 */
  candidates: number
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

const TCP_TEST_TIMEOUT_MS = 4000

function hostsPath(): string {
  return join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'drivers', 'etc', 'hosts')
}

/**
 * PowerShell 单引号字符串字面量。
 * 路径里的单引号是合法字符（用户名可含 `'`），不翻倍转义会把命令拼成语法错误。
 */
function psLiteral(value: string): string {
  return `'${value.replace(/'/g, "''")}'`
}

/** HTTPS GET JSON（DoH），带超时与单次重定向 */
function fetchJson(url: string): Promise<unknown> {
  return new Promise((resolvePromise, rejectPromise) => {
    let settled = false
    const done = (v: unknown) => { if (!settled) { settled = true; resolvePromise(v) } }
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
        res.on('end', () => {
          try { done(JSON.parse(Buffer.concat(chunks).toString('utf-8'))) } catch (e) { fail(e) }
        })
        res.on('error', fail)
      })
      req.once('timeout', () => { req.destroy(); fail(new Error('DoH 请求超时')) })
      req.once('error', fail)
    }
    go(url, 1)
  })
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

/** TCP connect:443 测速；失败返回 null */
function tcpLatency(ip: string, port = 443): Promise<number | null> {
  return new Promise((resolvePromise) => {
    const started = Date.now()
    const sock = net.connect(port, ip)
    let settled = false
    const done = (v: number | null) => { if (!settled) { settled = true; sock.destroy(); resolvePromise(v) } }
    sock.once('connect', () => done(Date.now() - started))
    sock.once('error', () => done(null))
    sock.once('timeout', () => done(null))
    sock.setTimeout(TCP_TEST_TIMEOUT_MS)
  })
}

export class GhHostsService {
  /** 域名 → 上次优选结果（内存缓存，重启后从 hosts 块里读） */
  private lastPicks: HostsPickRow[] = []

  /** 优选：DoH 解析 + TCP 测速，返回每个域名的最优 IP */
  async pick(): Promise<HostsPickRow[]> {
    const domains = githubHostsDomains()
    const rows: HostsPickRow[] = []
    // 每域名解析 + 测速串行化不划算；域名间并行，单域名内部先解析后测速
    await Promise.all(domains.map(async domain => {
      try {
        const ips = await resolveViaDoh(domain)
        if (ips.length === 0) {
          rows.push({ domain, ip: null, latencyMs: null, candidates: 0, error: 'DoH 解析无结果' })
          return
        }
        // 最多测 8 个候选，全部并发
        const tested = await Promise.all(ips.slice(0, 8).map(ip => tcpLatency(ip)))
        let bestIp: string | null = null
        let bestMs: number | null = null
        ips.slice(0, 8).forEach((ip, i) => {
          const ms = tested[i]
          if (ms !== null && (bestMs === null || ms < bestMs)) { bestIp = ip; bestMs = ms }
        })
        if (!bestIp) {
          rows.push({ domain, ip: null, latencyMs: null, candidates: ips.length, error: '候选 IP 均不可达（443 端口）' })
          return
        }
        rows.push({ domain, ip: bestIp, latencyMs: bestMs, candidates: ips.length })
      } catch (e) {
        rows.push({ domain, ip: null, latencyMs: null, candidates: 0, error: e instanceof Error ? e.message : String(e) })
      }
    }))
    rows.sort((a, b) => domains.indexOf(a.domain) - domains.indexOf(b.domain))
    this.lastPicks = rows
    return rows
  }

  async status(): Promise<HostsStatus> {
    const path = hostsPath()
    try {
      const content = await readFile(path, 'utf-8')
      const entries = parseBlockEntries(content)
      return {
        applied: hasHostsBlock(content),
        path,
        writable: true,
        entries,
        generatedAt: hostsBlockGeneratedAt(content),
        staleDays: hostsBlockAgeDays(content),
        stale: isHostsBlockStale(content),
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
  async apply(entries?: { domain: string; ip: string }[]): Promise<HostsStatus> {
    const use = (entries && entries.length ? entries : this.lastPicks.filter(r => r.ip).map(r => ({ domain: r.domain, ip: r.ip! })))
    if (use.length === 0) throw new Error('没有可用的优选结果，请先执行「优选 IP」')

    const path = hostsPath()
    let existing: string
    try {
      existing = existsSync(path) ? await readFile(path, 'utf-8') : ''
    } catch (e: any) {
      if (e?.code === 'EACCES' || e?.code === 'EPERM') throw new Error('无权限读取 hosts（需要管理员权限运行应用）')
      throw e
    }
    const hadBlock = hasHostsBlock(existing)
    if (!hadBlock) {
      await this.backupHosts(path)
    }
    const next = applyHostsBlock(existing, use)
    try {
      await writeFile(path, next, 'utf-8')
    } catch (e: any) {
      if (e?.code === 'EACCES' || e?.code === 'EPERM') {
        // 方案 A：按需提权写入（弹 UAC，用户确认后由提权子进程落盘）
        await this.elevatedWrite(next)
      } else {
        throw e
      }
    }
    return this.status()
  }

  /** 移除托管块（还原为写入前状态；首次写入的备份文件仍保留在 userData 供极端情况恢复） */
  async remove(): Promise<HostsStatus> {
    const path = hostsPath()
    let existing: string
    try {
      existing = await readFile(path, 'utf-8')
    } catch (e: any) {
      if (e?.code === 'EACCES' || e?.code === 'EPERM') {
        throw new Error('无权限读取 hosts（需要以管理员权限运行应用）')
      }
      if (e?.code === 'ENOENT') return { applied: false, path, writable: true, entries: [], generatedAt: null, staleDays: null, stale: false }
      throw e
    }
    const next = removeHostsBlock(existing)
    if (next !== existing) {
      try {
        await writeFile(path, next, 'utf-8')
      } catch (e: any) {
        if (e?.code === 'EACCES' || e?.code === 'EPERM') {
          await this.elevatedWrite(next, 'no-block')
        } else {
          throw e
        }
      }
    }
    return this.status()
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
    await writeFile(payloadPath, JSON.stringify({ path, content }), 'utf-8')
    // SEC-002：脚本不再落盘。原实现把 .ps1 写到当前用户可写的 userData/tmp，
    // 再让提权后的 PowerShell -File 执行它 —— 同一用户的低权限进程可在写入
    // 与执行之间替换脚本内容（TOCTOU），让 UAC 弹窗后的管理员进程执行任意
    // 代码。改用 -EncodedCommand（Base64/UTF-16LE 内联）后磁盘只剩 payload
    // 数据文件，不再是代码通道。
    // SEC-002b：但 payload 仍是用户可写目录里的**数据**，管理员进程读它之前可能被改。
    // 早先的实现把写入目标也交给 payload.path，于是"被篡改最多改写 hosts 内容"并不
    // 成立——改掉 path 就是拿着管理员权限往任意路径写任意字节，仍是提权。现在目标
    // 路径钉在 -EncodedCommand 里（命令一旦拼好就不再变），payload 只携带 content；
    // 自述的 path 与钉死值不一致即判定被改，直接失败，不落盘。
    const script = [
      "$ErrorActionPreference = 'Stop'",
      `$target = ${psLiteral(path)}`,
      `$payload = Get-Content -LiteralPath ${psLiteral(payloadPath)} -Raw -Encoding UTF8 | ConvertFrom-Json`,
      // 文案里不许出现下面取消判据的那几个词（取消/拒绝/提升/无法执行）：本函数用
      // stderr 匹配它们来识别"用户在 UAC 弹窗上按了取消"，而路径不符是被篡改，
      // 撞上同一组词就会被报成"已取消管理员授权"，把一次攻击提示说成用户自己放弃了。
      "if ($payload.path -cne $target) { throw 'payload 目标路径与命令内钉死的路径不符，写入已中止' }",
      '$bytes = [System.Text.Encoding]::UTF8.GetBytes($payload.content)',
      '[System.IO.File]::WriteAllBytes($target, $bytes)',
    ].join('\r\n')
    const encoded = Buffer.from(script, 'utf-16le').toString('base64')

    const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolvePromise) => {
      execFile(
        'powershell.exe',
        [
          '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command',
          `Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','${encoded}'`,
        ],
        { windowsHide: true, timeout: 120000 }, // UAC 弹窗可能等用户很久，60s 不够
        (err, stdout, stderr) => {
          resolvePromise({
            code: err ? (typeof (err as any).code === 'number' ? (err as any).code : 1) : 0,
            stdout: String(stdout ?? ''),
            stderr: String(stderr ?? ''),
          })
        },
      )
    })

    // 清理临时文件（提权子进程已结束，删得掉；删不掉也无害）
    try { await unlink(payloadPath) } catch { /* ignore */ }

    if (result.code !== 0) {
      const text = `${result.stdout}\n${result.stderr}`
      // UAC 取消 / 拒绝提升：给明确的中文取消文案，而不是一串英文。
      // 中英文系统的报错文案都要覆盖（英文 canceled/cancelled/elevation；中文 取消/拒绝/提升/无法执行）。
      if (/canceled|cancelled|The operation was canceled|取消|拒绝|提升|无法执行/i.test(text)) {
        throw new Error('已取消管理员授权，hosts 未修改')
      }
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
