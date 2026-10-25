/**
 * Git 仓库管理 —— 主进程服务（本地仓库注册表 git-repos.json + 本地 git 执行）。
 *
 * 持久化骨架照抄 github-accel.ts（读-改-写经 enqueue 串行化 / tmp+rename 原子写 /
 * 损坏隔离备份）。git 一律 execFile + `-C <path>` 参数数组（不经 shell，无注入面）；
 * 解析与命令构造的纯逻辑在 shared/git-repos-core.ts（单测在 electron/git-repos-core.test.ts）。
 * 远程仓库不在此处执行：渲染层构造命令经 sshExec 下发（同 Docker 路线），
 * 本服务只替渲染层保管「按 connectionId 记忆的远端路径表」。
 */
import { app } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink } from 'fs/promises'
import { existsSync } from 'fs'
import { execFile } from 'child_process'
import {
  GIT_LOG_FORMAT,
  buildGraphLogArgs,
  firstLine,
  isValidBranchName,
  isValidCommitFiles,
  isValidCommitMsg,
  isValidRemoteRepoPath,
  parseGitLog,
  parseGitStatusPorcelain,
  parseRemoteConfig,
  pickRemote,
  type GitRawResult,
  type GitReposRegistry,
  type LocalRepoStatusResult,
} from '../shared/git-repos-core'

export type { GitRawResult, GitReposRegistry }

const GIT_TIMEOUT_MS = 8_000
const FETCH_TIMEOUT_MS = 60_000
/** pull 含 fetch + 合并，慢网络下给足预算 */
const PULL_TIMEOUT_MS = 120_000
/** switch / add / commit 是本地操作，常规预算 */
const ACTION_TIMEOUT_MS = 30_000
/** push 走网络（可能带新上传包），给足预算 */
const PUSH_TIMEOUT_MS = 120_000
/** statusAll 并发上限：几十个仓库时不对进程池倾泻 */
const STATUS_CONCURRENCY = 6
/** 单条命令输出上限：仓库多 / 长提交主题时 1MB 默认缓冲容易被顶爆 */
const GIT_MAX_BUFFER = 4 * 1024 * 1024
/** 超时被杀沿用 coreutils timeout 的退出码约定 */
const TIMEOUT_EXIT_CODE = 124

function getStorePath(): string {
  return join(app.getPath('userData'), 'git-repos.json')
}

function defaultRegistry(): GitReposRegistry {
  return { version: 1, local: [], remote: {} }
}

