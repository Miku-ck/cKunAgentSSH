/**
 * GitHub 加速 —— 主进程服务（github-accel.json 持久化 + 镜像测速 + git/ssh 配置管理）
 *
 * 持久化骨架照抄 tunnel-store.ts：读-改-写串行化 / tmp+rename 原子写 / 损坏隔离。
 * git 操作走 execFile（不经 shell，无注入面）；测速走 node https（TTFB 计时）。
 * 纯逻辑（URL 解析 / 链接生成 / insteadOf 参数 / ssh 托管块）在 shared/github-accel-core.ts。
 */
import { app } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink, copyFile } from 'fs/promises'
import { existsSync } from 'fs'
import { homedir } from 'os'
import { execFile } from 'child_process'
import https from 'https'
import type { ClientRequest } from 'http'
import {
  applySshBlock,
  buildGitReadArgs,
  buildGitRestoreArgs,
  buildGitSetArgs,
  buildGitUnsetSubsectionArgs,
  buildProbeUrl,
  PROBE_TARGET,
  defaultGithubAccelConfig,
  findGithubInsteadOf,
  hasSshBlock,
  removeSshBlock,
  validateProxyPrefix,
  validateWebMirrorHost,
  parseGitInsteadOf,
  GITHUB_BASE,
  type AccelProxy,
  type AccelWebMirror,
  type GithubAccelConfig,
  type InsteadOfEntry,
} from '../shared/github-accel-core'

export type { AccelProxy, AccelWebMirror, GithubAccelConfig }

