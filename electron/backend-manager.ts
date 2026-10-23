import { spawn, ChildProcess, execFile, execSync } from 'child_process'
import { join, basename, isAbsolute } from 'path'
import { app } from 'electron'
import http from 'http'
import { mkdirSync, existsSync, renameSync, copyFileSync, unlinkSync } from 'fs'
import { homedir } from 'os'
import { randomBytes } from 'crypto'
import { perfMark } from './utils/perf-mark'
import { resolvePowerShell } from './utils/powershell-path'
import { clearPythonCache, loadPythonCache, savePythonCache } from './backend-python-cache'

/** whenReady 的等待者：就绪/失败/超时三选一，各自只能被唤醒一次 */
type ReadyWaiter = {
  timer: ReturnType<typeof setTimeout>
  resolve: (port: number) => void
  reject: (err: Error) => void
}

/**
 * Python FastAPI 后端进程管理器
 * 负责 spawn、健康检查、优雅关闭、崩溃自动重启
 */
export class BackendManager {
  private process: ChildProcess | null = null
  private port: number = 0
  private intentionalStop = false
  /**
   * 审查R2修复：应用退出终态标志 — before-quit→stop() 若落在 start() 的
   * await 间隙（killOrphanBackends/findFreePort），this.process 尚为空，
   * stop() 直接 return，随后 spawn 照常执行 → 应用退出后留下孤儿 python。
   * stop() 置位后不再复位（崩溃自动重启也走 start()，同样必须放弃），
   * start() 在 spawn 前检查，置位即终止启动。
   */
  private appQuitting = false
  private starting = false
  private readonly MAX_RESTART_ATTEMPTS = 3
  private readonly RESTART_COOLDOWN_MS = 60_000  // 60 秒内最多重启 MAX_RESTART_ATTEMPTS 次
  /**
   * 后端端口分配区间 —— 分配（findFreePort）与孤儿识别（isOurBackendProcess）
   * 必须读同一个区间：两者各写一份字面量时，改了分配区间而忘了谓词，孤儿检测就
   * 再也匹配不上自家进程，退化成宽口径兜底（那条分支会误伤同签名的无关项目）。
   */
  private static readonly BACKEND_PORT_MIN = 8010
  private static readonly BACKEND_PORT_MAX = 8100
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

  /**
   * 上次 start() 注入的额外环境变量（如 Electron bridge 端口/token）。
   * 自动重启时若丢弃它，新后端进程将失去桥接地址 → 定时任务自动建会话
   * 会因"连接失败"或"未可用"而失败。这里保存并在重启时复用。
   */
  private lastExtraEnv: Record<string, string> = {}

  /**
   * 后端是否已通过健康检查、真正可服务。
   * this.port 在 spawn【之前】就分配好了（findFreePort 的返回值），所以"端口非 0"
   * 只代表"选定了一个端口"，不代表"有人在那个端口上监听" —— 按端口判断可服务状态
   * 必然拿到 ECONNREFUSED。
   */
  private ready = false
  private readonly readyWaiters = new Set<ReadyWaiter>()

  /**
   * 本次启动尝试期间后端 stderr 的尾部环形缓存（保留最后 4KB）。
   * 健康检查超时 / 进程提前退出时拼进错误消息 —— 否则“后端服务启动超时”
   * 这种只有一句话的报错，让人无法区分“Python 没装依赖”和“启动就是慢”。
   */
  private stderrTail = ''

  /** 返回当前后端 API token（即使未启动也已生成，供启动前查询） */
  getApiToken(): string {
    return this.apiToken
  }

  /**
   * 等待后端进入可服务状态并返回端口；已就绪立即 resolve。
   * 供主进程转发后端 HTTP 请求前使用（渲染进程挂载时抢发的 IPC 会早于 uvicorn 起监听）。
   * 崩溃自动重启期间同样适用：等待者会被这一次新实例的就绪唤醒。
   * 超时/本次启动失败则 reject，调用方按普通后端错误处理，不会永久挂起。
   */
  whenReady(timeoutMs = 15_000): Promise<number> {
    if (this.ready) return Promise.resolve(this.port)
    return new Promise<number>((resolve, reject) => {
      const waiter: ReadyWaiter = {
        timer: setTimeout(
          () => this.finishReady(waiter, new Error('后端服务启动超时，请稍后重试')),
          timeoutMs,
        ),
        resolve,
        reject,
      }
      this.readyWaiters.add(waiter)
    })
  }

  /** 唤醒单个等待者（已从集合移除才生效，保证 resolve/reject 只发生一次） */
  private finishReady(waiter: ReadyWaiter, err?: Error): void {
    if (!this.readyWaiters.delete(waiter)) return
    clearTimeout(waiter.timer)
    if (err) waiter.reject(err)
    else waiter.resolve(this.port)
  }

