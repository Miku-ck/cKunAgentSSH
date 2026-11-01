/**
 * tar 形态自动更新的纯逻辑（无 IO、无 electron 依赖）。
 *
 * 为什么自己写而不是 electron-updater：本仓 2026-10-23 起发布形态只有
 * `dir` + `tar.gz`（解包直发，无安装器），而 electron-updater 的 Windows 路径
 * 绑死 NSIS/Squirrel。详见 docs/superpowers/specs/2026-10-30-tar-form-auto-update-design.md。
 *
 * 这一层只回答三件事，全部可单测：
 *   1. 清单里有没有比当前更新的、且是本平台本架构的产物？  selectUpdate()
 *   2. 换包时文件从哪搬到哪、失败怎么退回？               stagingPaths() / buildApplierScript()
 *   3. 状态机允许哪些迁移（避免"error 之后卡死不出新状态"）？ canTransition()
 */

export interface UpdateRelease {
  platform: string
  arch: string
  url: string
  sha256: string
  size?: number
}

export interface UpdateManifest {
  version: string
  notes?: string
  releases: UpdateRelease[]
}

export interface UpdateTarget {
  platform: string
  arch: string
}

/** 落盘后的待装更新。重启时靠它决定"要不要立刻换包"。 */
export interface PendingUpdate {
  version: string
  appDir: string
  stagingDir: string
  backupDir: string
  exeName: string
  pid: number
  preparedAt: string
}

export type UpdatePhase =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'ready'
  | 'applying'
  | 'error'

export interface UpdateState {
  phase: UpdatePhase
  /** 当前运行版本（app.getVersion()），UI 用它和 targetVersion 并排显示 */
  currentVersion: string
  /** 清单里挑出来的目标版本；没有可用更新时为 null */
  targetVersion: string | null
  notes: string | null
  /** 0-100，仅 downloading 阶段有意义 */
  percent: number
  error: string | null
  /** 清单来源；空串表示"未配置，不检查" */
  feedUrl: string
  /**
   * 当前运行形态能不能自助换包。
   * 开发态跑的是 node_modules/electron/dist/electron.exe，换包等于把开发环境拆了 ——
   * UI 靠这一位把按钮禁用并说明原因，而不是让用户点完看不出所以然。
   */
  supported: boolean
}

/**
 * 允许的状态迁移。
 *
 * `error` 必须能回到 `checking`/`idle`：否则用户点一次"重试"就没有合法出口，
 * 只能重启应用 —— 这是自动更新最常见的一类死锁。
 * `applying` 只出不进（写完标记就要退出应用），故只有一条出口。
 */
const TRANSITIONS: Record<UpdatePhase, UpdatePhase[]> = {
  idle: ['checking', 'error'],
  checking: ['available', 'idle', 'error'],
  available: ['downloading', 'checking', 'error'],
  downloading: ['ready', 'error'],
  ready: ['applying', 'checking', 'error'],
  applying: ['ready'],
  error: ['idle', 'checking', 'downloading', 'error'],
}

export function canTransition(from: UpdatePhase, to: UpdatePhase): boolean {
  return TRANSITIONS[from].includes(to)
}

function parseVersion(v: string): { nums: number[]; pre: string | null } | null {
  const m = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(v.trim())
  if (!m) return null
  return { nums: [Number(m[1]), Number(m[2]), Number(m[3])], pre: m[4] ?? null }
}

/**
 * 语义化版本比较：返回 -1 / 0 / 1。
 *
 * 无法解析的版本（如 `0.1`、`1.0.0.0`）按"比任何合法版本都旧"处理 ——
 * 发布清单里出现畸形版本号时应当是"不更新"，而不是抛异常让整个检查失败。
 */
export function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a)
  const pb = parseVersion(b)
  if (!pa && !pb) return 0
  if (!pa) return -1
  if (!pb) return 1
  for (let i = 0; i < 3; i++) {
    if (pa.nums[i] !== pb.nums[i]) return pa.nums[i] > pb.nums[i] ? 1 : -1
  }
  // 有预发布标签的版本低于同号正式版（1.0.0-beta < 1.0.0）
  if (pa.pre === pb.pre) return 0
  if (pa.pre === null) return 1
  if (pb.pre === null) return -1
  return pa.pre > pb.pre ? 1 : -1
}

export function isNewerVersion(candidate: string, current: string): boolean {
  return compareVersions(candidate, current) > 0
}