export interface ProbeResult {
  /** 探测目标（proxy id 或前缀本身） */
  target: string
  ok: boolean
  /** 首字节延迟（ms）；失败为 null */
  latencyMs: number | null
  status?: number
  error?: string
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

function getStorePath(): string {
  return join(app.getPath('userData'), 'github-accel.json')
}

function getSshConfigPath(): string {
  return join(homedir(), '.ssh', 'config')
}

function execGit(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolvePromise) => {
    execFile('git', args, { timeout: GIT_TIMEOUT_MS, windowsHide: true }, (err, stdout, stderr) => {
      if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
        resolvePromise({ code: -1, stdout: '', stderr: 'git-not-found' })
        return
      }
      // execFile 的 err.code 对非零退出是数字退出码
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
    if (!obj || typeof obj !== 'object' || !Array.isArray(obj.proxies) || !Array.isArray(obj.webMirrors)) {
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
  const savedWeb: any[] = Array.isArray(obj.webMirrors) ? obj.webMirrors : []

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

  const webMirrors: AccelWebMirror[] = defaults.webMirrors.map(d => {
    const s = savedWeb.find(x => x && x.id === d.id)
    return { ...d, enabled: s ? !!s.enabled : d.enabled }
  })
  for (const s of savedWeb) {
    if (!s || typeof s !== 'object' || typeof s.id !== 'string' || s.builtin) continue
    if (typeof s.host !== 'string' || validateWebMirrorHost(s.host)) continue
    if (webMirrors.some(w => w.id === s.id)) continue
    webMirrors.push({ id: s.id, name: String(s.name || s.host), host: s.host.toLowerCase(), builtin: false, enabled: !!s.enabled })
  }

  const gitBackup = obj.gitBackup && typeof obj.gitBackup === 'object' && typeof obj.gitBackup.prefix === 'string'
    ? { prefix: obj.gitBackup.prefix, previousValue: typeof obj.gitBackup.previousValue === 'string' ? obj.gitBackup.previousValue : null }
    : null

  return {
    proxies,
    webMirrors,
    gitBackup,
    lastProxyPrefix: typeof obj.lastProxyPrefix === 'string' ? obj.lastProxyPrefix : undefined,
  }
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

/** 单次 HTTPS 探测：GET 全地址，计首字节时间，跟一次重定向。 */
function probeOnce(url: string, redirectsLeft: number): Promise<ProbeResult> {
  return new Promise((resolvePromise) => {
    const started = Date.now()
    let settled = false
    const done = (r: ProbeResult) => {
      if (settled) return
      settled = true
      resolvePromise(r)
    }
    let req: ClientRequest
    try {
      req = https.get(url, {
        timeout: PROBE_TIMEOUT_MS,
        headers: { 'User-Agent': 'cKunAgentSSH-ghaccel-probe' },
      }, (res) => {
        const status = res.statusCode ?? 0
        // 重定向跟一次（ghproxy 类偶尔 302 到 CDN）
        if (status >= 300 && status < 400 && res.headers.location && redirectsLeft > 0) {
          res.resume()
          const next = new URL(res.headers.location, url).toString()
          probeOnce(next, redirectsLeft - 1).then(r => done({ ...r, target: url }))
          return
        }
        res.once('data', () => {
          const latencyMs = Date.now() - started
          res.resume()
          done({ target: url, ok: status < 500, latencyMs, status })
        })
        res.once('end', () => {
          // 空响应体也算到达
          const latencyMs = Date.now() - started
          done({ target: url, ok: status < 500, latencyMs, status })
        })
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

// ─── 服务 ───

export class GithubAccelService {
  /** 读-改-写串行化（同 tunnel-store：防并发写互相覆写丢更新） */
  private writeQueue: Promise<unknown> = Promise.resolve()

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
   * 保存代理/镜像站清单（UI 整表提交）。内置条目的 id/prefix/host 不可改（防 UI 篡改
   * 出厂值），只允许翻转 enabled；用户条目需通过格式校验。
   */
  async saveLists(proxies: AccelProxy[], webMirrors: AccelWebMirror[]): Promise<GithubAccelConfig> {
    return this.enqueue(async () => {
      if (!Array.isArray(proxies) || !Array.isArray(webMirrors)) throw new Error('镜像清单格式无效')
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
      const nextWeb: AccelWebMirror[] = []
      for (const w of webMirrors) {
        if (!w || typeof w !== 'object' || typeof w.id !== 'string') throw new Error('镜像站条目无效')
        const builtin = defaults.webMirrors.find(d => d.id === w.id)
        if (w.builtin || builtin) {
          if (!builtin) throw new Error(`未知的内置镜像站: ${w.id}`)
          nextWeb.push({ ...builtin, enabled: !!w.enabled })
          continue
        }
        const err = validateWebMirrorHost(w.host)
        if (err) throw new Error(`镜像站「${w.name || w.host}」：${err}`)
        if (nextWeb.some(x => x.host === w.host.trim().toLowerCase())) throw new Error(`镜像站域名重复: ${w.host}`)
        nextWeb.push({ id: w.id, name: String(w.name || w.host).slice(0, 40), host: w.host.trim().toLowerCase(), builtin: false, enabled: !!w.enabled })
      }
      const current = await readConfigFromDisk()
      const next: GithubAccelConfig = { ...current, proxies: nextProxies, webMirrors: nextWeb }
      await writeConfigToDisk(next)
      return next
    })
  }

  /** 对传入的地址列表逐个探测（并发），UI 用结果给镜像排序/标死活 */
  async probe(targets: { id: string; url: string }[]): Promise<(ProbeResult & { id: string })[]> {
    if (!Array.isArray(targets)) return []
    const jobs = targets.slice(0, 16).map(async t => {
      // 只探测 http(s)，防 file:// 等协议注入
      if (typeof t.url !== 'string' || !/^https:\/\//i.test(t.url)) {
        return { id: String(t.id), target: String(t.url ?? ''), ok: false, latencyMs: null, error: '仅支持 https 探测目标' }
      }
      const r = await probeOnce(t.url, 1)
      return { ...r, id: t.id }
    })
    return Promise.all(jobs)
  }

  /** 组装全部启用镜像的探测地址（含一个直连 raw 文件对照组，与代理同口径） */
  async probeAll(): Promise<(ProbeResult & { id: string; name: string })[]> {
    const config = await this.getConfig()
    const targets = [
      { id: '__direct__', name: '直连对照', url: PROBE_TARGET },
      ...config.proxies.filter(p => p.enabled).map(p => ({ id: p.id, name: p.name, url: buildProbeUrl(p.prefix) })),
      ...config.webMirrors.filter(w => w.enabled).map(w => ({ id: w.id, name: w.name, url: `https://${w.host}/` })),
    ]
    const results = await this.probe(targets)
    return results.map(r => ({ ...r, name: targets.find(t => t.id === r.id)?.name ?? r.id }))
  }

  /** 记住一键加速使用的镜像前缀 */
  async saveOptions(patch: { lastProxyPrefix?: string }): Promise<GithubAccelConfig> {
    return this.enqueue(async () => {
      const current = await readConfigFromDisk()
      const next: GithubAccelConfig = {
        ...current,
        lastProxyPrefix: typeof patch.lastProxyPrefix === 'string' ? patch.lastProxyPrefix : current.lastProxyPrefix,
      }
      await writeConfigToDisk(next)
      return next
    })
  }

  // ─── git insteadOf ───

  async gitStatus(): Promise<GitInsteadOfStatus> {
    const r = await execGit(buildGitReadArgs())
    if (r.stderr === 'git-not-found') {
      return { available: false, entries: [], active: false, managed: false, backup: null, message: '未检测到 git，请先安装 Git' }
    }
    // 无条目时 git 退出码为 1、输出为空 —— 正常情况
    if (r.code !== 0 && r.code !== 1) {
      return { available: true, entries: [], active: false, managed: false, backup: null, message: `git config 查询失败: ${r.stderr.trim() || `退出码 ${r.code}`}` }
    }
    const entries = parseGitInsteadOf(r.stdout)
    const activeEntry = findGithubInsteadOf(entries)
    const config = await this.getConfig()
    const managed = !!activeEntry && config.proxies.some(p => activeEntry.subsection === `${p.prefix}${GITHUB_BASE}`)
    return {
      available: true,
      entries,
      active: !!activeEntry,
      activeSubsection: activeEntry?.subsection,
      managed,
      backup: config.gitBackup?.previousValue ?? null,
    }
  }

  /**
   * 开启 insteadOf 接管：git clone https://github.com/... 一律改写为 <prefix>https://github.com/...
   * 若用户已有其它 github.com 改写规则，先备份原值（关闭时还原；保留最早一份备份）。
   */
  async gitEnable(prefix: string): Promise<GitInsteadOfStatus> {
    const err = validateProxyPrefix(prefix)
    if (err) throw new Error(err)
    return this.enqueue(async () => {
      const before = await this.gitStatus()
      if (!before.available) throw new Error(before.message || 'git 不可用')
      const existing = before.activeSubsection
      const target = `${prefix}${GITHUB_BASE}`
      const config = await this.getConfig()

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

      const set = await execGit(buildGitSetArgs(prefix))
      if (set.code !== 0) throw new Error(`写入 git 配置失败: ${set.stderr.trim() || `退出码 ${set.code}`}`)
      await writeConfigToDisk(config)
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
      config.gitBackup = null
      await writeConfigToDisk(config)
      return this.gitStatus()
    })
  }

  // ─── ~/.ssh/config 443 托管块 ───

  async ssh443Status(): Promise<Ssh443Status> {
    const path = getSshConfigPath()
    if (!existsSync(path)) return { applied: false, path, exists: false }
    try {
      const content = await readFile(path, 'utf-8')
      return { applied: hasSshBlock(content), path, exists: true }
    } catch (e) {
      return { applied: false, path, exists: true, message: e instanceof Error ? e.message : String(e) }
    }
  }

  async ssh443Apply(): Promise<Ssh443Status> {
    const path = getSshConfigPath()
    let existing = ''
    if (existsSync(path)) {
      existing = await readFile(path, 'utf-8')
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
    await writeFile(path, next, 'utf-8')
    return { applied: true, path, exists: true }
  }

  async ssh443Remove(): Promise<Ssh443Status> {
    const path = getSshConfigPath()
    if (!existsSync(path)) return { applied: false, path, exists: false }
    const existing = await readFile(path, 'utf-8')
    const next = removeSshBlock(existing)
    if (next !== existing) await writeFile(path, next, 'utf-8')
    return { applied: false, path, exists: true }
  }
}