function execGit(args: string[], timeoutMs = GIT_TIMEOUT_MS): Promise<GitRawResult> {
  return new Promise((resolvePromise) => {
    execFile('git', args, { timeout: timeoutMs, windowsHide: true, maxBuffer: GIT_MAX_BUFFER }, (err, stdout, stderr) => {
      if (err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
        resolvePromise({ code: -1, stdout: '', stderr: 'git-not-found' })
        return
      }
      if (err) {
        const e = err as NodeJS.ErrnoException & { killed?: boolean }
        if (e.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
          resolvePromise({ code: 1, stdout: '', stderr: `git 输出超过 ${GIT_MAX_BUFFER} 字节上限` })
          return
        }
        // 超时被杀时 err.code 为 null，只有 killed 能认出来；不折成普通失败
        if (e.killed) {
          resolvePromise({
            code: TIMEOUT_EXIT_CODE,
            stdout: String(stdout ?? ''),
            stderr: String(stderr ?? '') || `git 执行超时（${timeoutMs}ms）`,
          })
          return
        }
      }
      // execFile 的 err.code 对非零退出是数字退出码
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

/** 执行入口复检：与登记入口同一口径，不给被劫持的渲染层任意目录执行 git 的机会 */
function assertAbsolutePath(p: string): string {
  const v = String(p ?? '').trim()
  if (!isAbsolutePath(v)) throw new Error(`非法路径（须为绝对路径）: ${v || '(空)'}`)
  return v
}

/** 读写动作入口：除了绝对路径，还要确认当下仍是 git 工作区（注册后目录可能已被删/换） */
function assertRepoPath(p: string): string {
  const v = assertAbsolutePath(p)
  if (!isGitRepoPath(v)) throw new Error(`不是 git 仓库（目录下没有 .git）: ${v}`)
  return v
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

/** 注册表来自磁盘（外部可改）：本地路径只留绝对路径，去重 */
function sanitizeLocalPaths(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out = new Set<string>()
  for (const p of value) {
    if (typeof p === 'string' && isAbsolutePath(p)) out.add(p)
  }
  return [...out]
}

/** 远端路径同理只留可安全拼进远程命令的 POSIX 绝对路径，去重 */
function sanitizeRemotePaths(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out = new Set<string>()
  for (const p of value) {
    if (typeof p === 'string' && isValidRemoteRepoPath(p)) out.add(p)
  }
  return [...out]
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
      local: sanitizeLocalPaths(obj.local),
      remote: obj.remote && typeof obj.remote === 'object'
        ? Object.fromEntries(
            Object.entries(obj.remote).map(([k, v]) => [k, sanitizeRemotePaths(v)]),
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
      const list = [...new Set(
        (Array.isArray(paths) ? paths : [])
          .map(p => String(p || '').trim())
          .filter(Boolean),
      )]
      const invalid = list.find(p => !isValidRemoteRepoPath(p))
      if (invalid) throw new Error(`非法远端仓库路径（须为无引号/控制字符的绝对 POSIX 路径）: ${invalid}`)
      const reg = await readRegistryFromDisk()
      if (list.length) reg.remote[key] = list
      else delete reg.remote[key]
      await writeRegistryToDisk(reg)
      return reg
    })
  }

  /** 单仓一次取数：status / 最近提交 / remote 配置三条 execFile 并行 */
  private async statusOne(path: string): Promise<LocalRepoStatusResult> {
    const [st, lg, og] = await Promise.all([
      execGit(['-C', path, '-c', 'core.quotePath=false', 'status', '--porcelain=v1', '-b', '--ignore-submodules=dirty']),
      execGit(['-C', path, 'log', '-1', `--format=${GIT_LOG_FORMAT}`]),
      // get-regexp 取全部远端：远端不叫 origin（gitee / upstream / 自定义名）同样能展示
      execGit(['-C', path, 'config', '--get-regexp', '^remote\\..*\\.url$']),
    ])
    if (st.code !== 0) {
      return {
        path,
        ok: false,
        error: st.code === -1 ? '未检测到 git（请安装 git 或加入 PATH）' : (firstLine(st.stderr) || `git status 退出码 ${st.code}`),
      }
    }
    const status = parseGitStatusPorcelain(st.stdout)
    const pick = pickRemote(parseRemoteConfig(og.stdout), status.upstream)
    return {
      path,
      ok: true,
      status,
      lastCommit: lg.code === 0 ? (parseGitLog(lg.stdout)[0] ?? null) : null,
      originUrl: pick?.url ?? null,
      remoteName: pick?.name ?? null,
    }
  }

  /** 逐仓取数（并发上限分批）；单仓失败不拖垮整批，按行上浮；非法路径条目直接丢弃 */
  async statusAll(paths: string[]): Promise<LocalRepoStatusResult[]> {
    const list = [...new Set(Array.isArray(paths) ? paths.filter(p => isAbsolutePath(String(p ?? ''))) : [])]
    const results: LocalRepoStatusResult[] = []
    for (let i = 0; i < list.length; i += STATUS_CONCURRENCY) {
      const batch = await Promise.all(list.slice(i, i + STATUS_CONCURRENCY).map(p => this.statusOne(p)))
      results.push(...batch)
    }
    return results
  }

  async fetchOne(path: string): Promise<GitRawResult> {
    return execGit(['-C', assertRepoPath(path), 'fetch', '--all', '--prune'], FETCH_TIMEOUT_MS)
  }

  async pullOne(path: string): Promise<GitRawResult> {
    return execGit(['-C', assertRepoPath(path), 'pull', '--ff-only'], PULL_TIMEOUT_MS)
  }

  /** 切换分支：git 自身的安全边界（未提交变更冲突时拒绝）就是这里的安全策略，不 stash 不丢弃 */
  async switchBranch(path: string, branch: string): Promise<GitRawResult> {
    const repoPath = assertRepoPath(path)
    const name = String(branch ?? '').trim()
    if (!isValidBranchName(name)) throw new Error(`非法分支名: ${name || '(空)'}`)
    return execGit(['-C', repoPath, 'switch', name], ACTION_TIMEOUT_MS)
  }

  /** 提交所选文件：add（覆盖未跟踪）→ commit -- pathspec（只提交所选，忽略其余已暂存） */
  async commitOne(path: string, files: string[], msg: string): Promise<GitRawResult> {
    const repoPath = assertRepoPath(path)
    if (!isValidCommitFiles(files)) throw new Error('未选择要提交的文件，或文件路径非法')
    if (!isValidCommitMsg(msg)) throw new Error('提交信息为空、超长（>200 字符）或含控制字符')
    const paths = files.map(f => f.trim())
    const added = await execGit(['-C', repoPath, 'add', '--', ...paths], ACTION_TIMEOUT_MS)
    if (added.code !== 0) return added
    return execGit(['-C', repoPath, 'commit', '-m', msg.trim(), '--', ...paths], ACTION_TIMEOUT_MS)
  }

  /** 选中文件（pathspec）对 HEAD 的 diff：供提交信息 AI 归纳（只读，-U2 收敛上下文，
   *  pathspec 校验与 commitOne 同口径）。空仓库无 HEAD → 非零退出，调用侧退回清单推断 */
  async diffFiles(path: string, files: string[]): Promise<GitRawResult> {
    const repoPath = assertRepoPath(path)
    if (!isValidCommitFiles(files)) throw new Error('未选择文件，或文件路径非法')
    return execGit(
      ['-C', repoPath, '-c', 'core.quotePath=false', 'diff', 'HEAD', '-U2', '--', ...files.map(f => f.trim())],
      ACTION_TIMEOUT_MS,
    )
  }

  /** 推送：有上游直接 push；无上游按 branch.<name>.remote 或展示远端用 -u 建立跟踪 */
  async pushOne(path: string): Promise<GitRawResult> {
    const repoPath = assertRepoPath(path)
    const head = await execGit(['-C', repoPath, 'rev-parse', '--abbrev-ref', 'HEAD'])
    const branch = head.stdout.trim()
    if (!branch || branch === 'HEAD') {
      return { code: 1, stdout: '', stderr: 'HEAD 游离（detached HEAD），没有可推送的分支' }
    }
    const upstream = await execGit(['-C', repoPath, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
    if (upstream.code === 0 && upstream.stdout.trim()) {
      return execGit(['-C', repoPath, 'push'], PUSH_TIMEOUT_MS)
    }
    const cfgRemote = await execGit(['-C', repoPath, 'config', '--get', `branch.${branch}.remote`])
    const remotes = await execGit(['-C', repoPath, 'config', '--get-regexp', '^remote\\..*\\.url$'])
    const pick = cfgRemote.code === 0 && cfgRemote.stdout.trim()
      ? { name: cfgRemote.stdout.trim() }
      : pickRemote(parseRemoteConfig(remotes.stdout), null)
    if (!pick?.name) {
      return { code: 1, stdout: '', stderr: '没有配置远程仓库，无法推送' }
    }
    return execGit(['-C', repoPath, 'push', '-u', pick.name, branch], PUSH_TIMEOUT_MS)
  }

  /** 详情三段原始输出（解析在渲染层 parseRepoDetailSections，单测共用同一份） */
  async detail(path: string): Promise<{ ok: boolean; error?: string; statusRaw?: string; branchRaw?: string; logRaw?: string }> {
    const repoPath = assertRepoPath(path)
    const [st, br, lg] = await Promise.all([
      execGit(['-C', repoPath, '-c', 'core.quotePath=false', 'status', '--porcelain=v1', '--ignore-submodules=dirty']),
      execGit(['-C', repoPath, 'branch', '-vv']),
      execGit(['-C', repoPath, 'log', '-20', `--format=${GIT_LOG_FORMAT}`]),
    ])
    if (st.code !== 0) {
      return { ok: false, error: firstLine(st.stderr) || '取数失败' }
    }
    return { ok: true, statusRaw: st.stdout, branchRaw: br.stdout, logRaw: lg.stdout }
  }

  /** 提交图谱原始输出（解析在渲染层 parseGraphLog，单测共用同一份）；limit 上限在 core 收口 */
  async graphLog(path: string, limit: number): Promise<GitRawResult> {
    return execGit(['-C', assertRepoPath(path), ...buildGraphLogArgs(limit)])
  }
}