/** 只认 64 位十六进制；大小写归一。哈希是本形态唯一的完整性凭据，格式不对即拒绝。 */
export function normalizeSha256(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim().toLowerCase()
  return /^[0-9a-f]{64}$/.test(s) ? s : null
}

/** 回环判定：localhost / 127.0.0.0/8 / ::1。IPv6 在 URL.hostname 里带方括号，先剥。 */
function isLoopbackHost(hostname: string): boolean {
  const h = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (h === 'localhost' || h === '::1') return true
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)
}

/**
 * 更新链路的地址是否可信：只认 https，明文 http 仅在本机回环放行。
 *
 * 为什么这条必须比「是 http(s)」严：清单本身没有签名，走明文等于把「版本号 +
 * 产物地址 + 期望 sha256」三件事一起交给链路上的任何人改 —— 下载端那记
 * sha256 校验挡不住它，因为期望值和包是同一个人给的。回环放行的理由很具体：
 * 设计里写死的无网络依赖验收路径就是本地 `python -m http.server` 托管一份假清单。
 * 局域网段不算回环 —— 同一条链路上的任何人都能改包，那正是这条路要挡的。
 */
export function isTrustedUpdateUrl(raw: unknown): raw is string {
  if (typeof raw !== 'string' || !raw) return false
  let u: URL
  try {
    u = new URL(raw)
  } catch {
    return false
  }
  if (u.protocol === 'https:') return true
  return u.protocol === 'http:' && isLoopbackHost(u.hostname)
}

/**
 * 从清单里挑出本次可用的更新。
 *
 * 返回 null 的六种情况都必须"看起来一样"（都是不更新），但成因不同：
 * 清单畸形、版本不更新、没有本平台产物、产物 URL 不可信、缺 sha256、平台同名但架构不符。
 * 只有"真的可以更新"才返回 release，调用方不需要再二次校验。
 */
export function selectUpdate(
  manifest: unknown,
  currentVersion: string,
  target: UpdateTarget,
): { version: string; notes: string | null; release: UpdateRelease } | null {
  if (!manifest || typeof manifest !== 'object') return null
  const m = manifest as Partial<UpdateManifest>
  if (typeof m.version !== 'string' || !parseVersion(m.version)) return null
  if (!isNewerVersion(m.version, currentVersion)) return null
  if (!Array.isArray(m.releases)) return null

  for (const raw of m.releases) {
    if (!raw || typeof raw !== 'object') continue
    const r = raw as Partial<UpdateRelease>
    if (r.platform !== target.platform || r.arch !== target.arch) continue
    const sha256 = normalizeSha256(r.sha256)
    if (!sha256 || !isTrustedUpdateUrl(r.url)) continue
    return {
      version: m.version.trim().replace(/^v/, ''),
      notes: typeof m.notes === 'string' ? m.notes : null,
      release: {
        platform: r.platform,
        arch: r.arch,
        url: r.url,
        sha256,
        size: typeof r.size === 'number' && r.size > 0 ? r.size : undefined,
      },
    }
  }
  return null
}

/**
 * 换包用到的三个路径。
 *
 * ⚠️ staging 必须是 appDir 的**兄弟目录**，不能放在 appDir 里面：
 * 换包第一步是把整个 appDir 改名成 backup，若 staging 在里面，它会被
 * 一起搬走，第二步"把 staging 提到 appDir"就会扑空。
 * 同盘（同一个父目录）同时保证 rename 不跨卷 —— 跨卷 rename 在 Windows 上
 * 直接抛错，而跨卷只能用 copy+delete，那就失去了原子性。
 */
export function stagingPaths(appDir: string, version: string): { stagingDir: string; backupDir: string } {
  // 统一成正斜杠：调用方可能给 `D:\apps\app` 也可能给尾带分隔符的路径，
  // 而 Linux/macOS 的 CI 上必须得到同一个形状（Path 分隔符差异会让断言与服务端日志对不上）。
  const normalized = appDir.replace(/\\/g, '/').replace(/\/+$/, '')
  const cut = normalized.lastIndexOf('/')
  const parent = cut === -1 ? '.' : normalized.slice(0, cut)
  const base = cut === -1 ? normalized : normalized.slice(cut + 1)
  return {
    stagingDir: `${parent}/.ckun-update-${version}`,
    backupDir: `${parent}/.${base}-backup`,
  }
}

