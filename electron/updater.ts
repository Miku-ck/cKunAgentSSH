/**
 * 主进程侧的自动更新执行器（tar 形态）。
 *
 * 职责边界：这里只做 IO 与编排，所有判定（版本比较、产物筛选、路径、助手脚本）
 * 都在 shared/update-core.ts 里，那边可单测、这边靠注入点测。
 *
 * 三条硬约束（都是踩过 Windows 的坑才有的）：
 *   1. 任何失败都不许阻塞启动与使用 —— 更新是"有空再说"的功能，不能变成新的启动阻塞点。
 *   2. 换包只能发生在进程真的退出之后（运行中的 exe 与 app.asar 都不可替换），
 *      所以 apply() 只写标记、落助手脚本，真正的搬目录由 detached 助手做。
 *   3. 状态机必须能从 error 回到 checking，否则用户点一次"重试"就没有合法出口。
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { get as httpGet } from 'node:http'
import { get as httpsGet } from 'node:https'
import { basename, join } from 'node:path'
import {
  buildApplierScript,
  canTransition,
  initialState,
  isHttpUrl,
  progressPercent,
  selectUpdate,
  stagingPaths,
  type PendingUpdate,
  type UpdateManifest,
  type UpdateState,
} from '../shared/update-core'
import { resolveTarExecutable } from './utils/tar-exe'

export interface UpdaterOptions {
  /** 应用所在目录（打包后 = win-unpacked，开发时 = 仓库根之外的其他目录，不做更新） */
  appDir: string
  /** userData：更新缓存与标记都放这里，换包不碰它 */
  userDataDir: string
  currentVersion: string
  exeName: string
  platform: string
  arch: string
  /** 清单地址。空串 = 未配置（开发/离线），check() 直接返回 idle */
  feedUrl: string
  log?: (level: 'info' | 'warn' | 'error', message: string) => void
  /** 注入点：单测替换网络/解压/进程启动，避免真实 IO */
  fetchJson?: (url: string) => Promise<unknown>
  downloadFile?: (url: string, dest: string, onProgress: (done: number, total: number) => void) => Promise<string>
  extractTar?: (archive: string, destDir: string) => Promise<void>
  spawnApplier?: (scriptPath: string) => void
}

const LIST_TIMEOUT_MS = 10_000

export class Updater {
  private state: UpdateState
  private readonly listeners = new Set<(s: UpdateState) => void>()
  private readonly opts: UpdaterOptions
  private manifest: UpdateManifest | null = null
  private archivePath: string | null = null

  constructor(opts: UpdaterOptions) {
    this.opts = opts
    // appDir 为空 = 开发态（跑的是 electron.exe，不是分发目录）：不参与自助换包
    this.state = initialState(opts.currentVersion, opts.feedUrl, opts.appDir !== '')
  }

  get updateDir(): string {
    return join(this.opts.userDataDir, 'update')
  }

  get pendingFile(): string {
    return join(this.updateDir, 'pending.json')
  }

  /** 设置页覆盖的清单地址落盘处（优先于 CKUN_UPDATE_FEED） */
  get configFile(): string {
    return join(this.updateDir, 'config.json')
  }

  getState(): UpdateState {
    return { ...this.state }
  }