  /**
   * 置就绪标志并唤醒全部等待者。
   * err 非空 = 本次启动已失败（spawn 错误 / 健康检查超时），等待者收到错误，
   * 而不是挂到超时才动。
   */
  private markReady(err?: Error): void {
    this.ready = !err
    for (const waiter of [...this.readyWaiters]) this.finishReady(waiter, err)
  }

  /**
   * 解析 PowerShell 可执行文件路径：实现已提取为共享 util
   * （electron/utils/powershell-path.ts，与 shortcut-win 同源使用，候选表与
   * 回退口径见那里）；这里只留一层薄委托供本类静态调用点使用。
   */
  private static resolvePowerShell(): string {
    return resolvePowerShell()
  }

  /**
   * 判定某进程命令行是否属于本应用的后端进程。
   *
   * 不能只匹配 "main.py" — 用户机器上其他 Python 项目同样可能以 main.py 为入口，
   * 误杀会破坏无关服务。要求命中本应用的启动特征之一：
   *   1. 命令行包含本应用 backend 目录路径（dev 与打包两种布局都覆盖；
   *      spawn 传的是绝对脚本路径，否则这一条永远不会命中 —— 见 start()）
   *   2. 命令行是 `<…>\backend\main.py` 且带本应用固定的启动参数与端口区间
   *      （backendDir 位置变化时的兜底：装在别的目录的同一款应用）
   *
   * 兜底必须同时卡住目录名与分配区间（BACKEND_PORT_MIN..BACKEND_PORT_MAX）：只按
   * `main.py --port --host 127.0.0.1` 判定的话，任何 FastAPI 教程项目的命令行都能
   * 命中，而 taskkill 带 /T 会连它的整棵子进程树一起杀掉。
   */
  private static isOurBackendProcess(cmdline: string, backendDir: string): boolean {
    if (!cmdline) return false
    const norm = cmdline.replace(/\//g, '\\').toLowerCase()
    if (!norm.includes('main.py')) return false

    const dir = backendDir.replace(/\//g, '\\').toLowerCase()
    if (dir && norm.includes(dir)) return true

    const portMatch = /--port\s+(\d+)/.exec(norm)
    const port = portMatch ? Number(portMatch[1]) : NaN
    return (
      norm.includes('\\backend\\main.py') &&
      norm.includes('--host 127.0.0.1') &&
      Number.isInteger(port) &&
      port >= BackendManager.BACKEND_PORT_MIN &&
      port <= BackendManager.BACKEND_PORT_MAX
    )
  }

  /**
   * 命令行是否服务于“本次启动”的端口：`--port <port>` 作为独立数字 token 命中即真。
   * 本次端口由 findFreePort 保证选定当时空闲——若某个旧孤儿仍活着占着该端口，
   * findFreePort 会因 bind 失败改选别的端口，所以此刻命令行写着本次端口的存活
   * 进程只可能是本次 spawn 的后端进程树（python.cmd 包装器与其拉起的真实 python.exe
   * 孙进程 pid 不同、无法靠 child.pid 排除，但都带同一 --port）。据此豁免它们，
   * 杜绝 A1 延迟枚举把自家后端误杀进重启风暴。
   */
  private static usesOurPort(cmdline: string, port: number): boolean {
    if (!cmdline || !Number.isInteger(port)) return false
    const m = /--port\s+(\d+)/.exec(cmdline.toLowerCase())
    return !!m && Number(m[1]) === port
  }

  /**
   * 杀掉前一次应用异常退出后遗留的孤儿 Python backend 进程。
   * 这些进程持有过期的 Electron bridge 端口/token，会导致定时任务执行时
   * bridge 连接全部失败（日志中即 "Electron bridge 连接失败 port=30000"）。
   *
   * 启动性能优化：枚举改为异步（execFile + WQL provider 侧过滤）。原实现
   * execFileSync 同步阻塞主进程 1.1~1.4s（实测 powershell 冷启动 ~0.6s +
   * CIM 枚举 ~0.5s），且发生在 whenReady 里 createWindow 之前 —— 窗口出现
   * 时间被整段同步查询垫高。现在枚举与 findFreePort 并行，主进程不冻结。
   *
   * 时序契约（防误杀，A1 改造后）：枚举与杀灭均晚于 spawn 异步完成，不再要求
   * spawn 前汇合 —— 新后端的命令行同样命中 "python.exe + main.py" 特征，且因
   * python.cmd 包装器拉起真实 python.exe 孙进程，其 pid ≠ child.pid，靠 killPids 的
   * pid exclude 护不住；因此杀灭前先按“本次端口”签名（usesOurPort）把自家后端
   * 进程树整批剔除，taskkill 晚到一两秒对孤儿旧进程无实质影响。start() 在 spawn
   * 后的 fire-and-forget 闭包里 await 本 Promise、过滤后再杀灭（start() 主链不等
   * 它，健康轮询不被拖慢）。
   *
   * @param backendDir 本应用 backend 目录，用于精确识别"自己的"后端，
   *                   避免误伤用户机器上其他以 main.py 为入口的 Python 服务
   */
  private static killOrphanBackends(backendDir: string): Promise<Array<{ pid: number; cmdline: string }>> {
    if (process.platform === 'win32') {
      return this.listOrphanPidsWin().then((rows) =>
        rows.filter(
          (r) =>
            // 二次甄别不可省：WQL 只按 python.exe + '%main.py%' 粗筛，用户机器上
            // 其他以 main.py 为入口的 Python 服务同样命中（与 POSIX 分支对称）
            BackendManager.isOurBackendProcess(r.cmdline, backendDir) &&
            Number.isInteger(r.pid) &&
            r.pid > 0,
        ),
      )
    }
    return this.listOrphanPidsPosix(backendDir)
  }

  /** Windows：异步枚举孤儿后端 PID（PowerShell CIM + WQL provider 侧过滤） */
  private static listOrphanPidsWin(): Promise<Array<{ pid: number; cmdline: string }>> {
    return new Promise((resolve) => {
      const psArgs = [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        // 原全量枚举再在 ForEach 内取 .CommandLine 要对每个进程单独做一次
        // WMI 属性往返；WQL 里直接 SELECT 需要的两列，provider 侧批量取。
        "Get-CimInstance -Query \"SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='python.exe' AND CommandLine LIKE '%main.py%'\" | ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }",
      ]
      execFile(
        BackendManager.resolvePowerShell(),
        psArgs,
        { encoding: 'utf-8', timeout: 10000, windowsHide: true },
        (err, stdout) => {
          if (err) {
            // 记录真实原因。原实现静默 catch 成一句"CIM query unavailable"，
            // 无法区分"没有 PowerShell / 没有 WMI / 权限不足 / 命令语法错"——
            // 实测就是因为 PATH 被裁剪、spawn 裸名 'powershell' 报 ENOENT，
            // 而这一点在日志里完全看不出来。
            console.warn('[BackendManager] CIM orphan enumeration failed:', err.message)
            resolve([])
            return
          }
          const rows: Array<{ pid: number; cmdline: string }> = []
          for (const rawLine of stdout.split(/\r?\n/)) {
            const line = rawLine.trim()
            if (!line) continue
            const sep = line.indexOf('|')
            if (sep <= 0) continue
            const pid = parseInt(line.slice(0, sep).trim(), 10)
            if (isNaN(pid)) continue
            rows.push({ pid, cmdline: line.slice(sep + 1) })
          }
          resolve(rows)
        },
      )
    })
  }

  /**
   * macOS / Linux：pgrep 找 main.py 进程。
   * 与 Windows 分支对称的二次甄别：本机可能有多个 FastAPI 项目都用
   * main.py 入口（如其他工作区的后端服务），只杀本应用自己的。
   */
  private static async listOrphanPidsPosix(backendDir: string): Promise<Array<{ pid: number; cmdline: string }>> {
    const run = (cmd: string, timeoutMs: number): Promise<string> =>
      new Promise((resolve) => {
        execFile('/bin/sh', ['-c', cmd], { encoding: 'utf-8', timeout: timeoutMs }, (err, stdout) => {
          resolve(err ? '' : stdout)
        })
      })
    const out = await run('pgrep -f "main.py"', 5000)
    const pids = out
      .split(/\s+/)
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => !isNaN(n) && n > 0)
    const orphans: Array<{ pid: number; cmdline: string }> = []
    for (const pid of pids) {
      const cmdline = (await run(`ps -p ${pid} -o command=`, 3000)).trim()
      // 进程刚退出时取不到命令行：跳过（幂等，下次启动还会尝试）
      if (cmdline && BackendManager.isOurBackendProcess(cmdline, backendDir)) orphans.push({ pid, cmdline })
    }
    return orphans
  }

  /** 杀已确认的孤儿 PID 列表（spawn 新后端之后执行，异步不阻塞） */
  private static killPids(pids: number[], exclude: number[]): void {
    const isWin = process.platform === 'win32'
    for (const pid of pids) {
      // 防御：绝不对当前/刚拉起的后端进程下手（双保险，正常时序下枚举必早于 spawn）
      if (exclude.includes(pid) || pid === process.pid) continue
      try {
        if (isWin) {
          execFile('taskkill', ['/PID', String(pid), '/T', '/F'], { timeout: 5000, windowsHide: true }, () => {})
        } else {
          process.kill(pid, 'SIGTERM')
        }
      } catch { /* already exited */ }
    }
  }

  /**
   * 启动 Python 后端，返回就绪的端口号
   * @param extraEnv 额外环境变量（如 Electron bridge 端口/token）
   */
  async start(extraEnv?: Record<string, string>): Promise<number> {
    if (this.starting) {
      return this.port  // 已在启动中，避免重叠
    }
    // 杀掉上次异常退出残留的孤儿 python 主进程（持有过期的 bridge 端口/token，
    // 定时任务执行时 bridge 会连接失败）。枚举异步启动、与下面的 findFreePort
    // 并行；A1 提速改造：spawn 前不再 await 枚举汇合（CIM 枚举实测 0.9–1.3s，
    // 是启动关键路径上最大的一段串行阻塞），改为 spawn 后异步汇合再杀灭。
    // 防误杀语义不变：killPids 的 exclude 兜住新 spawn 的后端 pid 与主进程 pid
    // ——即便枚举快照（spawn 后才完成）把新后端捞进结果也不会被 taskkill 下手。
    const orphansPromise = BackendManager.killOrphanBackends(this.getBackendDir())
    // 记录本次注入的环境变量，供崩溃自动重启时复用（见 tryAutoRestart）
    if (extraEnv) {
      Object.assign(this.lastExtraEnv, extraEnv)
    }
    this.starting = true
    try {
      this.intentionalStop = false
      // 发射 starting 状态，让渲染进程知道后端正在启动
      this.onStatusChange?.({ status: 'starting' })

      const portProbe = this.findFreePort(BackendManager.BACKEND_PORT_MIN, BackendManager.BACKEND_PORT_MAX)
      this.port = await portProbe

      // 审查R2修复：上面的 findFreePort 是 await 间隙 — 若期间 before-quit→stop()
      // 已执行（当时 process 未 spawn、杀不到任何东西），此处必须放弃启动，
      // 否则 spawn 照常 → 应用退出后遗留孤儿 python。A1 后孤儿枚举汇合已后移
      // 到 spawn 之后，本检查仍是 spawn 前的 appQuitting 拦截点；start() 入口
      // 不重置该标志（自动重启同须放弃）。
      if (this.appQuitting) {
        this.killCurrentProcess() // 防御性清理（此刻通常无进程，幂等）
        throw new Error('应用正在退出，已取消后端启动')
      }

      const backendDir = this.getBackendDir()
      // spawn 前必须确认后端入口存在。理由不是"顺手加个校验"：Node 对「cwd 不存在」
      // 报的同样是 ENOENT，且 err.path 仍是解释器名（实测 `spawn('node',['-v'],
      // {cwd:'<不存在的目录>'})` → code=ENOENT / syscall=spawn node / path=node），
      // 于是下面的 error 监听会把"后端目录没了"翻译成"没装 Python"，把人往完全
      // 错误的方向查。本应用吃过一次这种误诊，见 getBackendDir 的两种启动形态。
      const mainPyPath = join(backendDir, 'main.py')
      if (!existsSync(mainPyPath)) {
        throw new Error(
          `后端入口文件不存在：${mainPyPath}`
          + `（appPath=${app.getAppPath()} isPackaged=${app.isPackaged}）`
          + ' — dev 请在仓库根启动，打包产物请检查 resources/backend 是否完整',
        )
      }
      const python = await this.resolvePythonCommand()
      perfMark('backend: python interpreter resolved')
      const dbPath = this.ensureDbPath(backendDir)

      // M2 修复：默认绑定 127.0.0.1，防止局域网内其他设备访问后端（含 API Key、SSH 凭据等敏感数据）
      // 脚本路径必须是绝对路径：命令行是 isOurBackendProcess 唯一的自有性判据，
      // 传相对名 "main.py" 时 backendDir 那条精确匹配永远不会命中，
      // 孤儿检测就只剩宽口径兜底在跑（宽口径必然误伤同签名的无关项目）。
      this.process = spawn(python.cmd, [...python.prefixArgs, join(backendDir, 'main.py'), '--port', String(this.port), '--host', '127.0.0.1'], {
        cwd: backendDir,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          // Windows 上 Python 管道 stdout 默认用 ANSI 代码页(cp936/GBK)，
          // Electron 按 UTF-8 读管道 → 中文日志/traceback 变 ?????。强制 UTF-8。
          PYTHONIOENCODING: 'utf-8',
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
      // 快照本次端口：闭包异步执行时 this.port 可能已被后续重启改写，必须钉死本次值。
      const currentPort = this.port
      perfMark(`backend: spawned pid=${child.pid}`)
      // A1：枚举汇合与杀灭整体后移到 spawn 之后，fire-and-forget，不阻塞健康轮询。
      // 杀灭前先按“本次端口”签名剔除自家后端进程树（包装器 + 孙进程，pid 各异但同端口），
      // 再交给 killPids；exclude 双保险（新 spawn 的直接 pid 与主进程 pid）仍保留。
      // 闭包只捕获上方 const child / currentPort 常量快照，不引用可变的 this.process。
      void (async () => {
        try {
          const rows = await orphansPromise
          perfMark('backend: orphan enumeration done')
          const orphans = rows
            .filter((r) => !BackendManager.usesOurPort(r.cmdline, currentPort))
            .map((r) => r.pid)
          BackendManager.killPids(orphans, [child.pid ?? -1, process.pid])
        } catch (e) {
          console.warn('[BackendManager] 孤儿枚举/杀灭失败（不影响本次启动）:', e)
        }
      })()

      // 修复 2a：spawn 失败（如 python 不存在 ENOENT）时若无 error 监听，
      // 会触发未捕获异常直接崩掉整个 Electron 主进程；此处记录日志并拒绝 start()。
      let spawnErrorOccurred = false
      // E23 说明：此 Promise<never> 在 spawn 成功时【永不 settle 是有意的】—
      // 它只与 waitForReady 竞速：'error' 事件触发时 reject（ENOENT 等启动
      // 失败立即返回，不用等 15s 超时）；启动成功则它一直悬置，无副作用
      // （无人再引用它，GC 回收）。不要"顺手"补 resolve 逻辑。
      const spawnErrorPromise = new Promise<never>((_, reject) => {
        child.on('error', (err) => {
          spawnErrorOccurred = true
          console.error('[BackendManager] Failed to spawn Python backend:', err)
          if (this.process === child) this.process = null
          const friendly = (err as NodeJS.ErrnoException)?.code === 'ENOENT'
            // cwd 已在 spawn 前校验过（见上方 mainPyPath），走到这里 ENOENT 就真是
            // 解释器不可执行 —— 消息里带上实际 spawn 的命令名，不再是泛指"Python"。
            ? `未检测到可执行的 Python 解释器（spawn ${ (err as NodeJS.ErrnoException).path || python.cmd } 失败），请安装 Python 或用 CKUN_PYTHON 指定解释器路径`
            : `后端进程启动失败: ${err.message || String(err)}`
          this.onStatusChange?.({ status: 'error', error: friendly })
          reject(new Error(friendly))
        })
      })

      // 空监听器不是遗漏：挂 'data' 才会把 stdout 推入 flowing 模式，
      // 不消费则子进程会因管道缓冲写满而阻塞。
      // 注意这里**只是丢弃**：stdout 内容不会进状态事件（onStatusChange 的载荷只有
      // status/port/error），渲染层看不到后端日志。原先这句“已由状态事件透传”是假的，
      // 会让人以为可以在 UI 里找到后端 stdout。要读后端输出只能看 stderr ——
      // 它在下面被转进主进程控制台（'[Python] …'）。
      this.process.stdout?.on('data', () => {})

      this.stderrTail = ''
      this.process.stderr?.on('data', (data: Buffer) => {
        try {
          const text = data.toString()
          // 环形缓存尾部 4KB：启动失败时拼进错误消息，超时不再是“无头案”
          this.stderrTail = (this.stderrTail + text).slice(-4000)
          console.error(`[Python] ${text.trim()}`)
        } catch { /* EPIPE on exit */ }
      })

      this.process.on('exit', (code) => {
        this.process = null
        // 进程已死，端口上再没有人监听；重启完成前 whenReady 的等待者必须继续等
        this.ready = false

        // 非主动停止且非 spawn 失败时尝试自动重启
        if (!this.intentionalStop && !spawnErrorOccurred && code !== 0) {
          if (this.starting) {
            // 审查 M-7：首次启动进行中 — 不能立即重启（会重叠 spawn），
            // 记录意图，由 start() 的 finally 在返回后触发
            this.pendingRestart = true
          } else {
            this.tryAutoRestart()
          }
        } else if (!this.intentionalStop && !spawnErrorOccurred && code === 0) {
          // 审查修复：非主动停止却以 code=0 退出（如 uvicorn 应用初始化失败后
          // 正常退出）— 原实现既不重启也不推状态，前端停留在 'ready' 假象，
          // 所有 API 调用才失败且无从排查。通知前端后端已停。
          console.warn('[BackendManager] Backend exited with code 0 unexpectedly (not intentional stop)')
          this.onStatusChange?.({ status: 'error', error: '后端进程异常退出，请重启应用' })
        }
      })

      // 审查 M-7 补充：进程在就绪【之前】退出时立即失败，不再干等 15s 超时。
      // 实测场景：Windows 的 Microsoft Store python 存根（App Execution Alias）
      // spawn 成功但立即 exit code=9009 且无任何有效输出 —— 原实现只能等
      // waitForReady 15s 超时，用户看到的是“启动超时”而非真实原因。
      // 注意与下方 exit 处理器共生：它已置 pendingRestart，本 Promise 只负责
      // 让 start() 立刻带着真实错误返回，自动重启链路不受影响。
      const childExitPromise = new Promise<never>((_, reject) => {
        child.once('exit', (code) => {
          if (this.ready) return // 已就绪后的退出走崩溃自动重启，不归启动流程管
          const tail = this.stderrTail.trim()
          reject(new Error(
            `后端进程在启动阶段退出（code=${code}）`
            + (tail ? `，后端输出：${tail}` : '')
          ))
        })
      })

      // 等待后端就绪（与 spawn 错误/提前退出竞争：任一失败立即返回，而不是等 15s 超时）
      try {
        await Promise.race([this.waitForReady(this.port, 15000), spawnErrorPromise, childExitPromise])
      } catch (err) {
        // 修复 2b：启动失败（spawn 错误或超时）时清理已 spawn 的子进程，
        // 避免留下孤儿进程占用端口；随后按原有失败语义向上抛出。
        this.killCurrentProcess()
        // A2：健康就绪前失败 = 本次解释器（含缓存命中值）失真，作废缓存，
        // 下一次 start 回到全量探测，不会反复信一个已经坏掉的缓存值。
        clearPythonCache(app.getPath('userData'))
        // 唤醒 whenReady 等待者：本次启动已确定失败，让它们立即拿到错误，
        // 而不是挂到自己的超时才动
        this.markReady(err instanceof Error ? err : new Error(String(err)))
        throw err
      }
      this.markReady()
      // A2：就绪是唯一可信锚点——本次实际用于 spawn 并通过健康检查的解释器
      // 才写入缓存（探测成功不提前写，避免把"能导入依赖但跑不起服务"的值污染缓存）
      savePythonCache(app.getPath('userData'), python)
      perfMark('backend: health ready')
      return this.port
    } catch (err) {
      // spawn 之前的失败（后端目录缺失 / 没有可用解释器 / 应用正在退出）也必须
      // 唤醒 whenReady 的等待者：它们 otherwise 只能干等自己那 15s 超时，还拿不到
      // 真实原因。markReady 幂等（等待者取空后才生效），spawn 之后那条失败路径
      // 已在 Promise.race 的 catch 里唤醒过，这里重复调用无副作用。
      // A2：启动在就绪前以任何理由中断都作废缓存（含 appQuitting 取消——幂等删除，
      // 最坏代价是下一次多跑一轮探测，不会把失真值留给下一次）。
      clearPythonCache(app.getPath('userData'))
      this.markReady(err instanceof Error ? err : new Error(String(err)))
      throw err
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
    if (this.starting) return
    const now = Date.now()
    // 清理过期的重启记录
    this.restartTimestamps = this.restartTimestamps.filter(t => now - t < this.RESTART_COOLDOWN_MS)

    if (this.restartTimestamps.length >= this.MAX_RESTART_ATTEMPTS) {
      console.error(`[BackendManager] Too many restarts (${this.restartTimestamps.length} in ${this.RESTART_COOLDOWN_MS / 1000}s), giving up`)
      this.onStatusChange?.({ status: 'error', error: '后端多次崩溃，请重启应用' })
      return
    }

    this.restartTimestamps.push(now)

    this.onStatusChange?.({ status: 'restarting' })

    // 延迟 1 秒后重启，避免立即重试
    await new Promise(r => setTimeout(r, 1000))

    // M1 修复：延迟期间状态可能变化——
    //   - 用户可能调用了 stop()（intentionalStop=true），此时不应再自动重启
    //   - 外部可能已手动调用了 start()（starting=true），避免重复 spawn
    if (this.intentionalStop) return
    if (this.starting) return

    try {
      const port = await this.start(this.lastExtraEnv)
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
   * TerminateProcess(主进程)，不杀子进程。主 Python 被杀后它名下的任何子进程都会
   * 变孤儿继续 LISTENING 端口，下次启动又 spawn 新的，越积越多
   * （5 个僵尸进程占 8010-8014 的实测根因就是这个）。
   *
   * 归因更正：这里原先写的是"uvicorn reload=true 会 spawn reload 子进程"，而
   * `backend/main.py` 明确 `reload=False`（那里的注释还专门说明为了健康检查不走
   * reloader）。按树杀（taskkill /T /F）的防护本身是对的 —— 它管的是"任何派生子进程"
   * 这一整类，不依赖 reload 开着；留着那句错归因会让人以为关掉 reload 就不用 /T 了。
   *
   * Windows 用 taskkill /T /F 杀整个进程树；POSIX 平台仍用 SIGTERM→SIGKILL。
   * 修复 2b：启动失败（超时等）路径也复用本方法清理孤儿进程。
   */
  private killCurrentProcess(): void {
    const proc = this.process
    if (!proc) return

    // 就绪标志先落地：下面 removeAllListeners 会摘掉 'exit' 监听，
    // 那条路径上的 ready=false 也就不会执行了（stop() 主动停止走这里）
    this.ready = false

    // Optimization 修复：杀进程前移除所有监听器，防止闭包（含 this）长期持有旧 process 引用
    proc.stdout?.removeAllListeners()
    proc.stderr?.removeAllListeners()
    proc.removeAllListeners('exit')
    proc.removeAllListeners('error')

    const pid = proc.pid ?? 0
    const isWin = process.platform === 'win32'

    // Windows: taskkill /T /F 杀整个进程树（含主 Python 名下任何子进程）
    if (isWin && pid) {
      try {
        // 审查 MINOR：taskkill 对挂死的进程树可能长时间不返回（同步阻塞主进程）；
        // 5s 超时兜底 — 超时后进程多半已在终止过程中，不再等待
        execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore', timeout: 5000 })
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
    // 审查R2修复：置应用退出终态标志 — 即使 stop() 落在 start() 的 await 间隙
    // （this.process 为空、下面直接 return），start() 也会在 spawn 前检查此标志
    // 并放弃启动，不再遗留退出后的孤儿 python（见 start() 内 appQuitting 守卫）
    this.appQuitting = true
    if (!this.process) return
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
      const family = BackendManager.legacyDbFamily(legacy)
      try {
        BackendManager.moveDbFamily(family, targetDir, targetPath)
        return targetPath
      } catch (e) {
        console.warn(`[BackendManager] Failed to migrate DB from ${legacy}:`, e)
      }
    }
    return targetPath
  }

  /**
   * S0-08：一次迁移必须搬的文件集合。
   *
   * 后端 `PRAGMA journal_mode=WAL`（backend/database.py），已提交的事务会合法地
   * 留在 `ckunagentic.db-wal` 里，直到 checkpoint 才回写主文件。原实现只 rename
   * 主 `.db`，于是 SQLite 在目标目录新建一个空 `-wal`，旧目录那份 wal 里最新的
   * 事务成为孤儿 —— 而且下一次启动 `existsSync(targetPath)` 会直接早退，把这个
   * 半迁移态永久锁死。丢的是 SSH 连接记录与凭据、agent 记忆、定时任务，
   * 且用户看不到任何错误。
   */
  static legacyDbFamily(
    legacy: string,
    exists: (p: string) => boolean = existsSync,
  ): string[] {
    return [legacy, `${legacy}-wal`, `${legacy}-shm`].filter((p) => exists(p))
  }

  /**
   * 把整族文件落到 targetDir。先整批暂存成 `.part`，再逐个同目录 rename ——
   * 主库只有在三件都拷成功之后才会出现在目标位置，所以任何中途失败都不会留下
   * 「有主库、缺 wal」的半迁移态（那正是原实现被 existsSync 早退锁死的那个坑）。
   */
  private static moveDbFamily(
    family: string[],
    targetDir: string,
    targetPath: string,
  ): void {
    const staged = family.map((from) => {
      const to = join(targetDir, basename(from))
      return { from, to, part: `${to}.part` }
    })
    try {
      for (const s of staged) copyFileSync(s.from, s.part)
    } catch (e) {
      for (const s of staged) {
        try { unlinkSync(s.part) } catch { /* 没拷出来就不必清 */ }
      }
      throw e
    }
    for (const s of staged) renameSync(s.part, s.to)
    // 源文件最后删，且删失败只当噪声：此刻目标已完整，旧位置多留一份不影响正确性
    // （下次启动走 existsSync(targetPath) 早退，不会再读它）。
    for (const s of staged) {
      try { unlinkSync(s.from) } catch (e) {
        console.warn(`[BackendManager] legacy DB source left behind: ${s.from}`, e)
      }
    }
    if (!existsSync(targetPath)) {
      throw new Error(`migration produced no target DB: ${targetPath}`)
    }
  }

  /**
   * 解析 Python 命令，解析顺序：
   * 1. CKUN_PYTHON 环境变量（开发/排障显式指定，不做探测直接信任）
   * 2. 打包后的内嵌运行时 resources/python-runtime/（构建时由 scripts/package.js
   *    把仓库根 runtime/ 拷入 extraResources；Portable Python 解压到此目录）
   * 3. 乐观缓存（上次健康就绪过的解释器，见 backend-python-cache 模块）
   * 4. 并行探测系统候选（Windows: python / python3 / py -3；POSIX: python3 / python）
   *
   * 缓存失效口径：健康就绪前任何失败（spawn ENOENT/9009、健康超时、启动被取消）
   * 都视为缓存失真，start() 统一 clearPythonCache；只有 markReady 成功才回写——
   * 探测通过≠后端真能用，就绪才是唯一可信锚点。
   *
   * 探测的必要性：Windows 上裸名 'python' 可能解析到 Microsoft Store 的
   * App Execution Alias 存根 —— spawn 成功、立即 exit 9009、无有效输出，
   * 原实现的裸名回退只能等到健康检查 15s 超时且看不出原因。探测还顺带
   * 验证了 fastapi/uvicorn 可导入，把“解释器存在但没装依赖”也挡在 spawn 之前。
   * 全部候选不可用时抛出带尝试明细的错误，不再继续 spawn。
   */
  private async resolvePythonCommand(): Promise<{ cmd: string; prefixArgs: string[] }>
  {
    const envOverride = process.env.CKUN_PYTHON?.trim()
    if (envOverride) {
      if (existsSync(envOverride)) return { cmd: envOverride, prefixArgs: [] }
      console.warn(`[BackendManager] CKUN_PYTHON 指定的解释器不存在，回退到自动探测: ${envOverride}`)
    }
    if (app.isPackaged) {
      const bundled =
        process.platform === 'win32'
          ? join(process.resourcesPath, 'python-runtime', 'python.exe')
          : join(process.resourcesPath, 'python-runtime', 'bin', 'python3')
      if (existsSync(bundled)) return { cmd: bundled, prefixArgs: [] }
    }

    // A2 乐观缓存层（上次健康就绪过的解释器）：命中即跳过并行探测（实测 ~0.5s）。
    // 存在性判定分两口径：绝对路径 cmd 必须在盘上（卸载/移动即回退探测）；
    // 裸名（python/py）由 spawn 按 PATH 解析，existsSync 判不了也不算失真——
    // 失真兜底在消费端：健康就绪前任何失败都会 clearPythonCache 作废本次缓存。
    const cached = loadPythonCache(app.getPath('userData'))
    if (cached && (!isAbsolute(cached.cmd) || existsSync(cached.cmd))) {
      perfMark('backend: python cache hit')
      console.log(`[BackendManager] 解释器缓存命中: ${[cached.cmd, ...cached.prefixArgs].join(' ')}`)
      return cached
    }

    const candidates = process.platform === 'win32'
      ? [
          { cmd: 'python', prefixArgs: [] as string[] },
          { cmd: 'python3', prefixArgs: [] },
          { cmd: 'py', prefixArgs: ['-3'] },
        ]
      : [
          { cmd: 'python3', prefixArgs: [] as string[] },
          { cmd: 'python', prefixArgs: [] },
        ]

    // 启动性能优化：所有候选解释器并行探测（Promise.all），
    // 按优先级选第一个成功的返回。原实现串行逐个 spawn 探测（最快 1×、最慢 3×
    // spawn 开销），无论几台候选现在都只等一轮并行探测完成。
    const results = await Promise.all(
      candidates.map((c) =>
        BackendManager.probePython(c.cmd, c.prefixArgs).then((ok) => ({ ok, c })),
      ),
    )
    for (const r of results) {
      if (r.ok) {
        const label = [r.c.cmd, ...r.c.prefixArgs].join(' ')
        console.log(`[BackendManager] Using Python interpreter: ${label}`)
        return r.c
      }
    }

    const failures = candidates.map((c) => [c.cmd, ...c.prefixArgs].join(' '))
    throw new Error(
      `未找到可运行后端的 Python 环境（需要 fastapi/uvicorn）。`
      + `已尝试: ${failures.join('、')}。`
      + `请安装依赖或设置 CKUN_PYTHON 环境变量指定解释器路径。`
    )
  }

  /** 探测候选解释器：能执行且可导入 fastapi/uvicorn 才算可用（10s 超时兜底） */
  private static probePython(cmd: string, prefixArgs: string[]): Promise<boolean> {
    return new Promise((resolve) => {
      execFile(
        cmd,
        [...prefixArgs, '-c', 'import fastapi, uvicorn'],
        { timeout: 10000, windowsHide: true },
        (err) => resolve(!err),
      )
    })
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
        server.once('error', () => {
          // D17 修复：error 分支补 server.close()，与 electron-bridge.ts 同名
          // 实现对齐 — 否则监听套接句柄泄漏，端口扫描循环可能挂住
          server.close()
          tryPort(port + 1)
        })
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
          // 附带 stderr 尾部：区分“启动慢”与“根本起不来”（如依赖缺失的 traceback）
          const tail = this.stderrTail.trim()
          reject(new Error('后端服务启动超时，请稍后重试' + (tail ? `，后端输出：${tail}` : '')))
          return
        }

        // 每次尝试只允许重新排期一次轮询：超时分支的 req.destroy() 会再触发
        // 'error'，若两路各自 setTimeout(check) 则每轮翻倍 → 一次 15s 启动内
        // 并发 GET 指数级暴涨。settled 去重，无论 error/timeout/非200 只重排一次。
        let settled = false
        const reschedule = () => {
          if (settled) return
          settled = true
          // 启动性能优化：Python 导入 + uvicorn 起监听集中在最初 2~3 秒完成，
          // 固定 500ms 间隔会把就绪时间向上取整、平均多等 250ms。前期收紧到
          // 150ms，后端仍未起（如冷启动慢盘/杀软扫描）时退回 500ms 不再加压。
          const elapsed = Date.now() - start
          setTimeout(check, elapsed < 3000 ? 150 : 500)
        }

        const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
          if (res.statusCode === 200) {
            res.resume()
            // 成功即终结本次尝试：不置 settled 的话，resolve 前已在途的 socket
            // 'error' 回调仍会多排一轮 check（幂等但留悬空定时器）。
            settled = true
            req.destroy()
            resolve()
          } else {
            res.resume()
            reschedule()
          }
        })

        req.on('error', reschedule)

        req.setTimeout(2000, () => {
          req.destroy()
          reschedule()
        })
      }

      check()
    })
  }
}
