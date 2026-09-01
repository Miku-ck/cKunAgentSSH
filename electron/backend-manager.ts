import { spawn, ChildProcess, execSync } from 'child_process'
import { join } from 'path'
import { app } from 'electron'
import http from 'http'
import { mkdirSync, existsSync, renameSync, copyFileSync } from 'fs'
import { homedir } from 'os'
import { randomBytes } from 'crypto'

/**
 * Python FastAPI 后端进程管理器
 * 负责 spawn、健康检查、优雅关闭、崩溃自动重启
 */
export class BackendManager {
  private process: ChildProcess | null = null
  private port: number = 0
  private intentionalStop = false
  private starting = false
  private readonly MAX_RESTART_ATTEMPTS = 3
  private readonly RESTART_COOLDOWN_MS = 60_000  // 60 秒内最多重启 MAX_RESTART_ATTEMPTS 次
  private restartTimestamps: number[] = []
  /**
   * 审查 M-7：首次 start() 尚未返回（starting=true）期间后端崩溃时，
   * exit 处理器无法立即重启（会与进行中的 start() 重叠）；原实现直接丢弃
   * 该重启意图，应用停留在 error 状态直到用户手动重启。
   * 现在记录待重启标志，start() 返回（finally）后立即执行。
   */
  private pendingRestart = false
  /** 后端状态变化回调：status / port / error */
  onStatusChange: ((data: { status: 'starting' | 'ready' | 'error' | 'restarting'; port?: number; error?: string }) => void) | null = null

  /**
   * 修复 4：后端 API token（构造时生成一次，自动重启复用同一 token，
   * 通过环境变量 CKUN_API_TOKEN 注入子进程，渲染层经 backend:getToken 获取）
   */
  private readonly apiToken: string = randomBytes(32).toString('hex')

  /** 返回当前后端 API token（即使未启动也已生成，供启动前查询） */
  getApiToken(): string {
    return this.apiToken
  }