function psQuote(s: string): string {
  // PowerShell 单引号字符串里，单引号用两个单引号转义
  return `'${s.replace(/'/g, "''")}'`
}

/**
 * 生成"退出后换包"的 PowerShell 助手脚本。
 *
 * 为什么是独立进程 + 等 PID 消失：Windows 上运行中的 exe 不可替换、app.asar 被
 * Electron 持有句柄，所以换包只能在**本进程真的退出之后**由别人做。助手由主进程在
 * before-quit 里 detached 启动，宿主一退它就接手。
 *
 * 失败语义（顺序即安全性）：
 *   - 备份改名失败 → 什么都没动，退出（旧版本完好）
 *   - 提升 staging 失败 → 把备份改回原名，退出（回滚到旧版本）
 *   - 启动新版本失败 → 备份已删也没关系，用户重解压 tar.gz 即可（数据在 userData）
 */
export function buildApplierScript(pending: PendingUpdate, logFile: string): string {
  const exePath = `${pending.appDir}\\${pending.exeName}`
  return [
    `# 由 cKunAgentSSH 主进程生成：等宿主退出 → 换包 → 拉起新版本。不要手工编辑。`,
    `$ErrorActionPreference = 'Stop'`,
    `$log = ${psQuote(logFile)}`,
    `function Write-Log([string]$m) {`,
    `  try { ('{0:o} {1}' -f (Get-Date), $m) | Out-File -LiteralPath $log -Append -Encoding utf8 } catch { }`,
    `}`,
    `Write-Log ${psQuote(`applier start pid=${pending.pid} version=${pending.version}`)}`,
    `# 等宿主进程退出；最多等 120s，超时也继续（用户可能已强杀，句柄已释放）`,
    `try { $p = Get-Process -Id ${pending.pid} -ErrorAction Stop; [void]$p.WaitForExit(120000) } catch { }`,
    `Write-Log 'host exited'`,
    `try {`,
    `  if (Test-Path -LiteralPath ${psQuote(pending.backupDir)}) { Remove-Item -LiteralPath ${psQuote(pending.backupDir)} -Recurse -Force }`,
    `  Move-Item -LiteralPath ${psQuote(pending.appDir)} -Destination ${psQuote(pending.backupDir)} -ErrorAction Stop`,
    `} catch {`,
    `  Write-Log ('backup rename failed: ' + $_.Exception.Message)`,
    `  exit 1`,
    `}`,
    `try {`,
    `  Move-Item -LiteralPath ${psQuote(pending.stagingDir)} -Destination ${psQuote(pending.appDir)} -ErrorAction Stop`,
    `} catch {`,
    `  Write-Log ('promote failed: ' + $_.Exception.Message)`,
    `  try { Move-Item -LiteralPath ${psQuote(pending.backupDir)} -Destination ${psQuote(pending.appDir)} -ErrorAction Stop } catch { Write-Log ('rollback failed: ' + $_.Exception.Message) }`,
    `  exit 1`,
    `}`,
    `Write-Log 'swap done'`,
    `try { Start-Process -FilePath ${psQuote(exePath)} -WorkingDirectory ${psQuote(pending.appDir)} } catch { Write-Log ('relaunch failed: ' + $_.Exception.Message) }`,
    // 备份删除是收尾工作，可能被杀软/索引服务短暂锁住（实测偶发）。重试三次，
    // 仍失败也不回滚 —— 新版已经就位，备份留到下次启动也不会覆盖它。
    `for ($i = 0; $i -lt 3; $i++) {`,
    `  try { Remove-Item -LiteralPath ${psQuote(pending.backupDir)} -Recurse -Force -ErrorAction Stop; break }`,
    `  catch { Start-Sleep -Milliseconds 500 }`,
    `}`,
    // 必须再写一条终态日志：否则外部观察者（支持人员看 applier.log、测试断言）
    // 在 'swap done' 之后仍然不知道备份是否清完。
    `Write-Log 'cleanup done'`,
    `exit 0`,
    ``,
  ].join('\r\n')
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

export function progressPercent(done: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0
  const p = Math.round((done / total) * 100)
  return p < 0 ? 0 : p > 100 ? 100 : p
}

export function initialState(currentVersion: string, feedUrl: string, supported = true): UpdateState {
  return {
    phase: 'idle',
    currentVersion,
    targetVersion: null,
    notes: null,
    percent: 0,
    error: null,
    feedUrl,
    supported,
  }
}