  onState(listener: (s: UpdateState) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private log(level: 'info' | 'warn' | 'error', message: string): void {
    this.opts.log?.(level, message)
  }

  /**
   * 状态迁移。非法迁移只记日志不抛：宁可多推一次状态让 UI 有反应，
   * 也不要因为一次顺序错乱把整个更新通道卡死（用户看到的就是"点了没反应"）。
   */
  private setState(patch: Partial<UpdateState>): void {
    const next: UpdateState = { ...this.state, ...patch }
    if (next.phase !== this.state.phase && !canTransition(this.state.phase, next.phase)) {
      this.log('warn', `update 状态迁移异常：${this.state.phase} → ${next.phase}（继续）`)
    }
    this.state = next
    for (const l of this.listeners) {
      try {
        l(this.getState())
      } catch (e) {
        this.log('warn', `update 状态回调抛错：${(e as Error).message}`)
      }
    }
  }

  /** 拉清单 → 挑产物。失败静默回 idle（离线不该在界面上留一条红字）。 */
  async check(): Promise<UpdateState> {
    if (!this.opts.feedUrl || !this.opts.appDir) {
      this.setState({ phase: 'idle', error: null, targetVersion: null })
      return this.getState()
    }
    this.setState({ phase: 'checking', error: null })
    try {
      const fetchJson = this.opts.fetchJson ?? this.defaultFetchJson.bind(this)
      const manifest = (await fetchJson(this.opts.feedUrl)) as UpdateManifest
      const picked = selectUpdate(manifest, this.opts.currentVersion, {
        platform: this.opts.platform,
        arch: this.opts.arch,
      })
      if (!picked) {
        this.manifest = null
        this.setState({ phase: 'idle', targetVersion: null, notes: null, error: null })
        return this.getState()
      }
      this.manifest = manifest
      this.setState({
        phase: 'available',
        targetVersion: picked.version,
        notes: picked.notes,
        error: null,
      })
      this.log('info', `发现新版本 ${picked.version}（当前 ${this.opts.currentVersion}）`)
    } catch (e) {
      // 网络不通 / 清单畸形：静默。下次启动或用户手点重试即可。
      this.setState({ phase: 'idle', error: null, targetVersion: null })
      this.log('warn', `检查更新失败（忽略）：${(e as Error).message}`)
    }
    return this.getState()
  }

  /** 下载 + 校验 + 解压到 staging。任一步失败都回到 error，且旧版本一个字节没动。 */
  async download(): Promise<UpdateState> {
    if (!this.opts.appDir) {
      this.setState({ phase: 'error', error: '开发运行不执行换包（当前目录不是分发产物）' })
      return this.getState()
    }
    const picked = this.pickedRelease()
    if (!picked) {
      this.setState({ phase: 'error', error: '没有可下载的新版本' })
      return this.getState()
    }
    mkdirSync(this.updateDir, { recursive: true })
    const archive = join(this.updateDir, `${picked.version}.tar.gz`)
    this.setState({ phase: 'downloading', percent: 0, error: null })
    try {
      // 先探可写再看网络：装到 Program Files 的副本换包必失败，
      // 让用户在下 171MB 之前就知道要改成"管理员权限手动解压"。
      this.assertAppDirWritable()
      const downloadFile = this.opts.downloadFile ?? this.defaultDownloadFile.bind(this)
      const sha = await downloadFile(picked.release.url, archive, (done, total) => {
        this.setState({ percent: progressPercent(done, total) })
      })
      if (sha.toLowerCase() !== picked.release.sha256) {
        rmSync(archive, { force: true })
        throw new Error(`sha256 不匹配（期望 ${picked.release.sha256.slice(0, 12)}…，实得 ${sha.slice(0, 12)}…）`)
      }
      const { stagingDir } = stagingPaths(this.opts.appDir, picked.version)
      if (existsSync(stagingDir)) rmSync(stagingDir, { recursive: true, force: true })
      mkdirSync(stagingDir, { recursive: true })
      const extractTar = this.opts.extractTar ?? this.defaultExtractTar.bind(this)
      await extractTar(archive, stagingDir)
      this.archivePath = archive
      this.setState({ phase: 'ready', percent: 100, error: null })
      this.log('info', `新版本 ${picked.version} 已就绪：${stagingDir}`)
    } catch (e) {
      // staging 半成品必须清掉：下次重试时"目录已存在"会让解压行为不确定
      const p = stagingPaths(this.opts.appDir, picked.version)
      rmSync(p.stagingDir, { recursive: true, force: true })
      this.setState({ phase: 'error', error: (e as Error).message })
      this.log('error', `下载/解压失败：${(e as Error).message}`)
    }
    return this.getState()
  }

  /**
   * 设置页覆盖清单地址。空串 = 清空（回到"不检查"）。
   *
   * 只收 http(s)：file:// 与裸路径会把"更新"变成读本地目录，那是另一条链路，
   * 也让清单来源失去可审计性。落盘失败不影响本次生效，只记一条 warn。
   */
  setFeed(raw: string): UpdateState {
    const url = String(raw ?? '').trim()
    if (url && !isHttpUrl(url)) {
      this.setState({ phase: 'error', error: '清单地址必须是 http(s) 链接' })
      return this.getState()
    }
    this.opts.feedUrl = url
    this.manifest = null
    try {
      mkdirSync(this.updateDir, { recursive: true })
      writeFileSync(this.configFile, JSON.stringify({ feedUrl: url }, null, 2), 'utf8')
    } catch (e) {
      this.log('warn', `清单地址落盘失败（本次仍生效）：${(e as Error).message}`)
    }
    this.setState({ phase: 'idle', feedUrl: url, targetVersion: null, notes: null, percent: 0, error: null })
    return this.getState()
  }

  /**
   * 启动时读设置页覆盖值。**必须早于 onState 订阅**：这里直接改内部状态，
   * 不推通知 —— 那时还没有监听者，推了也只是空转。
   */
  loadConfig(): void {
    if (!existsSync(this.configFile)) return
    try {
      const raw = JSON.parse(readFileSync(this.configFile, 'utf8')) as { feedUrl?: unknown }
      if (typeof raw.feedUrl !== 'string') return
      const url = raw.feedUrl.trim()
      if (url && !isHttpUrl(url)) {
        this.log('warn', 'update/config.json 里的清单地址非法，忽略')
        return
      }
      this.opts.feedUrl = url
      this.state = { ...this.state, feedUrl: url }
    } catch (e) {
      this.log('warn', `update/config.json 损坏，忽略：${(e as Error).message}`)
    }
  }

  /** 装到受保护目录（Program Files 等）时，换包第一步改名就会失败。 */
  private assertAppDirWritable(): void {
    const probe = join(this.opts.appDir, '.ckun-update-probe')
    try {
      writeFileSync(probe, '1')
      rmSync(probe, { force: true })
    } catch {
      throw new Error('当前安装目录不可写（可能装在 Program Files）；请用管理员权限手动解压新版本')
    }
  }

  /**
   * 落标记 + 拉起助手。调用方随后必须真的退出应用（助手在等 PID 消失）。
   * 注意这里不删 archive：解压完就不再需要，但留着能让"助手失败后手工重试"少下 171MB。
   */
  apply(pid: number): boolean {
    const picked = this.pickedRelease()
    if (!picked || this.state.phase !== 'ready') {
      this.log('warn', `update 未就绪，拒绝应用（phase=${this.state.phase}）`)
      return false
    }
    const { stagingDir, backupDir } = stagingPaths(this.opts.appDir, picked.version)
    const pending: PendingUpdate = {
      version: picked.version,
      appDir: this.opts.appDir,
      stagingDir,
      backupDir,
      exeName: this.opts.exeName,
      pid,
      preparedAt: new Date().toISOString(),
    }
    mkdirSync(this.updateDir, { recursive: true })
    const scriptPath = join(this.updateDir, 'applier.ps1')
    const script = buildApplierScript(pending, join(this.updateDir, 'applier.log'))
    try {
      writeFileSync(this.pendingFile, JSON.stringify(pending, null, 2), 'utf8')
      // PowerShell 脚本带中文注释：写 UTF-8 BOM，否则 Windows PowerShell 5.1 会按 ANSI 解析
      writeFileSync(scriptPath, `\uFEFF${script}`, 'utf8')
      const spawnApplier = this.opts.spawnApplier ?? this.defaultSpawnApplier.bind(this)
      spawnApplier(scriptPath)
      this.setState({ phase: 'applying', error: null })
      this.log('info', `换包助手已启动（版本 ${picked.version}），退出后自动重启`)
      return true
    } catch (e) {
      rmSync(this.pendingFile, { force: true })
      this.setState({ phase: 'error', error: (e as Error).message })
      this.log('error', `拉起换包助手失败：${(e as Error).message}`)
      return false
    }
  }

  /**
   * 启动时调用：处理上次留下的标记。
   *
   * 三种可能，处理完全不同：
   *   - 标记版本 == 当前版本 → 助手换包成功（新版本的 exe 正在跑），标记作废；
   *   - 标记版本 != 当前版本 → 助手没换成（进程没退干净 / 改名失败），
   *     staging 已经没有意义，清掉以免下次误判为"已就绪"；
   *   - 标记文件损坏 → 当作没有，直接删。
   */
  consumePending(): { applied: boolean; version: string | null } {
    const pending = this.readPending()
    if (!pending) return { applied: false, version: null }
    rmSync(this.pendingFile, { force: true })
    if (pending.version === this.opts.currentVersion) {
      this.log('info', `更新已完成，当前版本 ${this.opts.currentVersion}`)
      this.cleanupStale()
      return { applied: true, version: pending.version }
    }
    rmSync(pending.stagingDir, { recursive: true, force: true })
    this.log('warn', `上次换包未完成（标记 ${pending.version} / 实际 ${this.opts.currentVersion}），已清理 staging`)
    return { applied: false, version: pending.version }
  }

  readPending(): PendingUpdate | null {
    if (!existsSync(this.pendingFile)) return null
    try {
      const raw = JSON.parse(readFileSync(this.pendingFile, 'utf8')) as Partial<PendingUpdate>
      if (
        typeof raw.version !== 'string' ||
        typeof raw.appDir !== 'string' ||
        typeof raw.stagingDir !== 'string' ||
        typeof raw.backupDir !== 'string' ||
        typeof raw.exeName !== 'string'
      ) {
        this.log('warn', 'pending.json 字段不全，丢弃')
        return null
      }
      return {
        version: raw.version,
        appDir: raw.appDir,
        stagingDir: raw.stagingDir,
        backupDir: raw.backupDir,
        exeName: raw.exeName,
        pid: typeof raw.pid === 'number' ? raw.pid : 0,
        preparedAt: typeof raw.preparedAt === 'string' ? raw.preparedAt : '',
      }
    } catch (e) {
      this.log('warn', `pending.json 损坏，丢弃：${(e as Error).message}`)
      return null
    }
  }

  /** 清掉不属于当前版本的 staging 与安装包残留（助手失败留下的现场）。 */
  cleanupStale(): void {
    try {
      const parent = join(this.opts.appDir, '..')
      for (const name of readdirSync(parent)) {
        if (!name.startsWith('.ckun-update-')) continue
        if (name === `.ckun-update-${this.opts.currentVersion}`) continue
        rmSync(join(parent, name), { recursive: true, force: true })
        this.log('info', `清理残留 staging：${name}`)
      }
      if (existsSync(this.updateDir)) {
        for (const name of readdirSync(this.updateDir)) {
          if (!name.endsWith('.tar.gz')) continue
          if (this.archivePath && name === basename(this.archivePath)) continue
          rmSync(join(this.updateDir, name), { force: true })
        }
      }
    } catch (e) {
      this.log('warn', `清理残留失败（忽略）：${(e as Error).message}`)
    }
  }

  private pickedRelease() {
    if (!this.manifest) return null
    return selectUpdate(this.manifest, this.opts.currentVersion, {
      platform: this.opts.platform,
      arch: this.opts.arch,
    })
  }

  private defaultFetchJson(url: string): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const getter = url.startsWith('https:') ? httpsGet : httpGet
      const req = getter(url, { timeout: LIST_TIMEOUT_MS, headers: { 'User-Agent': 'cKunAgentSSH-updater' } }, (res) => {
        // 重定向：清单常挂在静态托管后面（307/302）
        const status = res.statusCode ?? 0
        if (status >= 300 && status < 400 && res.headers.location) {
          res.resume()
          this.defaultFetchJson(new URL(res.headers.location, url).toString()).then(resolve, reject)
          return
        }
        if (status !== 200) {
          res.resume()
          reject(new Error(`HTTP ${status}`))
          return
        }
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (c: string) => {
          body += c
          // 清单只有几 KB；超过 1MB 说明地址指向的不是清单
          if (body.length > 1024 * 1024) {
            req.destroy(new Error('清单体积异常'))
          }
        })
        res.on('end', () => {
          try {
            resolve(JSON.parse(body))
          } catch (e) {
            reject(e as Error)
          }
        })
        res.on('error', reject)
      })
      req.on('timeout', () => req.destroy(new Error('清单请求超时')))
      req.on('error', reject)
    })
  }

  private defaultDownloadFile(
    url: string,
    dest: string,
    onProgress: (done: number, total: number) => void,
  ): Promise<string> {
    return new Promise((resolve, reject) => {
      const hash = createHash('sha256')
      const out = createWriteStream(dest)
      let done = 0
      let settled = false
      const fail = (e: Error) => {
        if (settled) return
        settled = true
        out.destroy()
        rmSync(dest, { force: true })
        reject(e)
      }
      const getter = url.startsWith('https:') ? httpsGet : httpGet
      const req = getter(url, { headers: { 'User-Agent': 'cKunAgentSSH-updater' } }, (res) => {
        const status = res.statusCode ?? 0
        if (status >= 300 && status < 400 && res.headers.location) {
          res.resume()
          out.destroy()
          rmSync(dest, { force: true })
          this.defaultDownloadFile(new URL(res.headers.location, url).toString(), dest, onProgress).then(resolve, reject)
          return
        }
        if (status !== 200) {
          res.resume()
          fail(new Error(`HTTP ${status}`))
          return
        }
        const total = Number(res.headers['content-length'] ?? 0)
        res.on('data', (chunk: Buffer) => {
          done += chunk.length
          hash.update(chunk)
          onProgress(done, total)
        })
        res.pipe(out)
        out.on('finish', () => {
          if (settled) return
          settled = true
          resolve(hash.digest('hex'))
        })
        res.on('error', fail)
        out.on('error', fail)
      })
      req.on('error', fail)
    })
  }

  private defaultExtractTar(archive: string, destDir: string): Promise<void> {
    // 复用打包链路的 bsdtar 假设（Windows 10+ 自带 C:\Windows\System32\tar.exe）
    return new Promise((resolve, reject) => {
      const child = spawn(resolveTarExecutable(), ['-xzf', archive, '-C', destDir], {
        stdio: 'ignore',
        windowsHide: true,
      })
      child.on('error', reject)
      child.on('exit', (code) => {
        if (code === 0) resolve()
        else reject(new Error(`tar 退出码 ${code}`))
      })
    })
  }

  /**
   * 拉起 detached 换包助手。
   *
   * ⚠️ 不能直接 `spawn(powershell.exe, ……, { detached: true })`：Windows 的 detached
   * 对应 DETACHED_PROCESS（无控制台），而 powershell.exe / cmd.exe 这类控制台宿主
   * 拿不到控制台会**立即 exit 0 且什么都不做** —— 实测五种组合全静默（node 子进程不受影响）。
   * 所以用 `cmd /c start` 另开一个控制台托管 PowerShell：cmd 在 50ms 内退出，
   * PowerShell 被孤儿化，宿主进程死后它照常跑完。
   */
  private defaultSpawnApplier(scriptPath: string): void {
    if (process.platform === 'win32') {
      // 正斜杠：Node 的 path.join 在 Windows 上照单全收，也避免源码里满屏双反斜杠
      const sysRoot = process.env['SystemRoot'] || 'C:/Windows'
      const ps = join(sysRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
      const cmd = join(sysRoot, 'System32', 'cmd.exe')
      const child = spawn(cmd, ['/c', 'start', '""', '/min', ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', scriptPath], {
        stdio: 'ignore',
        windowsHide: true,
      })
      child.unref()
      return
    }
    const child = spawn('pwsh', ['-NoProfile', '-File', scriptPath], {
      detached: true,
      stdio: 'ignore',
    })
    // 不 unref 的话主进程退出会被这个子进程拖住；助手本来就是要在宿主死后继续活的
    child.unref()
  }
}