  /**
   * 启动 Python 后端，返回就绪的端口号
   * @param extraEnv 额外环境变量（如 Electron bridge 端口/token）
   */
  async start(extraEnv?: Record<string, string>): Promise<number> {
    if (this.starting) {
      return this.port  // 已在启动中，避免重叠
    }
    this.starting = true
    try {
      this.intentionalStop = false
      // 发射 starting 状态，让渲染进程知道后端正在启动
      this.onStatusChange?.({ status: 'starting' })

      this.port = await this.findFreePort(8010, 8100)

      const backendDir = this.getBackendDir()
      const pythonCmd = this.getPythonCommand()
      const dbPath = this.ensureDbPath(backendDir)

      console.log(`[BackendManager] Starting Python backend on port ${this.port}`)
      console.log(`[BackendManager] Backend dir: ${backendDir}`)
      console.log(`[BackendManager] Python cmd: ${pythonCmd}`)
      console.log(`[BackendManager] DB path: ${dbPath}`)

      // M2 修复：默认绑定 127.0.0.1，防止局域网内其他设备访问后端（含 API Key、SSH 凭据等敏感数据）
      this.process = spawn(pythonCmd, ['main.py', '--port', String(this.port), '--host', '127.0.0.1'], {
        cwd: backendDir,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          DEV_MODE: app.isPackaged ? 'false' : 'true',
          // 显式传数据库绝对路径，避免后端依赖 cwd 解析相对路径在不同启动方式下丢库
          DB_PATH: dbPath,
          // 修复 4：注入后端 API token（进程级认证契约，后端据此校验 X-API-Token）
          CKUN_API_TOKEN: this.apiToken,
          ...extraEnv,
        },
        stdio: ['ignore', 'pipe', 'pipe']
      })

      const child = this.process
      // 修复 2a：spawn 失败（如 python 不存在 ENOENT）时若无 error 监听，
      // 会触发未捕获异常直接崩掉整个 Electron 主进程；此处记录日志并拒绝 start()。
      let spawnErrorOccurred = false
      const spawnErrorPromise = new Promise<never>((_, reject) => {
        child.on('error', (err) => {
          spawnErrorOccurred = true
          console.error('[BackendManager] Failed to spawn Python backend:', err)
          if (this.process === child) this.process = null
          const friendly = (err as NodeJS.ErrnoException)?.code === 'ENOENT'
            ? '未检测到 Python 运行环境，请安装 Python 后重试'
            : `后端进程启动失败: ${err.message || String(err)}`
          this.onStatusChange?.({ status: 'error', error: friendly })
          reject(new Error(friendly))
        })
      })

      this.process.stdout?.on('data', (data: Buffer) => {
        try { console.log(`[Python] ${data.toString().trim()}`) } catch { /* EPIPE on exit */ }
      })

      this.process.stderr?.on('data', (data: Buffer) => {
        try { console.error(`[Python] ${data.toString().trim()}`) } catch { /* EPIPE on exit */ }
      })

      this.process.on('exit', (code) => {
        console.log(`[BackendManager] Python process exited with code ${code}`)
        this.process = null

        // 非主动停止且非 spawn 失败时尝试自动重启
        if (!this.intentionalStop && !spawnErrorOccurred && code !== 0) {
          if (this.starting) {
            // 审查 M-7：首次启动进行中 — 不能立即重启（会重叠 spawn），
            // 记录意图，由 start() 的 finally 在返回后触发
            this.pendingRestart = true
          } else {
            this.tryAutoRestart()
          }
        }
      })

      // 等待后端就绪（与 spawn 错误竞争：ENOENT 时立即失败，而不是等 15s 超时）
      try {
        await Promise.race([this.waitForReady(this.port, 15000), spawnErrorPromise])
      } catch (err) {
        // 修复 2b：启动失败（spawn 错误或超时）时清理已 spawn 的子进程，
        // 避免留下孤儿进程占用端口；随后按原有失败语义向上抛出。
        this.killCurrentProcess()
        throw err
      }
      return this.port
    } finally {
      this.starting = false
      // 审查 M-7：补发首次启动期间记录的重启意图（进程非零退出且非主动停止）
      if (this.pendingRestart) {
        this.pendingRestart = false
        if (!this.intentionalStop) {
          void this.tryAutoRestart()
        }
      }
    }
  }

  /**
   * 崩溃后自动重启（带冷却限制，防止无限重启循环）
   */
  private async tryAutoRestart(): Promise<void> {
    // 已在启动中（例如首次 start() 尚未结束），跳过避免与进行中的 start() 重叠
    if (this.starting) {
      console.log('[BackendManager] 启动中，跳过自动重启')
      return
    }
    const now = Date.now()
    // 清理过期的重启记录
    this.restartTimestamps = this.restartTimestamps.filter(t => now - t < this.RESTART_COOLDOWN_MS)

    if (this.restartTimestamps.length >= this.MAX_RESTART_ATTEMPTS) {
      console.error(`[BackendManager] Too many restarts (${this.restartTimestamps.length} in ${this.RESTART_COOLDOWN_MS / 1000}s), giving up`)
      this.onStatusChange?.({ status: 'error', error: '后端多次崩溃，请重启应用' })
      return
    }

    this.restartTimestamps.push(now)
    console.log(`[BackendManager] Auto-restarting backend (${this.restartTimestamps.length}/${this.MAX_RESTART_ATTEMPTS} in window)...`)
    this.onStatusChange?.({ status: 'restarting' })

    // 延迟 1 秒后重启，避免立即重试
    await new Promise(r => setTimeout(r, 1000))

    // M1 修复：延迟期间状态可能变化——
    //   - 用户可能调用了 stop()（intentionalStop=true），此时不应再自动重启
    //   - 外部可能已手动调用了 start()（starting=true），避免重复 spawn
    if (this.intentionalStop) {
      console.log('[BackendManager] 延迟期间被主动停止，取消自动重启')
      return
    }
    if (this.starting) {
      console.log('[BackendManager] 延迟期间已有启动在进行，跳过自动重启')
      return
    }

    try {
      const port = await this.start()
      console.log(`[BackendManager] Backend auto-restarted on port ${port}`)
      this.onStatusChange?.({ status: 'ready', port })
    } catch (err) {
      console.error('[BackendManager] Auto-restart failed:', err)
      this.onStatusChange?.({ status: 'error', error: String(err) })
    }
  }

  /**
   * 杀当前子进程（含 Windows 进程树），并清理引用。幂等：进程已为空时直接返回。
   *
   * Windows 不支持 POSIX 信号：Node 在 Win 上把 SIGTERM/SIGKILL 都映射成
   * TerminateProcess(主进程)，不杀子进程。而 uvicorn reload=true 时会 spawn
   * reload 子进程——主 Python 被杀后 reload 子进程变孤儿继续 LISTENING 端口，
   * 下次启动又 spawn 新的，越积越多（5 个僵尸进程占 8010-8014 的实测根因）。
   *
   * Windows 用 taskkill /T /F 杀整个进程树；POSIX 平台仍用 SIGTERM→SIGKILL。
   * 修复 2b：启动失败（超时等）路径也复用本方法清理孤儿进程。
   */
  private killCurrentProcess(): void {
    const proc = this.process
    if (!proc) return

    // Optimization 修复：杀进程前移除所有监听器，防止闭包（含 this）长期持有旧 process 引用
    proc.stdout?.removeAllListeners()
    proc.stderr?.removeAllListeners()
    proc.removeAllListeners('exit')
    proc.removeAllListeners('error')

    const pid = proc.pid ?? 0
    const isWin = process.platform === 'win32'

    // Windows: taskkill /T /F 杀整个进程树（含 uvicorn reload 子进程）
    if (isWin && pid) {
      try {
        // 审查 MINOR：taskkill 对挂死的进程树可能长时间不返回（同步阻塞主进程）；
        // 5s 超时兜底 — 超时后进程多半已在终止过程中，不再等待
        execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore', timeout: 5000 })
        console.log(`[BackendManager] taskkill /PID ${pid} /T /F issued`)
      } catch (e) {
        console.warn(`[BackendManager] taskkill failed (process may have exited or timed out):`, e)
      }
      // Windows: taskkill 后清理引用
      this.process = null
      return
    }

    // POSIX: 优雅关闭 → 超时强杀
    proc.kill('SIGTERM')
    const forceKillTimer = setTimeout(() => {
      if (this.process === proc) {
        console.log('[BackendManager] Force killing Python process')
        proc.kill('SIGKILL')
      }
    }, 5000)
    proc.once('exit', () => {
      clearTimeout(forceKillTimer)
      if (this.process === proc) this.process = null
    })
  }

  /**
   * 停止 Python 后端
   * 修复 2c：与 spawn 错误路径共用 killCurrentProcess，均为幂等操作，不会互相竞争。
   */
  stop(): void {
    this.intentionalStop = true
    if (!this.process) return
    console.log('[BackendManager] Stopping Python backend...')
    this.killCurrentProcess()
  }

  getPort(): number {
    return this.port
  }

  /**
   * 获取 backend 目录路径
   * 开发模式：项目根目录/backend
   * 生产模式：app 资源目录/backend
   */
  private getBackendDir(): string {
    if (app.isPackaged) {
      return join(process.resourcesPath, 'backend')
    }
    return join(app.getAppPath(), 'backend')
  }

  /**
   * 确保数据库路径落在用户数据目录（跨启动稳定、跨重装保留、打包后可写）
   * 并从旧位置（backend/ckunagentic.db，相对 cwd 脆弱）迁移已有数据。
   *
   * 旧位置候选（开发模式可能留下的旧库）：
   *   - {backendDir}/ckunagentic.db
   *   - {cwd}/ckunagentic.db（兜底）
   */
  private ensureDbPath(backendDir: string): string {
    const userData = app.getPath('userData')
    const targetDir = join(userData, 'db')
    try {
      mkdirSync(targetDir, { recursive: true })
    } catch (e) {
      console.warn('[BackendManager] mkdir db dir failed (will fall back to userData root):', e)
    }
    const targetPath = join(targetDir, 'ckunagentic.db')

    // 已存在目标库则直接用——不覆盖用户已有数据
    if (existsSync(targetPath)) {
      return targetPath
    }

    // 尝试从旧位置迁移（开发模式遗留的相对路径库）
    const legacyCandidates = [
      join(backendDir, 'ckunagentic.db'),
      join(process.cwd(), 'ckunagentic.db'),
      join(homedir(), 'ckunagentic.db'),
    ]
    for (const legacy of legacyCandidates) {
      if (!existsSync(legacy)) continue
      try {
        // rename 跨卷失败时回退到 copy
        try {
          renameSync(legacy, targetPath)
        } catch {
          copyFileSync(legacy, targetPath)
        }
        console.log(`[BackendManager] Migrated DB: ${legacy} -> ${targetPath}`)
        return targetPath
      } catch (e) {
        console.warn(`[BackendManager] Failed to migrate DB from ${legacy}:`, e)
      }
    }
    return targetPath
  }

  /**
   * 获取 Python 命令
   * 优先使用系统 Python，后续可改为嵌入式 Python
   */
  private getPythonCommand(): string {
    return process.platform === 'win32' ? 'python' : 'python3'
  }

  /**
   * 查找可用端口
   */
  private findFreePort(start: number, end: number): Promise<number> {
    return new Promise((resolve, reject) => {
      const tryPort = (port: number) => {
        if (port > end) {
          reject(new Error('未找到可用端口，请检查系统端口占用情况'))
          return
        }
        const server = http.createServer()
        server.once('error', () => tryPort(port + 1))
        server.once('listening', () => {
          server.close(() => resolve(port))
        })
        server.listen(port, '127.0.0.1')
      }
      tryPort(start)
    })
  }

  /**
   * 轮询健康检查端点直到后端就绪
   */
  private waitForReady(port: number, timeoutMs: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const start = Date.now()
      const check = () => {
        if (Date.now() - start > timeoutMs) {
          reject(new Error('后端服务启动超时，请稍后重试'))
          return
        }

        const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
          if (res.statusCode === 200) {
            res.resume()
            resolve()
          } else {
            res.resume()
            setTimeout(check, 500)
          }
        })

        req.on('error', () => {
          setTimeout(check, 500)
        })

        req.setTimeout(2000, () => {
          req.destroy()
          setTimeout(check, 500)
        })
      }

      check()
    })
  }
}
