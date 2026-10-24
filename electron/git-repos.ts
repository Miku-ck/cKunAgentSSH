/**
 * Git 仓库管理 —— 主进程服务（本地仓库注册表 git-repos.json + 目录扫描 + 本地 git 执行）。
 *
 * 持久化骨架照抄 github-accel.ts（读-改-写经 enqueue 串行化 / tmp+rename 原子写 /
 * 损坏隔离备份）。git 一律 execFile + `-C <path>` 参数数组（不经 shell，无注入面）；
 * 解析与命令构造的纯逻辑在 shared/git-repos-core.ts（单测在 electron/git-repos-core.test.ts）。
 * 远程仓库不在此处执行：渲染层构造命令经 sshExec 下发（同 Docker 路线），
 * 本服务只替渲染层保管「按 connectionId 记忆的远端路径表」。
 */
import { app } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink, readdir } from 'fs/promises'
import { existsSync } from 'fs'
import { execFile } from 'child_process'
import {
  GIT_LOG_FORMAT,
  firstLine,
  parseGitLog,
  parseGitStatusPorcelain,
  type GitRawResult,
  type GitReposRegistry,
  type LocalRepoStatusResult,
} from '../shared/git-repos-core'

export type { GitRawResult, GitReposRegistry }

const GIT_TIMEOUT_MS = 8_000
const FETCH_TIMEOUT_MS = 60_000
/** pull 含 fetch + 合并，慢网络下给足预算 */
const PULL_TIMEOUT_MS = 120_000
/** statusAll 并发上限：几十个仓库时不对进程池倾泻 */
const STATUS_CONCURRENCY = 6
/** 扫描深度与结果上限：防误选根目录后遍历整块盘 */
const SCAN_MAX_DEPTH = 3
const SCAN_MAX_RESULTS = 200

function getStorePath(): string {
  return join(app.getPath('userData'), 'git-repos.json')
}

function defaultRegistry(): GitReposRegistry {
  return { version: 1, local: [], remote: {} }
}

function execGit(args: string[], timeoutMs = GIT_TIMEOUT_MS): Promise<GitRawResult> {
  return new Promise((resolvePromise) => {
    execFile('git', args, { timeout: timeoutMs, windowsHide: true }, (err, stdout, stderr) => {
      if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
        resolvePromise({ code: -1, stdout: '', stderr: 'git-not-found' })
        return
      }
      // execFile 的 err.code 对非零退出是数字退出码；超时被杀时为 null
      const code = err ? (typeof (err as unknown as { code?: number | string }).code === 'number' ? (err as unknown as { code: number }).code : 1) : 0
      resolvePromise({ code, stdout: String(stdout ?? ''), stderr: String(stderr ?? '') })
    })
  })
}

function isGitRepoPath(p: string): boolean {
  try {
    return existsSync(join(p, '.git'))
  } catch {
    return false
  }
}

/** Windows（盘符 / UNC）与 POSIX 绝对路径都认；相对路径登记进来没有意义 */
function isAbsolutePath(p: string): boolean {
  return /^[a-zA-Z]:[\\/]/.test(p) || p.startsWith('\\\\') || p.startsWith('/')
}

async function quarantineCorruptFile(path: string, raw: string): Promise<void> {
  try {
    const bak = `${path}.corrupt-${Date.now()}.bak`
    try {
      await rename(path, bak)
    } catch {
      await writeFile(bak, raw, 'utf-8')
    }
    console.warn(`[GitRepos] 损坏文件已备份到: ${bak}`)
  } catch (e) {
    console.warn('[GitRepos] 备份损坏文件失败:', e)
  }
}

