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
  message?: string
}

const TCP_TEST_TIMEOUT_MS = 4000

function hostsPath(): string {
  return join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'drivers', 'etc', 'hosts')
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
          go(new URL(res.headers.location, target).toString(), redirects - 1)
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

/** 并发向两个公共 DoH 解析，取并集（一个被墙时另一个兜底） */
async function resolveViaDoh(domain: string): Promise<string[]> {
  const urls = [
    `https://dns.google/resolve?name=${domain}&type=A`,
    `https://cloudflare-dns.com/dns-query?name=${domain}&type=A`,
  ]
  const results = await Promise.allSettled(urls.map(u => fetchJson(u)))
  const ips = new Set<string>()
  for (const r of results) {
    if (r.status === 'fulfilled') for (const ip of extractARecords(r.value)) ips.add(ip)
  }
  return [...ips]
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
      return { applied: hasHostsBlock(content), path, writable: true, entries }
    } catch (e: any) {
      if (e?.code === 'ENOENT') return { applied: false, path, writable: false, entries: [], message: 'hosts 文件不存在' }
      if (e?.code === 'EACCES' || e?.code === 'EPERM') {
        return { applied: false, path, writable: false, entries: [], message: '无权限读取 hosts（需要管理员权限）' }
      }
      return { applied: false, path, writable: false, entries: [], message: e instanceof Error ? e.message : String(e) }
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
      if (e?.code === 'ENOENT') return { applied: false, path, writable: true, entries: [] }
      throw e
    }
    const next = removeHostsBlock(existing)
    if (next !== existing) {
      try {
        await writeFile(path, next, 'utf-8')
      } catch (e: any) {
        if (e?.code === 'EACCES' || e?.code === 'EPERM') {
          await this.elevatedWrite(next)
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
   */
  private async elevatedWrite(content: string): Promise<void> {
    const path = hostsPath()
    const tmpDir = join(app.getPath('userData'), 'tmp')
    await mkdir(tmpDir, { recursive: true })
    const stamp = Date.now()
    const payloadPath = join(tmpDir, `ghaccel-hosts-${stamp}.json`)
    const scriptPath = join(tmpDir, `ghaccel-hosts-${stamp}.ps1`)
    await writeFile(payloadPath, JSON.stringify({ path, content }), 'utf-8')
    // 脚本只做一件事：把 payload.content 原样写入 payload.path（UTF-8 无 BOM）
    const script = [
      "$ErrorActionPreference = 'Stop'",
      `$payload = Get-Content -LiteralPath '${payloadPath}' -Raw -Encoding UTF8 | ConvertFrom-Json`,
      '$bytes = [System.Text.Encoding]::UTF8.GetBytes($payload.content)',
      '[System.IO.File]::WriteAllBytes($payload.path, $bytes)',
    ].join('\r\n')
    await writeFile(scriptPath, script, 'utf-8')

    const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolvePromise) => {
      execFile(
        'powershell.exe',
        [
          '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command',
          `Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','${scriptPath}'`,
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
    try { await unlink(scriptPath) } catch { /* ignore */ }

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
    if (!status.applied) throw new Error('提权后 hosts 校验未通过，写入可能未生效')
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