async function readRegistryFromDisk(): Promise<GitReposRegistry> {
  const path = getStorePath()
  if (!existsSync(path)) return defaultRegistry()
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // 读失败向上抛，防止后续写以出厂配置覆写用户配置（tunnel-store 同口径）
    throw new Error(`读取仓库注册表失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') {
      await quarantineCorruptFile(path, raw)
      return defaultRegistry()
    }
    return {
      version: 1,
      local: Array.isArray(obj.local) ? obj.local.filter((p: unknown): p is string => typeof p === 'string') : [],
      remote: obj.remote && typeof obj.remote === 'object'
        ? Object.fromEntries(
            Object.entries(obj.remote)
              .map(([k, v]) => [k, Array.isArray(v) ? v.filter((p: unknown): p is string => typeof p === 'string') : []]),
          )
        : {},
    }
  } catch (e) {
    if (e instanceof SyntaxError) {
      console.warn('[GitRepos] git-repos.json 损坏（JSON 解析失败），已隔离原文件')
      await quarantineCorruptFile(path, raw)
      return defaultRegistry()
    }
    throw e
  }
}

async function writeRegistryToDisk(reg: GitReposRegistry): Promise<void> {
  const path = getStorePath()
  await mkdir(dirname(path), { recursive: true })
  const tmpPath = path + '.tmp'
  const data = JSON.stringify(reg, null, 2)
  await writeFile(tmpPath, data, 'utf-8')
  // Windows rename 到已存在目标可能 EPERM/EBUSY：重试一次后回退直写（与 tunnel-store 同款）
  try {
    await rename(tmpPath, path)
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code
    if (code === 'EPERM' || code === 'EACCES' || code === 'EBUSY') {
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

export class GitReposService {
  /** 读-改-写串行化（与 GithubAccelService.enqueue 同款） */
  private queue: Promise<unknown> = Promise.resolve()

  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.queue.then(op)
    this.queue = run.catch(() => undefined)
    return run
  }

  getRegistry(): Promise<GitReposRegistry> {
    return this.enqueue(() => readRegistryFromDisk())
  }

  /** 登记本地仓库：绝对路径 + 目录下有 .git；重复登记幂等 */
  async addLocal(path: string): Promise<GitReposRegistry> {
    return this.enqueue(async () => {
      const p = String(path || '').trim()
      if (!isAbsolutePath(p)) throw new Error('请提供仓库的绝对路径')
      if (!isGitRepoPath(p)) throw new Error(`${p} 下没有 .git，不是 git 仓库`)
      const reg = await readRegistryFromDisk()
      if (!reg.local.includes(p)) reg.local.push(p)
      await writeRegistryToDisk(reg)
      return reg
    })
  }

  async addLocalMany(paths: string[]): Promise<GitReposRegistry> {
    return this.enqueue(async () => {
      const list = Array.isArray(paths) ? paths.map(p => String(p || '').trim()).filter(Boolean) : []
      const added: string[] = []
      for (const p of list) {
        if (isAbsolutePath(p) && isGitRepoPath(p)) added.push(p)
      }
      if (!added.length) throw new Error('没有可登记的仓库（路径须为绝对路径且含 .git）')
      const reg = await readRegistryFromDisk()
      for (const p of added) if (!reg.local.includes(p)) reg.local.push(p)
      await writeRegistryToDisk(reg)
      return reg
    })
  }

  /** 仅从注册表移除，绝不触碰磁盘文件 */
  async removeLocal(path: string): Promise<GitReposRegistry> {
    return this.enqueue(async () => {
      const reg = await readRegistryFromDisk()
      reg.local = reg.local.filter(p => p !== path)
      await writeRegistryToDisk(reg)
      return reg
    })
  }

  /** 远端路径表按 connectionId 记忆（完整覆盖写，调用方给出增删后的全集） */
  async setRemotePaths(connectionId: string, paths: string[]): Promise<GitReposRegistry> {
    return this.enqueue(async () => {
      const key = String(connectionId || '').trim()
      if (!key) throw new Error('缺少连接标识，无法记忆远端仓库')
      const list = (Array.isArray(paths) ? paths : [])
        .map(p => String(p || '').trim())
        .filter(Boolean)
      const reg = await readRegistryFromDisk()
      if (list.length) reg.remote[key] = list
      else delete reg.remote[key]
      await writeRegistryToDisk(reg)
      return reg
    })
  }

  /**
   * 扫描根目录发现 .git 仓库：BFS、深度 ≤3、命中仓库即止（不下钻其子目录）、
   * 跳过点开头目录与 node_modules，结果 ≤200。返回候选路径，登记与否由调用方决定。
   */
  async scanRoot(root: string): Promise<string[]> {
    const rootPath = String(root || '').trim()
    if (!isAbsolutePath(rootPath)) throw new Error('请提供扫描根目录的绝对路径')
    const found: string[] = []
    if (isGitRepoPath(rootPath)) return [rootPath]
    const queue: Array<{ dir: string; depth: number }> = [{ dir: rootPath, depth: 0 }]
    while (queue.length && found.length < SCAN_MAX_RESULTS) {
      const { dir, depth } = queue.shift()!
      let entries
      try {
        entries = await readdir(dir, { withFileTypes: true })
      } catch {
        continue // 无权限/已删除：跳过该目录，不中断整个扫描
      }
      for (const e of entries) {
        if (found.length >= SCAN_MAX_RESULTS) break
        if (!e.isDirectory() || e.name.startsWith('.') || e.name === 'node_modules') continue
        const child = join(dir, e.name)
        if (isGitRepoPath(child)) {
          found.push(child)
          continue
        }
        if (depth < SCAN_MAX_DEPTH) queue.push({ dir: child, depth: depth + 1 })
      }
    }
    return found
  }

  /** 单仓一次取数：status / 最近提交 / origin 三条 execFile 并行 */
  private async statusOne(path: string): Promise<LocalRepoStatusResult> {
    const [st, lg, og] = await Promise.all([
      execGit(['-C', path, 'status', '--porcelain=v1', '-b', '--ignore-submodules=dirty']),
      execGit(['-C', path, 'log', '-1', `--format=${GIT_LOG_FORMAT}`]),
      execGit(['-C', path, 'config', '--get', 'remote.origin.url']),
    ])
    if (st.code !== 0) {
      return {
        path,
        ok: false,
        error: st.code === -1 ? '未检测到 git（请安装 git 或加入 PATH）' : (firstLine(st.stderr) || `git status 退出码 ${st.code}`),
      }
    }
    return {
      path,
      ok: true,
      status: parseGitStatusPorcelain(st.stdout),
      lastCommit: lg.code === 0 ? (parseGitLog(lg.stdout)[0] ?? null) : null,
      originUrl: og.code === 0 ? (og.stdout.trim() || null) : null,
    }
  }

  /** 逐仓取数（并发上限分批）；单仓失败不拖垮整批，按行上浮 */
  async statusAll(paths: string[]): Promise<LocalRepoStatusResult[]> {
    const list = [...new Set(Array.isArray(paths) ? paths : [])]
    const results: LocalRepoStatusResult[] = []
    for (let i = 0; i < list.length; i += STATUS_CONCURRENCY) {
      const batch = await Promise.all(list.slice(i, i + STATUS_CONCURRENCY).map(p => this.statusOne(p)))
      results.push(...batch)
    }
    return results
  }

  async fetchOne(path: string): Promise<GitRawResult> {
    return execGit(['-C', path, 'fetch', '--all', '--prune'], FETCH_TIMEOUT_MS)
  }

  async pullOne(path: string): Promise<GitRawResult> {
    return execGit(['-C', path, 'pull', '--ff-only'], PULL_TIMEOUT_MS)
  }

  /** 详情三段原始输出（解析在渲染层 parseRepoDetailSections，单测共用同一份） */
  async detail(path: string): Promise<{ ok: boolean; error?: string; statusRaw?: string; branchRaw?: string; logRaw?: string }> {
    const [st, br, lg] = await Promise.all([
      execGit(['-C', path, 'status', '--porcelain=v1', '--ignore-submodules=dirty']),
      execGit(['-C', path, 'branch', '-vv']),
      execGit(['-C', path, 'log', '-20', `--format=${GIT_LOG_FORMAT}`]),
    ])
    if (st.code !== 0) {
      return { ok: false, error: firstLine(st.stderr) || '取数失败' }
    }
    return { ok: true, statusRaw: st.stdout, branchRaw: br.stdout, logRaw: lg.stdout }
  }
}
