import { app, BrowserWindow, ipcMain, shell, dialog, session, nativeImage } from 'electron'
import { join, resolve as pathResolve, dirname, basename, extname } from 'path'
import { pathToFileURL } from 'url'
import { readdir, readFile, writeFile, stat, lstat, mkdir, rename, rm, realpath, copyFile } from 'fs/promises'
import { existsSync, mkdirSync, writeFileSync, unlinkSync } from 'fs'
import { homedir } from 'os'
import { execSync, execFile, spawn } from 'child_process'
import { BackendManager } from './backend-manager'
import { PtyManager, clampPtyDimension } from './pty-manager'
import { loadSsh2, SSHManager } from './ssh-manager'
import { SSHError, toIpcTransport } from './ssh-error'
import { SSHStore, type SSHConnectionRecord } from './ssh-store'
import { TunnelStore, type TunnelRule } from './tunnel-store'
import { TunnelManager } from './tunnel-manager'
import { GithubAccelService } from './github-accel'
import { GitReposService } from './git-repos'
import { GhDownloadManager } from './gh-download'
import { GhHostsService } from './gh-hosts'
import { SshTransport } from './transports/ssh-transport'
import { GatewayStore, type GatewayRecord } from './gateway-store'
import { GatewayManager } from './gateway-manager'
import { ElectronBridge, BridgeOp } from './electron-bridge'
import { BridgeWsClient } from './bridge-ws-client'
import {
  getDesktopPrefs,
  initDesktopPrefs,
  markQuitting,
  notifyDesktop,
  setDesktopPrefs,
  shouldHideOnClose,
} from './desktop-prefs'
import { assertSafePath, allowDialogKeyPath, isCopyTargetSelfNested, rejectSymlinkTarget } from './utils/path-guard'
import { beginCopy, endCopy, requestCopyCancel } from './utils/copy-cancel'
import { perfMark } from './utils/perf-mark'
import { ensureShellShortcuts } from './shortcut-win'

perfMark('main module loaded')

let mainWindow: BrowserWindow | null = null
// 开发态判定：原 `is.dev`（@electron-toolkit/utils）语义就是 `!app.isPackaged`，
// 仅这一处用途，为一个布尔值拖一个依赖不值（依赖清单已删该包，保留 import 会让
// 全新 install 后 typecheck 红、打包产物在 externalizeDepsPlugin 下启动即崩）
const isDev = !app.isPackaged
let backendManager: BackendManager | null = null
let ptyManager: PtyManager | null = null
let sshManager: SSHManager | null = null
let sshStore: SSHStore | null = null
let tunnelStore: TunnelStore | null = null
let tunnelManager: TunnelManager | null = null
let gatewayStore: GatewayStore | null = null
let gatewayManager: GatewayManager | null = null
let githubAccel: GithubAccelService | null = null
let ghDownload: GhDownloadManager | null = null
let ghHosts: GhHostsService | null = null
let gitRepos: GitReposService | null = null
let electronBridge: ElectronBridge | null = null
let bridgeWsClient: BridgeWsClient | null = null

// IPC sender 校验：仅允许主窗口的渲染进程发起调用，防止 XSS / 外部导航接管 IPC。
function assertTrustedSender(event: Electron.IpcMainInvokeEvent): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    throw new Error('主窗口不存在，拒绝 IPC 请求')
  }
  if (event.sender !== mainWindow.webContents) {
    throw new Error(`拒绝来自非主窗口渲染进程的 IPC 请求: ${event.senderFrame?.url || 'unknown'}`)
  }
}

/** 带 sender 校验 + SSHError 序列化的 IPC handler 注册（所有 ipcMain.handle 一律走此）。 */
function handleTrusted(
  channel: string,
  fn: (event: Electron.IpcMainInvokeEvent, ...args: any[]) => any,
): void {
  ipcMain.handle(channel, (event, ...args) => {
    assertTrustedSender(event)
    try {
      const result = fn(event, ...args)
      // async handler 返回 Promise：拦截 rejection 中的 SSHError
      if (result && typeof (result as any).then === 'function') {
        return (result as Promise<any>).catch((err: unknown) => {
          throw normalizeIpcError(err)
        })
      }
      return result
    } catch (err) {
      throw normalizeIpcError(err)
    }
  })
}

/** 错误 SSHError → IPC 可序列化的 transport 格式（双通道：__sshError 增强 + name 前缀兜底）。 */
function normalizeIpcError(err: unknown): Error {
  if ((err as any)?.__sshError) {
    // 已是 transport（二次穿透防護）
    return err as Error
  }
  if (isSSHError(err)) {
    const transport = toIpcTransport(err as any)
    const e = new Error(transport.message) as Error & { __sshError?: any; name: string }
    e.name = `SSHError:${transport.code}`
    e.__sshError = transport
    if ((err as Error).stack) {
      e.stack = `${e.name}: ${e.message}\n${(err as Error).stack}`
    }
    return e
  }
  return err instanceof Error ? err : new Error(String(err))
}

/** 类型守卫：判断是否为 SSHError */
function isSSHError(e: unknown): e is SSHError {
  return e instanceof SSHError
}

const IMAGE_MIME_MAP: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
}

/** 背景图文件名前缀 + 存放目录（保存/清理/加载/删除四处共用，避免字面量散落） */
const BG_FILE_PREFIX = 'background.'
function backgroundDir(): string {
  return join(app.getPath('userData'), 'backgrounds')
}

/** 保留原始 err.message，仅在缺失时兜底中文文案后重新抛出（fs IPC 处理器共用） */
function fsErrorWith(err: unknown, fallback: string): Error {
  return new Error((err as Error)?.message || fallback)
}

function createWindow(): void {
  // dev 态窗口/任务栏用的是 node_modules 里 electron.exe 的默认图标，不显式设 icon
  // 就永远不随 resources/icon.png 更换；打包态 exe 虽已内嵌 .ico，这里同样显式设置，
  // 两条链路统一口径。读不到（空图）时不传 icon，回退各平台默认而不是用坏图标覆盖。
  const appIcon = nativeImage.createFromPath(join(app.getAppPath(), 'resources', 'icon.png'))
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    backgroundColor: '#0d1117',
    icon: appIcon.isEmpty() ? undefined : appIcon,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  // manager 的窗口引用只在这一处绑定：setup*IPC 先于本函数执行，那时 mainWindow 还是 null，
  // 各自再绑一遍是空操作。macOS activate 重建窗口也复用这里。
  ptyManager?.setWindow(mainWindow)
  sshManager?.setWindow(mainWindow)
  tunnelManager?.setWindow(mainWindow)

  mainWindow.on('ready-to-show', () => {
    perfMark('window ready-to-show')
    mainWindow?.show()
  })

  mainWindow.webContents.once('did-finish-load', () => perfMark('renderer did-finish-load'))

  // G8：关窗隐藏到托盘。preventDefault 后窗口不销毁，'closed' 不触发，
  // window-all-closed 也就不会误判成"用户退出了"而拆掉后端/PTY。
  mainWindow.on('close', (event) => {
    if (!shouldHideOnClose()) return
    event.preventDefault()
    mainWindow?.hide()
  })

  mainWindow.webContents.setWindowOpenHandler((details) => {
    let protocol = ''
    try {
      protocol = new URL(details.url).protocol
    } catch {
      return { action: 'deny' }  // 畸形 URL 一律拒绝
    }
    if (protocol === 'http:' || protocol === 'https:') {
      shell.openExternal(details.url)
    }
    return { action: 'deny' }
  })

  // 导航守卫：仅允许自身渲染层同源导航，防止外部站点通过链接/劫持接管应用窗口。
  const allowedOrigins = new Set<string>()
  if (isDev && process.env['ELECTRON_RENDERER_URL']) {
    try { allowedOrigins.add(new URL(process.env['ELECTRON_RENDERER_URL']).origin) } catch { /* ignore */ }
  }
  // prod file: 导航限制在应用渲染层目录内（避免任意本地文档通过窗口获取 preload IPC 面）。
  const rendererRootHref = pathToFileURL(join(__dirname, '../renderer')).href + '/'
  const isOwnRendererFile = (href: string): boolean =>
    process.platform === 'win32'
      ? href.toLowerCase().startsWith(rendererRootHref.toLowerCase())
      : href.startsWith(rendererRootHref)
  mainWindow.webContents.on('will-navigate', (event, url) => {
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      event.preventDefault()
      return
    }
    const isAllowed =
      (isDev && allowedOrigins.has(parsed.origin)) ||
      (!isDev && parsed.protocol === 'file:' && isOwnRendererFile(parsed.href)) ||
      url === mainWindow?.webContents.getURL()
    if (!isAllowed) {
      event.preventDefault()
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        shell.openExternal(url)
      }
    }
  })

  if (isDev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

// CSP 一次性安装（防止 macOS 窗口重建时重复叠加 onHeadersReceived 回调）
let cspInstalled = false
function installCsp(): void {
  if (cspInstalled) return
  cspInstalled = true
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https: http://127.0.0.1:*; font-src 'self' data:; connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*;"
        ]
      }
    })
  })
}

// ─── Window Control IPC ────────────────────────────
/** 对单个字符串做单引号 shell 转义 */
function shellQuote(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`
}

/** 把字符串数组转成 bash 位置参数列表，用于嵌入单引号 shell 脚本 */
function shellQuoteArgs(segments: string[]): string {
  return segments.map(shellQuote).join(' ')
}

/** 把多行 bash 脚本转义后嵌入单引号参数（如 bash -lic '...'） */
function escapeBashScript(script: string): string {
  return script.replace(/'/g, "'\\''")
}

/** 构造本地/远程 bash 补全脚本体（不含外层 bash -lic 包装） */
function buildBashCompletionScript(opts: {
  isCommand: boolean
  cmd: string
  prefix: string
  prevSegments?: string[]
}): string {
  const { isCommand, cmd, prefix, prevSegments = [] } = opts
  const qPrefix = shellQuote(prefix)
  if (isCommand) {
    return [
      'shopt -s expand_aliases 2>/dev/null',
      'for f in /etc/profile.d/*.sh /etc/profile.d/conda.sh; do [ -f "$f" ] && . "$f" 2>/dev/null; done',
      // 用户需求：只显示文件目录和路径，历史记录命令由前端补充
      // 不再输出 compgen -A command（PATH 命令范围太广）
      `compgen -A file -- ${qPrefix} 2>/dev/null | sed 's/^/file:/'`,
    ].join('\n')
  }
  const qCmd = shellQuote(cmd)
  const qPrevSegments = shellQuoteArgs(prevSegments)
  return [
    'set +e',
    `CMD=${qCmd}`,
    'echo_args() { while read -r x; do [ -n "$x" ] && echo "arg:$x"; done; }',
    'COMP_FUNC=""',
    'if [ -n "$CMD" ]; then',
    '  CMP=$(complete -p "$CMD" 2>/dev/null)',
    '  if [ -n "$CMP" ]; then',
    '    FUNC=$(echo "$CMP" | sed -n "s/.*-F \\([^ ]*\\).*/\\1/p")',
    '    if [ -n "$FUNC" ] && type "$FUNC" >/dev/null 2>&1; then',
    '      COMP_FUNC="$FUNC"',
    '    fi',
    '  fi',
    'fi',
    'if [ -n "$COMP_FUNC" ]; then',
    `  set -- ${qCmd} ${qPrevSegments} ${qPrefix}`,
    '  COMP_WORDS=("$@")',
    '  COMP_CWORD=$(( $# - 1 ))',
    '  COMP_LINE="$*"',
    '  COMP_POINT=${#COMP_LINE}',
    '  "$COMP_FUNC" "$@" 2>/dev/null | echo_args',
    'fi',
    // 无论是否有专属补全函数，都根据前缀补全当前目录文件/目录
    `compgen -A file -- ${qPrefix} 2>/dev/null | sed 's/^/file:/'`,
  ].join('\n')
}

function setupWindowIPC(): void {
  handleTrusted('win:minimize', () => mainWindow?.minimize())
  handleTrusted('win:maximize', () => {
    if (mainWindow?.isMaximized()) {
      mainWindow.unmaximize()
    } else {
      mainWindow?.maximize()
    }
  })
  handleTrusted('win:close', () => mainWindow?.close())

  // ── 桌面偏好（G8）：自启 / 关窗隐藏到托盘 / 桌面通知 ──
  handleTrusted('prefs:get', () => getDesktopPrefs())
  handleTrusted('prefs:set', (_event, patch: unknown) => setDesktopPrefs(patch))
  handleTrusted('app:notify', (_event, title: unknown, body: unknown) =>
    notifyDesktop(String(title ?? ''), String(body ?? '')))
}

// ─── File System IPC ────────────────────────────────

/** 在 PATH 环境变量中查找可执行文件 */
function findProgramInPath(name: string): string | null {
  const isWindows = process.platform === 'win32'
  const paths = (process.env.PATH || '').split(isWindows ? ';' : ':')
  const exts = isWindows ? (process.env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(';') : ['']
  for (const dir of paths) {
    if (!dir) continue
    for (const ext of exts) {
      const full = join(dir, name + ext)
      if (existsSync(full)) return full
    }
  }
  return null
}

/** PATH 扫描预算（PERF-002）：补全发生在主进程事件循环上，System32 等大目录
 * 单次 readdir 10~50ms，PATH 含网络盘/失效 UNC 时同步枚举会阻塞到 SMB 超时
 * （可达数秒），期间所有终端的 PTY/SSH 数据转发全部冻结。改异步 readdir +
 * 总预算兜底：超时即返回已有候选。 */
const PATH_SCAN_BUDGET_MS = 200

/** 在 PATH 中查找以 prefix 开头的可执行文件，返回去重后的命令名列表（不含扩展名，用于 CMD 弱补全） */
async function findProgramsInPathByPrefix(prefix: string): Promise<string[]> {
  const isWindows = process.platform === 'win32'
  const paths = (process.env.PATH || '').split(isWindows ? ';' : ':')
  const exts = (process.env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(isWindows ? ';' : ':')
  const found = new Set<string>()
  const lowerPrefix = prefix.toLowerCase()
  const startedAt = Date.now()
  for (const dir of paths) {
    if (!dir) continue
    if (Date.now() - startedAt > PATH_SCAN_BUDGET_MS) break
    try {
      const entries = await readdir(dir, { withFileTypes: true })
      for (const entry of entries) {
        if (!entry.isFile() && !entry.isSymbolicLink()) continue  // 仅文件和符号链接
        const name = entry.name
        const dotIndex = name.lastIndexOf('.')
        if (dotIndex <= 0) continue
        const baseName = name.slice(0, dotIndex)
        const ext = name.slice(dotIndex)
        if (baseName.toLowerCase().startsWith(lowerPrefix) && exts.some((e) => e.toLowerCase() === ext.toLowerCase())) {
          found.add(baseName)
        }
      }
    } catch {
      // 忽略不可读目录
    }
  }
  return Array.from(found).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
}

/** 列出目录下以 prefix 开头的文件/目录名（用于 CMD 弱补全） */
async function listFilesByPrefix(dir: string, prefix: string): Promise<string[]> {
  try {
    const entries = await readdir(dir, { withFileTypes: true })
    const lowerPrefix = prefix.toLowerCase()
    const result: string[] = []
    for (const entry of entries) {
      if (entry.name.toLowerCase().startsWith(lowerPrefix)) {
        result.push(entry.name)
      }
    }
    return result.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
  } catch {
    return []
  }
}

/** 读取 Git for Windows 安装路径（优先 HKLM， fallback HKCU） */
function findGitBashFromRegistry(): string | null {
  if (process.platform !== 'win32') return null
  const queries = [
    'reg query "HKLM\\SOFTWARE\\GitForWindows" /v InstallPath',
    'reg query "HKCU\\SOFTWARE\\GitForWindows" /v InstallPath',
  ]
  for (const query of queries) {
    try {
      const output = execSync(query, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'ignore'], timeout: 3000 })
      const match = output.match(/InstallPath\s+REG_SZ\s+(.+)/)
      if (match) {
        const installPath = match[1].trim()
        const bashPath = join(installPath, 'bin', 'bash.exe')
        if (existsSync(bashPath)) return bashPath
      }
    } catch {
      // ignore registry errors
    }
  }
  return null
}

/** 敏感路径拒绝事件不在 catch 中改写成通用文案（透传 + warn）。 */
function rethrowIfSensitivePath(err: any): void {
  if (typeof err?.message === 'string' && err.message.startsWith('拒绝访问敏感路径')) {
    console.warn(`[fs] 敏感路径访问被拒绝: ${err.message}`)
    throw err
  }
}

function setupFSIPC(): void {
  // 读取目录内容
  handleTrusted('fs:readDir', async (_event, dirPath: string) => {
    try {
      await assertSafePath(dirPath)
      const entries = await readdir(dirPath, { withFileTypes: true })
      const items = await Promise.all(
        entries.map(async (entry) => {
          const fullPath = join(dirPath, entry.name)
          try {
            const s = await stat(fullPath)
            return {
              name: entry.name,
              path: fullPath,
              // 用 stat 而不是 Dirent：readdir 的 Dirent 不跟随链接，Windows 上
              // junction / reparse point 指向的目录（用户目录里的「开始」菜单、
              // Application Data、Cookies 等）会被判成文件，图标、进入、大小三处
              // 跟着错。stat 已经为取 size 调了，它跟随链接，判据就是对的。
              isDirectory: s.isDirectory(),
              size: s.size,
              modified: s.mtimeMs,
            }
          } catch {
            return {
              name: entry.name,
              path: fullPath,
              isDirectory: entry.isDirectory(),
              size: 0,
              modified: 0,
            }
          }
        })
      )
      // 排序: 文件夹在前，然后按名称
      items.sort((a, b) => {
        if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1
        return a.name.localeCompare(b.name)
      })
      return items
    } catch (err: any) {
      rethrowIfSensitivePath(err)
      throw new Error('读取目录失败，请检查路径是否存在或是否有访问权限')
    }
  })

  // 读取文件内容
  handleTrusted('fs:readFile', async (_event, filePath: string) => {
    try {
      await assertSafePath(filePath)
      const s = await stat(filePath)  // 10MB 大小上限
      if (s.size > 10 * 1024 * 1024) {
        throw new Error('文件过大（超过 10MB），无法读取')
      }
      const content = await readFile(filePath, 'utf-8')
      return content
    } catch (err: any) {
      rethrowIfSensitivePath(err)
      throw new Error(err?.message && err.message.includes('文件过大')
        ? err.message
        : '读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // 写入文件内容
  handleTrusted('fs:writeFile', async (_event, filePath: string, content: string) => {
    try {
      await assertSafePath(filePath)
      if (Buffer.byteLength(content, 'utf-8') > 50 * 1024 * 1024) {  // 50MB 大小上限
        throw new Error('内容过大（超过 50MB），无法写入')
      }
      await writeFile(filePath, content, 'utf-8')
    } catch (err: any) {
      rethrowIfSensitivePath(err)
      throw new Error(err?.message && err.message.includes('内容过大')
        ? err.message
        : '写入文件失败，请检查路径是否存在或是否有写入权限')
    }
  })

  // 本地图片预览的二进制读取（读前 stat 挡 >5MB 再转 base64；与 sftp:readBinary 对称）
  handleTrusted('fs:readBinary', async (_event, filePath: string) => {
    try {
      await assertSafePath(filePath)
      const s = await stat(filePath)
      if (s.size > 5 * 1024 * 1024) {
        throw new Error('图片过大（超过 5MB），无法预览')
      }
      const buf = await readFile(filePath) as Buffer
      return { base64: buf.toString('base64'), size: s.size }
    } catch (err: any) {
      rethrowIfSensitivePath(err)
      const msg = err?.message
      // 尺寸超限类友好文案原样透出；其余归并为通用读取失败
      throw new Error(msg && msg.includes('图片过大')
        ? msg
        : '读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // 为弹窗编辑器读取文件内容（带大小/二进制校验）
  handleTrusted('fs:readFileForEditor', async (_event, filePath: string) => {
    try {
      await assertSafePath(filePath)
      const stats = await stat(filePath)
      const size = stats.size
      if (size > 10 * 1024 * 1024) {
        throw new Error('文件过大（超过 10MB），无法打开')
      }
      const buf = await readFile(filePath) as Buffer
      const checkLen = Math.min(buf.length, 8192)
      for (let i = 0; i < checkLen; i++) {
        if (buf[i] === 0) throw new Error('二进制文件，无法预览编辑')
      }
      return {
        content: buf.toString('utf-8'),
        size,
        readOnly: size > 5 * 1024 * 1024,
      }
    } catch (err: any) {
      throw fsErrorWith(err, '读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // 选择文件(用于私钥等)
  handleTrusted('fs:selectFile', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
    })
    if (result.canceled) return null
    const filePath = result.filePaths[0]
    try { allowDialogKeyPath(await realpath(filePath)) } catch { /* 路径消失则跳过白名单 */ }
    return filePath
  })

  // 选择背景图片并保存到 userData/backgrounds，返回文件名
  handleTrusted('fs:selectImage', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
      // 白名单只有 IMAGE_MIME_MAP 一处；对话框 filters 直接派生（避免两处登记分叉）
      filters: [{ name: 'Images', extensions: Object.keys(IMAGE_MIME_MAP).map(e => e.slice(1)) }],
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const filePath = result.filePaths[0]
    try {
      await assertSafePath(filePath)
      const stats = await stat(filePath)
      const MAX_SIZE = 15 * 1024 * 1024
      if (stats.size > MAX_SIZE) {
        throw new Error('图片过大（超过 15MB），请选择更小的图片')
      }
      const origExt = extname(filePath).toLowerCase()
      // 不认识的扩展名当场拒绝（避免扩展名与 mime 不匹配导致背景丢失）
      const declaredMime = IMAGE_MIME_MAP[origExt]
      if (!declaredMime) {
        throw new Error(`不支持的图片格式 ${origExt || '（无扩展名）'}，请选择 ${Object.keys(IMAGE_MIME_MAP).join(' / ')}`)
      }
      // finalExt/mime 在压缩分支可能被重写，确保扩展名与实际字节流一致
      let finalExt = origExt
      let mime = declaredMime
      const stripDot = (e: string): string => (e.startsWith('.') ? e.slice(1) : e)
      const bgDir = backgroundDir()
      await mkdir(bgDir, { recursive: true })
      let finalBuf: Buffer = await readFile(filePath)
      // 对超过阈值的大图进行缩放压缩，避免 base64/IPC 与 CSS 渲染压力过大
      const COMPRESS_THRESHOLD = 2 * 1024 * 1024
      const COMPRESS_MAX_WIDTH = 1920
      if (stats.size > COMPRESS_THRESHOLD) {
        const image = nativeImage.createFromPath(filePath)
        if (!image.isEmpty()) {
          const { width } = image.getSize()
          if (width > COMPRESS_MAX_WIDTH) {
            const resized = image.resize({ width: COMPRESS_MAX_WIDTH, quality: 'good' })
            if (origExt === '.png') {
              // PNG 保留 PNG 格式以保留透明度
              finalBuf = resized.toPNG() as Buffer
              finalExt = '.png'
              mime = 'image/png'
            } else {
              // 其他格式统一转为 JPEG（体积更小），同步更新扩展名与 mime 避免不匹配
              finalBuf = resized.toJPEG(85) as Buffer
              finalExt = '.jpg'
              mime = 'image/jpeg'
            }
          }
        }
      }
      const fileName = `${BG_FILE_PREFIX}${stripDot(finalExt)}`
      const targetPath = join(bgDir, fileName)
      await writeFile(targetPath, finalBuf)
      // 新文件落盘成功后再清理旧背景文件（不同扩展名残留）：清理若先于写入，
      // writeFile 失败（磁盘满等）会把旧图删掉而新图没写成，设置仍指向已不存在的
      // 旧文件名，重启后背景静默消失。
      try {
        const existing = await readdir(bgDir)
        for (const f of existing) {
          if (f.startsWith(BG_FILE_PREFIX) && f !== fileName) {
            await rm(join(bgDir, f))
          }
        }
      } catch { /* 目录读取失败忽略 */ }
      return { fileName, mime, name: basename(filePath) }
    } catch (err: any) {
      throw fsErrorWith(err, '读取图片失败')
    }
  })

  // 校验背景图文件名：必须为基础名、以 background. 开头、扩展名在图片白名单内、
  // 在 backgrounds 目录内
  function resolveBackgroundPath(fileName: string): string {
    const bgDir = backgroundDir()
    const base = basename(fileName)
    if (!base.startsWith(BG_FILE_PREFIX)) {
      throw new Error('无效的背景图文件名')
    }
    // 扩展名必须在 MIME 白名单内：不校验的话，渲染进程可通过其他 IPC（userData
    // 不在敏感路径表）写入 background.html 之类的文件，这里会把它当 image/png
    // 读成 data URL 交给渲染进程渲染
    const ext = extname(base).toLowerCase()
    if (!IMAGE_MIME_MAP[ext]) {
      throw new Error('不支持的背景图格式')
    }
    const filePath = join(bgDir, base)
    // 路径遍历二次校验：确保解析后的路径仍在 bgDir 内
    if (!filePath.toLowerCase().startsWith(bgDir.toLowerCase() + '\\') &&
        !filePath.toLowerCase().startsWith(bgDir.toLowerCase() + '/')) {
      throw new Error('非法背景图路径')
    }
    return filePath
  }

  // 从 userData/backgrounds 读取背景图为 data URL
  handleTrusted('fs:loadBackgroundImage', async (_, fileName: string) => {
    try {
      const filePath = resolveBackgroundPath(fileName)
      await assertSafePath(filePath)
      const ext = extname(filePath).toLowerCase()
      const mime = IMAGE_MIME_MAP[ext] || 'image/png'
      const buf = await readFile(filePath)
      // 与 selectImage 的 15MB 写入上限同口径：防止 userData/backgrounds 里的
      // 文件被其他路径替换成超大文件后整读进内存再 base64（体积再涨 1/3）打爆 IPC
      if (buf.length > 15 * 1024 * 1024) {
        throw new Error('背景图文件过大')
      }
      return `data:${mime};base64,${buf.toString('base64')}`
    } catch (err: any) {
      throw fsErrorWith(err, '加载背景图失败')
    }
  })

  // 删除 userData/backgrounds 中的背景图文件
  handleTrusted('fs:deleteBackgroundImage', async (_, fileName: string) => {
    try {
      const filePath = resolveBackgroundPath(fileName)
      await assertSafePath(filePath)
      if (existsSync(filePath)) {
        await rm(filePath)
      }
      return true
    } catch (err: any) {
      throw fsErrorWith(err, '删除背景图失败')
    }
  })

  handleTrusted('fs:getHomePath', () => homedir())

  // 获取可用盘符（Windows 返回 ['C:\\', 'D:\\', ...]，其他平台返回 []）
  handleTrusted('fs:getDrives', async () => {
    if (process.platform !== 'win32') return []
    try {
      const { promisify } = await import('node:util')
      // 复用模块级已静态导入的 execFile（line 8），不再重复动态 import
      const drivesExecFileAsync = promisify(execFile)
      // 使用 wmic 获取本地磁盘盘符列表
      const { stdout } = await drivesExecFileAsync('wmic', ['logicaldisk', 'get', 'caption'])
      const drives = stdout
        .split('\n')
        .slice(1) // 跳过表头 Caption
        .map(line => line.trim())
        .filter(line => /^[A-Z]:$/.test(line))
        .map(letter => letter + '\\')
      return drives
    } catch {
      // wmic 不可用时回退：检测 C 到 Z 的可访问性
      const { accessSync, constants } = await import('node:fs')
      const drives: string[] = []
      for (let i = 67; i <= 90; i++) { // C=67 ... Z=90
        const drive = String.fromCharCode(i) + ':\\'
        try {
          accessSync(drive, constants.R_OK)
          drives.push(drive)
        } catch { /* 跳过不可访问盘符 */ }
      }
      return drives
    }
  })

  handleTrusted('fs:mkdir', async (_event, dirPath: string) => {
    await assertSafePath(dirPath)
    await mkdir(dirPath, { recursive: true })
  })

  // 重命名
  handleTrusted('fs:rename', async (_event, oldPath: string, newPath: string) => {
    await assertSafePath(oldPath)
    await assertSafePath(newPath)
    await rename(oldPath, newPath)
  })

  // 递归复制文件/目录（10万文件 / 10GB 上限；收集阶段实时判限）
  const MAX_COPY_FILES = 100_000
  const MAX_COPY_BYTES = 10 * 1024 * 1024 * 1024
  handleTrusted('fs:copy', async (_event, srcPath: string, destPath: string, transferId: string) => {
    await assertSafePath(srcPath)
    await assertSafePath(destPath)

    // 目标位于源内部 → 无限递归
    if (isCopyTargetSelfNested(srcPath, destPath)) {
      throw new Error('目标文件夹位于被复制项内部（或与源同一路径），已取消')
    }

    const copyState = beginCopy(transferId)  // 协作式取消

    // 收集所有文件并统计总大小
    const files: Array<{ src: string; dest: string; size: number }> = []
    let collectedBytes = 0
    async function collect(src: string, dest: string, isRoot: boolean): Promise<void> {
      if (copyState.cancelled) throw new Error('复制已取消')
      // lstat 不跟随符号链接（避免 ~/.ssh 等内容被复制泄露）
      const st = isRoot
        ? await stat(src)
        : await lstat(src).catch((e) => {
            console.warn(`[fs:copy] 跳过无法访问的条目: ${src} (${e?.message || e})`)
            return null
          })
      if (!st) return
      if (!isRoot && st.isSymbolicLink()) {
        console.warn(`[fs:copy] 跳过符号链接（防止泄露链接目标内容）: ${src}`)
        return
      }
      if (st.isFile()) {
        files.push({ src, dest, size: st.size })
        collectedBytes += st.size
        if (files.length > MAX_COPY_FILES) {
          throw new Error(`文件数过多（已超过 ${MAX_COPY_FILES}），请分批复制或用远端命令处理`)
        }
        if (collectedBytes > MAX_COPY_BYTES) {
          throw new Error(`总大小超过 10GB 上限，请分批复制或用远端命令处理`)
        }
      } else if (st.isDirectory()) {
        await mkdir(dest, { recursive: true })
        const entries = await readdir(src)
        for (const entry of entries) {
          await collect(join(src, entry), join(dest, entry), false)
        }
      }
    }
    try {
      await collect(srcPath, destPath, true)

      const total = collectedBytes
      let transferred = 0
      let lastReport = 0
      const safeSendProgress = (data: unknown): void => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          try { mainWindow.webContents.send('sftp:progress', data) } catch { /* 窗口已销毁等场景忽略 */ }
        }
      }
      const report = () => {
        const now = Date.now()
        if (now - lastReport > 100) {
          lastReport = now
          safeSendProgress({ id: transferId, transferred, total })
        }
      }

      // PERF-004：有界并发复制 — 原实现逐文件串行 await（每文件 lstat+mkdir+
      // copyFile 三次串行系统调用往返，Windows 单次约 5~15ms），海量小文件耗时
      // 成倍放大。目标目录 mkdir 去重，8 路并发 copyFile；取消检查、symlink
      // 目标防护与进度上报粒度保持不变，首个错误即停（与原串行语义一致）。
      const COPY_CONCURRENCY = 8
      const dirsDone = new Set<string>()
      let nextIdx = 0
      let firstError: Error | null = null
      const worker = async (): Promise<void> => {
        while (!firstError && !copyState.cancelled) {
          const i = nextIdx++
          if (i >= files.length) return
          const f = files[i]
          try {
            const destErr = rejectSymlinkTarget(f.dest, await lstat(f.dest).catch(() => null))
            if (destErr) throw new Error(destErr)
            const dir = dirname(f.dest)
            if (!dirsDone.has(dir)) {
              await mkdir(dir, { recursive: true })
              dirsDone.add(dir)
            }
            await copyFile(f.src, f.dest)
          } catch (err) {
            firstError = firstError ?? (err instanceof Error ? err : new Error(String(err)))
            return
          }
          transferred += f.size
          report()
        }
      }
      await Promise.all(Array.from({ length: Math.min(COPY_CONCURRENCY, files.length) }, () => worker()))
      if (firstError) throw firstError
      if (copyState.cancelled) throw new Error('复制已取消')
      safeSendProgress({ id: transferId, transferred: total, total })
    } finally {
      endCopy(transferId)
    }
  })

  // 移到回收站
  handleTrusted('fs:trash', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    await shell.trashItem(filePath)
  })

  // 路径拼接（主进程使用 Node path，避免渲染进程引入 polyfill）
  handleTrusted('fs:pathJoin', async (_event, ...segments: string[]) => {
    return join(...segments)
  })

  handleTrusted('fs:exists', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    return existsSync(filePath)
  })

  // 获取文件/目录信息（用于判断是文件还是目录）
  handleTrusted('fs:stat', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    const s = await stat(filePath)
    return { isDirectory: s.isDirectory(), isFile: s.isFile(), size: s.size, modified: s.mtimeMs }
  })

  // 按环境变量 PATH 查找可执行文件；对 bash 额外读取 Git for Windows 注册表
  handleTrusted('fs:resolveProgram', (_event, name: string) => {
    if (name === 'bash') {
      const gitBash = findGitBashFromRegistry()
      if (gitBash) return gitBash
    }
    return findProgramInPath(name)
  })

  // 本地 ZIP 打包：sources 为本地路径数组，输出到 localZipPath
  // Windows 使用 PowerShell Compress-Archive，Linux/macOS 使用 zip 命令
  handleTrusted('fs:zipLocal', async (_event, sources: string[], localZipPath: string) => {
    if (!sources || sources.length === 0) throw new Error('请至少选择一个要压缩的文件/目录')
    for (const s of sources) await assertSafePath(s)
    await assertSafePath(localZipPath)

    // E-5 修复：打包前 lstat 递归扫描源树 — 发现任何符号链接直接拒绝
    // （fail-closed）。Compress-Archive / zip 都会跟随符号链接，会把链接
    // 目标内容（如 ~/.ssh）打包泄露。Windows 与非 Windows 分支共用此校验
    async function assertNoSymlinks(root: string): Promise<void> {
      const st = await lstat(root)
      if (st.isSymbolicLink()) {
        throw new Error(`源路径为符号链接，包含符号链接，已拒绝打包: ${root}`)
      }
      if (!st.isDirectory()) return
      const entries = await readdir(root, { withFileTypes: true })
      for (const e of entries) {
        if (e.isSymbolicLink()) {
          throw new Error(`源树包含符号链接，已拒绝打包: ${join(root, e.name)}`)
        }
        if (e.isDirectory()) {
          await assertNoSymlinks(join(root, e.name))
        }
      }
    }
    for (const s of sources) {
      await assertNoSymlinks(s)
    }

    // 确保输出目录存在
    const outDir = dirname(localZipPath)
    await mkdir(outDir, { recursive: true })

    const isWin = process.platform === 'win32'
    if (isWin) {
      // PowerShell Compress-Archive：-Path 接受多个路径，-DestinationPath 输出 zip，-Force 覆盖
      const psSources = sources.map(s => `'${s.replace(/'/g, "''")}'`).join(',')
      const psDest = `'${localZipPath.replace(/'/g, "''")}'`
      const psCmd = `Compress-Archive -Path ${psSources} -DestinationPath ${psDest} -Force`
      await execFileAsync('powershell', ['-NoProfile', '-Command', psCmd], 300)
    } else {
      // Linux/macOS: zip -r -y <zip> <src1> <src2> ...
      const args = ['-r', '-y', localZipPath, ...sources]
      await execFileAsync('zip', args, 300)
    }
  })

  // 本地 ZIP 解压：localZipPath 解压到 localDestDir
  handleTrusted('fs:unzipLocal', async (_event, localZipPath: string, localDestDir: string) => {
    await assertSafePath(localZipPath)
    await assertSafePath(localDestDir)
    await mkdir(localDestDir, { recursive: true })

    const isWin = process.platform === 'win32'
    if (isWin) {
      const psZip = `'${localZipPath.replace(/'/g, "''")}'`
      const psDest = `'${localDestDir.replace(/'/g, "''")}'`
      const psCmd = `Expand-Archive -Path ${psZip} -DestinationPath ${psDest} -Force`
      await execFileAsync('powershell', ['-NoProfile', '-Command', psCmd], 300)
    } else {
      // -o 覆盖不询问，-q 安静，-d 指定目录
      await execFileAsync('unzip', ['-o', '-q', localZipPath, '-d', localDestDir], 300)
    }
  })
}

/** execFile 的 Promise 化封装，带超时 */
function execFileAsync(cmd: string, args: string[], timeoutSec: number): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = execFile(cmd, args, { timeout: timeoutSec * 1000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        const msg = (stderr || stdout || err.message || '').toString().trim()
        reject(new Error(`本地压缩/解压失败: ${msg || '未知错误'}`))
      } else {
        resolve({ stdout: stdout?.toString() || '', stderr: stderr?.toString() || '' })
      }
    })
    // 防止未捕获的 error 事件
    child.on('error', () => {
      reject(new Error(`无法启动命令: ${cmd}（请确认系统已安装该工具）`))
    })
  })
}

// ─── PTY IPC ────────────────────────────────────────
function setupPtyIPC(): void {
  ptyManager = new PtyManager()

  handleTrusted('pty:create', (_event, opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => {
    return ptyManager!.create(opts)
  })

  handleTrusted('pty:write', (_event, id: string, data: string) => {
    ptyManager!.write(id, data)
  })

  handleTrusted('pty:resize', (_event, id: string, cols: number, rows: number) => {
    // 与 pty:create / ssh:connect / ssh:reconnect 同一把钳子：resize 是这四条里
    // 唯一还直传渲染层数值的一条，非法 cols/rows 会直接进 node-pty 的 ioctl。
    ptyManager!.resize(id, clampPtyDimension(cols, 80, 1, 1000), clampPtyDimension(rows, 24, 1, 500))
  })

  handleTrusted('pty:destroy', (_event, id: string) => {
    ptyManager!.destroy(id)
  })

  // 本地终端当前工作目录（「问 AI」终端上下文采集）
  handleTrusted('pty:getCwd', (_event, id: string) => {
    return ptyManager!.getCwd(id) ?? ''
  })

  // 本地终端命令补全：在指定 cwd 执行补全
  // Windows PowerShell：使用 [System.Management.Automation.CommandCompletion]::CompleteInput
  // macOS/Linux bash：使用 compgen（识别 function/alias/command/file）
  handleTrusted('pty:completeCommand', (_event, opts: { ptyId: string; cmd: string; prefix: string; cwd: string; isCommand: boolean; prevSegments: string[]; shell?: string }) => {
    // cwd 须过 assertSafePath；拒绝时按「无候选」收尾（渲染层空串当无补全）
    return new Promise<string>(async (resolve) => {
      const { ptyId, cmd, prefix, cwd, isCommand, prevSegments, shell } = opts
      const isPowerShell = process.platform === 'win32' && (!shell || /powershell|pwsh/i.test(shell))
      const isCmd = process.platform === 'win32' && !!shell && /cmd/i.test(shell)
      // 若前端未提供 cwd，回退到 PTY 会话的初始工作目录
      const actualCwd: string = cwd || ptyManager!.getCwd(ptyId) || process.cwd()
      try {
        await assertSafePath(actualCwd)
      } catch {
        resolve('')
        return
      }

      if (isCmd) {
        // CMD 弱补全：命令名在 PATH 中查找，参数仅做路径/文件名补全
        if (isCommand) {
          const cmds = await findProgramsInPathByPrefix(prefix)
          resolve(cmds.map((c) => `cmd:${c}`).join('\n'))
        } else {
          const sepIndex = Math.max(prefix.lastIndexOf('\\'), prefix.lastIndexOf('/'))
          let dir = actualCwd
          let filePrefix = prefix
          if (sepIndex >= 0) {
            dir = pathResolve(dir, prefix.slice(0, sepIndex))
            filePrefix = prefix.slice(sepIndex + 1)
          }
          try {
            // prefix 里的 `..` 会把枚举目录解析到已放行的 actualCwd 之外，过同一道门
            await assertSafePath(dir)
          } catch {
            resolve('')
            return
          }
          const files = await listFilesByPrefix(dir, filePrefix)
          resolve(files.map((f) => `file:${f}`).join('\n'))
        }
        return
      }

      if (isPowerShell) {
        // PowerShell 补全：调用内置 CompleteInput API
        const safePrefix = prefix.replace(/'/g, "''")
        const line = isCommand
          ? `${safePrefix}`
          : `${prevSegments?.join(' ') || ''} ${safePrefix}`.trim()
        const psScript = `
$ErrorActionPreference = 'SilentlyContinue'
$line = @'
${line.replace(/@/g, '@' + '{')}}
'@
$cursor = $line.Length
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput($line, [ref]$tokens, [ref]$errors) | Out-Null
$lastToken = $tokens | Where-Object { $_.Extent.EndOffset -le $cursor } | Select-Object -Last 1
$ast = $tokens | Where-Object { $_.Kind -eq 'CommandName' -or $_.Kind -eq 'CommandArgument' -or $_.Kind -eq 'StringExpandable' -or $_.Kind -eq 'StringLiteral' } | Select-Object -Last 1
$wordToComplete = ''
if ($ast) { $wordToComplete = $ast.Text }
$result = [System.Management.Automation.CommandCompletion]::CompleteInput($line, $cursor, @{})
if ($result.CompletionMatches) {
  foreach ($m in $result.CompletionMatches) {
    $t = switch ($m.ResultType) { 'ParameterName' { 'arg' } 'ParameterValue' { 'arg' } 'Command' { 'cmd' } 'Method' { 'arg' } 'Property' { 'arg' } default { 'file' } }
    Write-Output ("$t:$($m.CompletionText)")
  }
}
`
        const ps = isPowerShell && /pwsh/i.test(shell || '') ? 'pwsh' : 'powershell'
        // 修复 7：改用 spawn 数组传参（不经 cmd.exe），避免 cmd 与 PowerShell 引号规则冲突
        // 导致含双引号的输入被错误解析；psScript 内容与超时/错误处理语义保持不变。
        // pwsh 路径解析沿用 fs:resolveProgram 的 findProgramInPath 逻辑。
        const psPath = findProgramInPath(ps) || ps
        const child = spawn(psPath, ['-NoProfile', '-NonInteractive', '-NoLogo', '-Command', psScript], {
          cwd: actualCwd,
          windowsHide: true,
        })
        let out = ''
        let settled = false
        let timer: NodeJS.Timeout | null = null
        const finish = (value: string) => {
          if (settled) return
          settled = true
          if (timer) clearTimeout(timer)
          resolve(value)
        }
        timer = setTimeout(() => {
          try { child.kill() } catch { /* ignore */ }
          finish('')
        }, 5000)
        child.stdout?.on('data', (data: Buffer) => {
          // 审查修复（P2）：stdout 累积上限 — 原实现无上限，补全命令在异常
          // 环境（如损坏的 PSModulePath 脚本刷屏）下可无限累积撑爆主进程内存
          if (out.length > 256 * 1024) {
            finish('')
            return
          }
          out += data.toString('utf-8')
        })
        child.stderr?.on('data', () => { /* 丢弃 stderr，保持原 exec 只取 stdout 的语义 */ })
        child.on('error', () => finish(''))
        child.on('close', (code) => {
          // 保持原语义：退出码非 0（或 spawn 失败）视为失败，返回空串
          finish(code === 0 ? out : '')
        })
        return
      }

      // macOS / Linux / Git Bash 补全
      const scriptBody = buildBashCompletionScript({ isCommand, cmd, prefix, prevSegments })
      execFile('bash', ['-lic', scriptBody], { cwd: actualCwd, timeout: 5000, encoding: 'utf8', windowsHide: true }, (err, stdout) => {
        if (err) resolve('')
        else resolve(stdout)
      })
    })
  })
}

// ─── SSH IPC ────────────────────────────────────────

/**
 * 凭据回填（修复：重启后"已保存的密码失效"）。
 *
 * 渲染进程 connections 副本不含明文凭据（ssh:listConnections 已剥离，见 E10），
 * 主进程重启后渲染层内存里也不再有任何密码副本 → ssh:connect 收到的 password
 * 恒为 undefined，ssh2 无任何可用认证方式即发起请求 → USERAUTH_FAILURE，前端按
 * authType='password' 呈现为"密码认证失败"。实际上密码一直完好保存在 Store 中，
 * 只是连接路径没有被读出来（对比：ssh:testConnection 有回填，所以测试连接能过）。
 *
 * 语义：渲染层传了非空值优先（弹窗里刚输入/未保存的场景），否则用 Store 中已保存
 * 的凭据；认证方式与磁盘记录不一致时不用（用户正在切换认证方式）。
 */
async function resolveCredentials(config: any): Promise<any> {
  if (!config || !config.id) return config
  let stored: SSHConnectionRecord | null = null
  try {
    stored = await sshStore!.get(config.id)
  } catch (e) {
    console.warn('[ssh] 读取已保存凭据失败，本次连接仅用渲染层传入值:', e)
    return config
  }
  if (!stored || stored.authType !== config.authType) return config
  if (!config.password && stored.password) config.password = stored.password
  if (!config.passphrase && stored.passphrase) config.passphrase = stored.passphrase
  return config
}

/**
 * 剥离记录中的明文凭据，仅保留布尔标志（与 ssh:listConnections 同语义）。
 *
 * 修复 E10 的残留缺口：ssh:saveConnection / ssh:duplicateConnection 原样返回 Store
 * 记录（含解密后的 password/passphrase）给渲染进程，既违背"凭据不出主进程"的约定，
 * 也让"同会话内连接能成功"依赖渲染层内存里的密码副本 —— 这正是上面这个 bug
 * 只在重启后才暴露的原因。
 */
function stripCredentials(rec: SSHConnectionRecord): SSHConnectionRecord {
  const { password, passphrase, ...rest } = rec
  return { ...rest, hasPassword: !!password, hasPassphrase: !!passphrase } as SSHConnectionRecord
}

function setupSSHIPC(): void {
  sshManager = new SSHManager()
  sshStore = new SSHStore()
  // 隧道：复用 sshStore 取凭据 + sshManager.openRawClient 建 TOFU 一致的裸连接
  tunnelStore = new TunnelStore()
  tunnelManager = new TunnelManager()
  ghHosts = new GhHostsService()
  githubAccel = new GithubAccelService(ghHosts)
  ghDownload = new GhDownloadManager()
  gitRepos = new GitReposService()
  ghDownload.setSender((data) => {
    // 窗口销毁后 webContents.send 会抛，加守卫（与 gateway:status 同一口径）
    if (mainWindow && !mainWindow.isDestroyed()) {
      try { mainWindow.webContents.send('ghaccel:download', data) } catch { /* 窗口已销毁 */ }
    }
  })
  // 镜像巡检：主进程持有定时器与网络探测，渲染层只订阅结果（标题栏红点不依赖页面挂载）
  githubAccel.startHealthWatch((status) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      try { mainWindow.webContents.send('ghaccel:health', status) } catch { /* 窗口已销毁 */ }
    }
  })
  gatewayStore = new GatewayStore()
  gatewayManager = new GatewayManager(gatewayStore, (channel, data) => {
    // 窗口销毁后 webContents.send 会抛，加守卫（与 sftp:progress 同一口径）
    if (mainWindow && !mainWindow.isDestroyed()) {
      try { mainWindow.webContents.send(channel, data) } catch { /* 窗口已销毁 */ }
    }
  })
  // 网关被主动断开 / 删除时联动停掉挂在它上面的隧道（被动断开由 onDead 拆，见 tunnel-manager）
  gatewayManager.setOnGatewayDown((id) => tunnelManager?.stopByGateway(id))
  tunnelManager.setTransportFactory(async (rule) => {
    if (rule.via === 'gateway') {
      // 隧道搭网关的共享会话：没连接就先在「安全接入」页连接，不在这里偷偷替用户建会话
      const t = rule.gatewayId ? gatewayManager!.transportOf(rule.gatewayId) : null
      if (!t) throw new Error('网关未连接，请先在「安全接入」页连接该网关')
      return t
    }
    // 凭据主进程自取（渲染层全程无明文）；连接配置被删 → 抛中文文案，落 status.message
    const config = await sshStore!.get(rule.connectionId)
    if (!config) throw new Error('连接配置已删除，请重建连接或删除本条隧道规则')
    return new SshTransport(await sshManager!.openRawClient(config))
  })
  // ssh2 懒加载预热：setup 阶段就发起，与随后的 createWindow + 首屏 transform 并行（~1.3s），
  // 用户点连接前已就位；失败不致命——doConnect/testConnection 内还有 await loadSsh2() 兜底路径。
  void loadSsh2().catch((err) => console.warn('[main] ssh2 预热失败（首次连接时将重试）:', err))

  // 重连修复：重连时主进程自取已保存的连接配置（safeStorage 解密后的凭据），
  // 渲染层无需（也不应）经 IPC 传递凭据
  sshManager.setConfigProvider(async (connectionId) => {
    return sshStore!.get(connectionId)
  })
  // 重连成功后更新最后连接时间（与 ssh:connect 成功路径同语义）
  sshManager.setOnReconnected((connectionId) => {
    sshStore!.touchLastConnected(connectionId).catch(() => { /* ignore */ })
  })

  handleTrusted('ssh:connect', async (_event, config: any) => {
    const { cols, rows, ...connConfig } = config
    // E20：cols/rows 来自渲染进程，复用 pty:create 同款钳制
    // （cols 1..1000 默认 80，rows 1..500 默认 24），防非法值
    const safeCols = clampPtyDimension(cols, 80, 1, 1000)
    const safeRows = clampPtyDimension(rows, 24, 1, 500)
    // 凭据回填：渲染层不持有明文凭据，缺省时以 Store 中已保存的为准
    const resolved = await resolveCredentials(connConfig)
    const result = await sshManager!.connect(resolved, safeCols, safeRows)
    // 连接成功后更新最后连接时间
    if (connConfig.id) {
      sshStore!.touchLastConnected(connConfig.id).catch(() => { /* ignore */ })
    }
    return result
  })

  handleTrusted('ssh:connectSftp', async (_event, config: any) => {
    // 凭据回填：SFTP 连接与终端连接同一条凭据来源规则
    const resolved = await resolveCredentials(config)
    const result = await sshManager!.connectSftp(resolved)
    if (config.id) {
      sshStore!.touchLastConnected(config.id).catch(() => { /* ignore */ })
    }
    return result
  })

  handleTrusted('ssh:write', (_event, id: string, data: string) => {
    sshManager!.write(id, data)
  })

  handleTrusted('ssh:resize', (_event, id: string, cols: number, rows: number) => {
    // 同 pty:resize：钳制补齐，别让四条路径里只有三条设防
    sshManager!.resize(id, clampPtyDimension(cols, 80, 1, 1000), clampPtyDimension(rows, 24, 1, 500))
  })

  handleTrusted('ssh:disconnect', (_event, id: string) => {
    sshManager!.disconnect(id)
  })

  // 重连修复：手动重连已断开的会话（重置计数，重新走 3 次 1s/3s/9s 退避）。
  // cols/rows 来自渲染层当前终端实际尺寸（与 ssh:connect 同款钳制防非法值）
  handleTrusted('ssh:reconnect', (_event, id: string, cols?: number, rows?: number) => {
    const safeCols = clampPtyDimension(cols, 80, 1, 1000)
    const safeRows = clampPtyDimension(rows, 24, 1, 500)
    sshManager!.reconnect(id, safeCols, safeRows)
  })

  handleTrusted('ssh:getCwd', (_event, id: string) => {
    return sshManager!.getCwd(id)
  })

  // C8：超时参数可选第三参 timeoutSec，默认 30，钳制 1..600
  handleTrusted('ssh:exec', async (_event, id: string, command: string, timeoutSec?: number) => {
    const t = Math.max(1, Math.min(600, Math.floor(Number(timeoutSec) || 30)))
    return sshManager!.execCommandWithExitCode(id, command, t)
  })

  // 终止远端进程：信号编号白名单 + PID 整型校验，防止命令注入 / 误操作系统进程
  handleTrusted('ssh:killProcess', async (_event, id: string, pid: number, signal?: number) => {
    if (typeof id !== 'string' || !id) throw new Error('INVALID_SESSION_ID')
    if (!Number.isInteger(pid) || pid <= 0) throw new Error('INVALID_PID')
    const ALLOWED = new Set([1, 2, 9, 15, 18, 19, 20])
    const sig = typeof signal === 'number' && Number.isInteger(signal) && ALLOWED.has(signal) ? signal : 15
    const cmd = `kill -${sig} ${pid}`
    const r = await sshManager!.execCommandWithExitCode(id, cmd, 10)
    return { code: r.exitCode ?? -1, stdout: r.stdout, stderr: r.stderr }
  })

  // 连接配置管理
  // 审查修复（P2 E10）：连接列表返回前剥离明文密码/passphrase —
  // 原实现直接返回 SSHStore 完整记录（含解密后的 password/passphrase），
  // 渲染进程通过 IPC 可见全部连接的明文凭据（XSS 或调试面板均可读取）。
  // 渲染层只用列表做展示（名称/host/port），连接时由 ssh:connect 从
  // 主进程 Store 直接读取凭据，无需经 IPC 传输明文。
  handleTrusted('ssh:listConnections', async () => {
    const records = await sshStore!.list()
    // hasPassword/hasPassphrase 是布尔标志（不含任何凭据内容），供编辑弹窗
    // 显示"已保存，留空则保持原密码"占位提示，避免用户误以为凭据丢失而反复重输
    return records.map(stripCredentials)
  })

  handleTrusted('ssh:saveConnection', async (_event, conn: SSHConnectionRecord) => {
    // 落库后同样剥离明文凭据再回传（渲染层此后一律经 ssh:connect 由主进程取凭据）
    return stripCredentials(await sshStore!.save(conn))
  })

  handleTrusted('ssh:deleteConnection', (_event, id: string) => {
    // 重连修复：删除连接配置时取消该配置名下所有会话的待执行重连
    // （已断开的会话之后不再自动重连，手动重试也无凭据可用）
    sshManager!.cancelReconnectsByConnection(id)
    return sshStore!.delete(id)
  })

  handleTrusted('ssh:duplicateConnection', async (_event, id: string) => {
    // 副本同样剥离明文凭据再回传（与 list/save 一致）
    return stripCredentials(await sshStore!.duplicate(id))
  })

  handleTrusted('ssh:testConnection', async (_event, config: any) => {
    // 修复：编辑已保存连接时渲染进程看不到密码（列表已剥离明文凭据），
    // 测试连接若要求重输密码就失去意义 —— 与 ssh:connect 共用同一套回填规则
    // （凭据不出主进程，不落日志）
    const resolved = await resolveCredentials(config)
    return sshManager!.testConnection(resolved)
  })

  handleTrusted('ssh:migrateLegacy', (_event, legacy: any[]) => {
    return sshStore!.migrateLegacy(legacy)
  })

  // ── SSH 隧道（端口转发）──
  // 设计要点（历史设计文档已退役，口径以 tunnel-manager.ts 为准）：
  // 规则持久化走 TunnelStore，运行态走 TunnelManager（独立 ssh2 client，与终端
  // 会话解耦）；凭据经 configProvider 主进程自取，渲染层全程无明文。
  handleTrusted('tunnel:list', async () => {
    return tunnelStore!.list()
  })

  handleTrusted('tunnel:save', async (_event, rule: TunnelRule) => {
    return tunnelStore!.save(rule)
  })

  handleTrusted('tunnel:delete', async (_event, id: string) => {
    // 运行中的隧道先停（幂等），再删规则
    if (tunnelManager) await tunnelManager.stop(id)
    return tunnelStore!.delete(id)
  })

  handleTrusted('tunnel:start', async (_event, id: string) => {
    const rule = await tunnelStore!.get(id)
    if (!rule) throw new Error(`隧道规则 ${id} 不存在`)
    return tunnelManager!.start(rule)
  })

  handleTrusted('tunnel:stop', async (_event, id: string) => {
    return tunnelManager!.stop(id)
  })

  handleTrusted('tunnel:statusAll', () => {
    return tunnelManager!.statusAll()
  })

  // 保存前预检：本机监听端口被占即时报（而非启动时才暴露 EADDRINUSE）
  handleTrusted('tunnel:checkPort', (_event, host: string, port: number) => {
    return tunnelManager!.checkPort(host, port)
  })

  // ── GitHub 加速（镜像套件，方案 A+）──
  // 设计：shared/github-accel-core.ts 头注。无服务器场景下的镜像方案：
  // 清单持久化走 github-accel.json；git/ssh 配置改动遵循"谁设置谁还原"。
  handleTrusted('ghaccel:getConfig', () => {
    return githubAccel!.getConfig()
  })

  handleTrusted('ghaccel:saveLists', (_event, proxies: any[], webMirrors: any[]) => {
    return githubAccel!.saveLists(proxies, webMirrors)
  })

  handleTrusted('ghaccel:probeAll', () => {
    return githubAccel!.probeAll()
  })

  handleTrusted('ghaccel:gitStatus', () => {
    return githubAccel!.gitStatus()
  })

  // 自检（只读：不写 git/ssh/hosts 配置，不弹 UAC）与巡检状态拉取（标题栏挂载时用）
  handleTrusted('ghaccel:selfCheck', () => {
    return githubAccel!.selfCheck()
  })

  handleTrusted('ghaccel:healthStatus', () => {
    return githubAccel!.healthStatus()
  })

  handleTrusted('ghaccel:gitEnable', (_event, prefix: string) => {
    return githubAccel!.gitEnable(prefix)
  })

  handleTrusted('ghaccel:gitDisable', () => {
    return githubAccel!.gitDisable()
  })

  handleTrusted('ghaccel:ssh443Status', () => {
    return githubAccel!.ssh443Status()
  })

  handleTrusted('ghaccel:ssh443Apply', () => {
    return githubAccel!.ssh443Apply()
  })

  handleTrusted('ghaccel:ssh443Remove', () => {
    return githubAccel!.ssh443Remove()
  })

  // 系统浏览器打开镜像站（仅限 http/https，防 file:// 等协议跳转）
  handleTrusted('ghaccel:openExternal', (_event, url: string) => {
    if (typeof url !== 'string' || !/^https:\/\//i.test(url)) throw new Error('仅允许打开 https 链接')
    return shell.openExternal(url)
  })

  // ── GitHub 加速下载器（M2：流式落盘 + 进度推送 + Range 续传） ──
  handleTrusted('ghaccel:downloadStart', (_event, url: string, opts?: { gunzip?: boolean }) => {
    return ghDownload!.start(url, opts)
  })

  handleTrusted('ghaccel:downloadCancel', (_event, id: string) => {
    ghDownload!.cancel(id)
  })

  handleTrusted('ghaccel:downloadList', () => {
    return ghDownload!.list()
  })

  // 打开已完成的下载文件所在位置（id 必须先查我们的状态表，不接受渲染层路径）
  handleTrusted('ghaccel:showItemInFolder', (_event, id: string) => {
    const st = ghDownload!.statusOf(id)
    if (!st || st.state !== 'done' || !st.path) throw new Error('下载任务不存在或未完成')
    shell.showItemInFolder(st.path)
  })

  // ── hosts 优选 IP（M3 增强模式：DoH 解析 + TCP 测速 + 托管块写入/移除） ──
  handleTrusted('ghaccel:hostsStatus', () => {
    return ghHosts!.status()
  })

  handleTrusted('ghaccel:hostsPick', () => {
    return ghHosts!.pick()
  })

  handleTrusted('ghaccel:hostsApply', async (_event, entries?: { domain: string; ip: string }[]) => {
    const status = await ghHosts!.apply(entries)
    // hosts 托管块参与巡检：写入/移除后让红点与 hosts 结论跟上，而不是等下一个 30 分钟周期
    githubAccel!.refreshHealthSoon()
    return status
  })

  handleTrusted('ghaccel:hostsRemove', async () => {
    const status = await ghHosts!.remove()
    githubAccel!.refreshHealthSoon()
    return status
  })

  // 总控选项：记住一键加速使用的镜像
  handleTrusted('ghaccel:saveOptions', (_event, patch: { lastProxyPrefix?: string }) => {
    return githubAccel!.saveOptions(patch)
  })

  // ── Git 仓库管理（本地注册表 + 本地 git 执行；远端路径表也在这里持久化，
  //    但远端命令由渲染层经 sshExec 下发，主进程不碰远端）──
  handleTrusted('gitrepos:getRegistry', () => {
    return gitRepos!.getRegistry()
  })

  handleTrusted('gitrepos:addLocal', (_event, path: string) => {
    return gitRepos!.addLocal(path)
  })

  // 仅从注册表移除，绝不触碰磁盘文件
  handleTrusted('gitrepos:removeLocal', (_event, path: string) => {
    return gitRepos!.removeLocal(path)
  })

  handleTrusted('gitrepos:setRemotePaths', (_event, connectionId: string, paths: string[]) => {
    return gitRepos!.setRemotePaths(connectionId, paths)
  })

  // 目录选择器（本机仓库登记入口；服务内不持有窗口引用，窗口销毁/不存在时直接返回 null）
  handleTrusted('gitrepos:pickDirectory', async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return null
    const res = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory'] })
    return res.canceled || !res.filePaths.length ? null : res.filePaths[0]
  })

  handleTrusted('gitrepos:statusAll', (_event, paths: string[]) => {
    return gitRepos!.statusAll(paths)
  })

  handleTrusted('gitrepos:fetchOne', (_event, path: string) => {
    return gitRepos!.fetchOne(path)
  })

  handleTrusted('gitrepos:pullOne', (_event, path: string) => {
    return gitRepos!.pullOne(path)
  })

  // ── V2 写操作（详情弹窗动作）：switch / commit / push。远程侧不走这里，
  //    由渲染层经 sshExec 下发并计入审计，与远程 pull 同口径 ──
  handleTrusted('gitrepos:switchBranch', (_event, path: string, branch: string) => {
    return gitRepos!.switchBranch(path, branch)
  })

  handleTrusted('gitrepos:commit', (_event, path: string, files: string[], msg: string) => {
    return gitRepos!.commitOne(path, files, msg)
  })

  // 选中文件对 HEAD 的 diff（只读，供提交信息 AI 归纳）
  handleTrusted('gitrepos:diffFiles', (_event, path: string, files: string[]) => {
    return gitRepos!.diffFiles(path, files)
  })

  handleTrusted('gitrepos:push', (_event, path: string) => {
    return gitRepos!.pushOne(path)
  })

  handleTrusted('gitrepos:detail', (_event, path: string) => {
    return gitRepos!.detail(path)
  })

  // 提交图谱（--graph --all 原始输出，解析在渲染层）
  handleTrusted('gitrepos:graphLog', (_event, path: string, limit: number) => {
    return gitRepos!.graphLog(path, limit)
  })

  // ── TLS 接入网关（安全接入）──
  // 设计要点（历史设计文档已退役，帧格式见 shared/gateway-mux.ts）：
  // 一条网关 = 一条共享会话（gateway-manager），隧道规则经 transportFactory 搭车；
  // 口令只在主进程解出，渲染层拿到的 list 里没有该字段。
  handleTrusted('gateway:list', async () => {
    return gatewayStore!.list()
  })

  handleTrusted('gateway:save', async (_event, rec: GatewayRecord) => {
    return gatewayStore!.save(rec)
  })

  handleTrusted('gateway:delete', async (_event, id: string) => {
    // 先停会话（含在途重连定时器），再删记录
    await gatewayManager!.drop(id)
    return gatewayStore!.delete(id)
  })

  handleTrusted('gateway:connect', async (_event, id: string) => {
    return gatewayManager!.connect(id)
  })

  handleTrusted('gateway:disconnect', async (_event, id: string) => {
    return gatewayManager!.disconnect(id)
  })

  handleTrusted('gateway:statusAll', () => {
    return gatewayManager!.statusAll()
  })

  // 「通道」页签：渲染层按需拉一次快照（不推送，通道生命周期太短，推一条废一条）
  handleTrusted('gateway:channels', (_event, id: string) => {
    return gatewayManager!.channelsOf(id)
  })

  // TOFU 指纹确认：UI 上确认过才写库，不符一律硬失败（不降级为警告）。
  // 校验走 manager —— 它手里才有「本次握手实际收到的那份」，渲染层自填的字符串不作数。
  handleTrusted('gateway:trustHostKey', async (_event, id: string, spki: string) => {
    return gatewayManager!.trustHostKey(id, spki)
  })

  // 用户取消确认：只清待记录，不落指纹；下次连接重新问一遍
  handleTrusted('gateway:hostKeyReject', (_event, id: string) => {
    return gatewayManager!.rejectHostKey(id)
  })

  // 后端端口查询（渲染进程启动时兜底获取，防止 backend:status 事件丢失）
  // ⚠️ 返回值只代表"已选定端口"，不代表"有人在那个端口监听" —— this.port 在 spawn
  // 之前就分配好了。渲染层可以用它做 URL 提示，但不能据此判定后端 ready（见 stores/app.ts）。
  handleTrusted('backend:getPort', () => {
    return backendManager?.getPort() ?? 0
  })

  // 后端真就绪（通过健康检查）才 resolve，并给出实际端口。
  // 渲染层的兜底路径必须等这个，而不是等一个非零端口号。
  // backendManager 在 whenReady 里晚于本 IPC 注册才赋值，故这里等它就位：
  // 直接 reject 会让首屏在"其实一切正常"的情况下闪一个 error 态。
  handleTrusted('backend:whenReady', async () => {
    const deadline = Date.now() + 15_000
    while (!backendManager) {
      if (Date.now() > deadline) throw new Error('后端管理器未初始化')
      await new Promise((r) => setTimeout(r, 50))
    }
    return backendManager.whenReady()
  })

  // 后端 API token 查询（渲染层据此向后端请求携带 X-API-Token；未启动时返回空字符串）
  handleTrusted('backend:getToken', () => {
    if (!backendManager || backendManager.getPort() === 0) return ''
    return backendManager.getApiToken()
  })

}

// ─── Skills IPC（转发到后端 HTTP API）─────────────────
async function callBackendApi(path: string, options?: RequestInit): Promise<any> {
  // 端口在 spawn 前就分配好了 —— 直接读 getPort() 会把渲染进程挂载时抢发的 skills IPC
  // 打到一个还没起监听的端口上（ECONNREFUSED）。等健康检查通过再转发，
  // 崩溃自动重启期间也是连上新实例后再转发。
  if (!backendManager) throw new Error('后端未就绪')
  const port = await backendManager.whenReady()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> | undefined || {}),
  }
  // 注入进程级 API token（后端通过 CKUN_API_TOKEN 环境变量持有同一值，
  // 校验 X-API-Token 头）；token 为空（后端未启动）时不加头。
  const apiToken = backendManager?.getApiToken() || ''
  if (apiToken) {
    headers['X-API-Token'] = apiToken
  }
  const res = await fetch(`http://127.0.0.1:${port}/api${path}`, {
    ...options,
    headers,
    // 审查 MINOR：原实现无超时 — 后端单次挂起（worker 阻塞、GIL 卡死等）会让
    // fetch 永久等待，对应 IPC handler 永不 resolve，渲染层 UI 永久转圈
    signal: (options?.signal as AbortSignal | undefined) ?? AbortSignal.timeout(30000),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let message = '后端服务处理失败，请稍后重试'
    try {
      const parsed = JSON.parse(text)
      if (parsed.detail && typeof parsed.detail === 'string' && !parsed.detail.includes('Traceback')) {
        message = parsed.detail
      }
    } catch { /* ignore */ }
    throw new Error(message)
  }
  return res.json()
}

function setupSkillsIPC(): void {
  handleTrusted('skills:list', async (_event, mode?: string) => {
    const qs = mode ? `?mode=${encodeURIComponent(mode)}` : ''
    return callBackendApi(`/skills${qs}`)
  })
  handleTrusted('skills:updateEnabled', async (_event, id: string, enabled: boolean, mode?: string) => {
    const qs = mode ? `?mode=${encodeURIComponent(mode)}` : ''
    return callBackendApi(`/skills/${id}/enabled${qs}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
  })
  handleTrusted('skills:delete', async (_event, id: string) => {
    return callBackendApi(`/skills/${id}`, {
      method: 'DELETE',
    })
  })
  handleTrusted('skills:syncBuiltin', async (_event, skills: any[]) => {
    return callBackendApi('/skills/sync-builtin', {
      method: 'POST',
      body: JSON.stringify({ skills }),
    })
  })
}

// ─── SFTP IPC ───────────────────────────────────────
function setupSftpIPC(): void {
  handleTrusted('sftp:open', (_event, sshSessionId: string) => {
    return sshManager!.openSftp(sshSessionId)
  })
  handleTrusted('sftp:close', (_event, sshSessionId: string) => {
    sshManager!.closeSftp(sshSessionId)
  })
  handleTrusted('sftp:list', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpList(sshSessionId, remotePath)
  })
  handleTrusted('sftp:mkdir', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpMkdir(sshSessionId, remotePath)
  })
  handleTrusted('sftp:mkdirRecursive', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpMkdirRecursive(sshSessionId, remotePath)
  })
  // 检查远程路径是否存在（用于 ZIP 覆盖确认等场景）
  handleTrusted('sftp:exists', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpExists(sshSessionId, remotePath)
  })
  // 查询单个远程路径的类型与大小（替代「sftpList 全量列目录探测 isDir」）
  handleTrusted('sftp:stat', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpStat(sshSessionId, remotePath)
  })
  handleTrusted('sftp:rename', (_event, sshSessionId: string, oldPath: string, newPath: string) => {
    return sshManager!.sftpRename(sshSessionId, oldPath, newPath)
  })
  handleTrusted('sftp:delete', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpDelete(sshSessionId, remotePath)
  })
  // 上传/下载：本地路径须过 assertSafePath（防止受控渲染进程读写系统位置）
  handleTrusted('sftp:upload', async (_event, transferId: string, sshSessionId: string, localPath: string, remotePath: string) => {
    await assertSafePath(localPath)
    return sshManager!.sftpUpload(transferId, sshSessionId, localPath, remotePath)
  })
  handleTrusted('sftp:download', async (_event, transferId: string, sshSessionId: string, remotePath: string, localPath: string) => {
    await assertSafePath(localPath)
    return sshManager!.sftpDownload(transferId, sshSessionId, remotePath, localPath)
  })
  handleTrusted('sftp:cancel', (_event, transferId: string) => {
    requestCopyCancel(transferId)  // 同时通知 fs:copy 和 sftp 传输表
    sshManager!.sftpCancel(transferId)
  })
  handleTrusted('sftp:readFile', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpReadFile(sshSessionId, remotePath)
  })
  // 远程图片预览的二进制读取（base64 + 5MB 上限在主进程侧拦截）
  handleTrusted('sftp:readBinary', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpReadBinary(sshSessionId, remotePath)
  })
  handleTrusted('sftp:writeFile', (_event, sshSessionId: string, remotePath: string, content: string) => {
    return sshManager!.sftpWriteFile(sshSessionId, remotePath, content)
  })
  // 远程 ZIP 打包：通过 SSH exec 执行 zip/python 命令
  handleTrusted('sftp:zipRemote', (_event, sshSessionId: string, sources: string[], remoteZipPath: string, cwd: string) => {
    return sshManager!.sftpZipRemote(sshSessionId, sources, remoteZipPath, cwd || '/')
  })
  // 远程 ZIP 解压
  handleTrusted('sftp:unzipRemote', (_event, sshSessionId: string, remoteZipPath: string, remoteDestDir: string) => {
    return sshManager!.sftpUnzipRemote(sshSessionId, remoteZipPath, remoteDestDir)
  })

  // 远程终端命令补全（bash -lic + OSC 7 cwd 跟踪）
  handleTrusted('ssh:completeCommand', async (_event, sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments: string[] }) => {
    const { prefix, isCommand, cmd, prevSegments } = opts
    const scriptBody = buildBashCompletionScript({ isCommand, cmd, prefix, prevSegments })
    const cwd = sshManager!.getCwd(sshSessionId)  // OSC 7 跟踪的交互式 cwd
    const fullScript = cwd ? `cd ${shellQuote(cwd)} && ${scriptBody}` : scriptBody
    return sshManager!.execCommand(sshSessionId, `bash -lic '${escapeBashScript(fullScript)}' 2>/dev/null`)
  })
}

// ─── App Lifecycle ──────────────────────────────────
// 只禁 GPU 的**磁盘缓存**：它不改变渲染路径、无性能代价，挡的是"GPU 进程写不进缓存目录"
// 那一类权限错误（沙箱内实测会出现）。
// 注意：不使用 setPath('userData') 以避免破坏数据库/SSH 连接等现有数据路径
app.commandLine.appendSwitch('disable-gpu-disk-cache')
// 本机 GPU 驱动与 Electron v35 不兼容：不禁用渲染，主进程在 GPU 初始化阶段就静默退出——
// 窗口不弹、一行 [main] 日志都没有、WER 与事件日志零记录（2026-10-14 21:00 实测）。
// 这三个开关由 76956f2/9ed8b12 引入，17:39 按 ADR 0001「已证伪」删掉后本机即起不来，现恢复。
// 代价是全量走 CPU 渲染；再想删它们，先补同机「加/不加开关」的真实应用对照，别用最小外壳。
// 回归守卫见 electron/main-gpu-switches.test.ts。
app.commandLine.appendSwitch('disable-gpu')
app.commandLine.appendSwitch('disable-gpu-compositing')
app.commandLine.appendSwitch('disable-software-rasterizer')
// ⚠️ 下面这条 disable-gpu-sandbox 不在那次批准的三项渲染开关之内 —— 删它等于放宽 GPU 子进程
// 的沙箱边界（安全姿态），必须单独裁决，别按 ADR 0001 的口径顺手带走。
app.commandLine.appendSwitch('disable-gpu-sandbox')

// 单实例锁。拿不到锁有两种成因，语义完全相反，必须分开处理：
//   1) 真有别的应用实例在跑 → 本实例退出，由已有实例接收 second-instance 并带到前台；
//   2) Windows 上 Chromium 建锁文件失败（实测 process_singleton_win.cc(468)
//      "Lock file can not be created! Error code: 5"）→ 其实没有第二个实例，直接退出
//      就是用户眼里的「双击没反应」。
// 判别用 userData 可写探针：写不进去 = 权限成因，降级继续启动（单实例保证失效，
// 但功能可用）；写得进去 = 认定真有实例，退出。不用进程枚举做判别 —— 那要在启动
// 关键路径上补一次 PowerShell CIM，而这条链路刚把 1.1~1.4s 的同步枚举挪出 whenReady。
// ⚠️ 改前的写法是「win32 打印继续启动，whenReady 里却 if (!gotSingleInstanceLock) return」，
// 两条分支互相矛盾：实测走到那句之后窗口和后端都不创建，进程挂在原地（.qoder-run/app-nosb.log：
// 该句 → EXIT=1；dev3.log 同形）。下面这两个变量是两条分支唯一的接缝，别再各写各的判据。
function userDataWritable(): boolean {
  try {
    const dir = app.getPath('userData')
    mkdirSync(dir, { recursive: true })
    const probe = join(dir, '.single-instance-probe')
    writeFileSync(probe, '1')
    unlinkSync(probe)
    return true
  } catch (e) {
    console.warn('[main] userData 目录不可写探针失败:', (e as Error).message)
    return false
  }
}

const gotSingleInstanceLock = app.requestSingleInstanceLock()
// 允许启动 = 拿到锁，或"拿不到锁但已证实是权限成因"的降级模式
const mayStart = gotSingleInstanceLock || (!userDataWritable() && process.platform === 'win32')
if (!gotSingleInstanceLock) {
  if (mayStart) {
    console.warn('[main] 单实例锁创建失败（userData 不可写，非第二实例），以降级模式继续启动：单实例保证失效')
  } else {
    console.warn('[main] 已有实例在运行，本实例退出')
    app.quit()
  }
}
if (mayStart) {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    }
  })
}

// 顶层异常兜底：记日志 + 尽力保活；60s 内 3 次 uncaught 视不可恢复主动退出。
// 注册早于 whenReady —— installCsp 等同步调用若抛异常，未注册 handler 会直接崩溃。
const UNCAUGHT_WINDOW_MS = 60_000
const uncaughtTimes: number[] = []
process.on('uncaughtException', (err) => {
  console.error('[main] uncaughtException:', err)
  const now = Date.now()
  while (uncaughtTimes.length > 0 && now - uncaughtTimes[0] > UNCAUGHT_WINDOW_MS) {
    uncaughtTimes.shift()
  }
  uncaughtTimes.push(now)
  if (uncaughtTimes.length >= 3) {
    console.error(`[main] ${UNCAUGHT_WINDOW_MS / 1000} 秒内 3 次未捕获异常，退出应用`)
    uncaughtTimes.length = 0
    app.quit()
  }
})
process.on('unhandledRejection', (reason) => {
  console.error('[main] unhandledRejection:', reason)  // 只记日志不退出
})

app.whenReady().then(async () => {
  // 双保险：非主实例（且非权限降级模式）不启动任何服务，quit 与 ready 的时序竞态下仍然安全。
  // 判据必须与上面 mayStart 同源，否则「打印了继续启动却什么都不做」的僵尸分支会回来。
  if (!mayStart) return
  perfMark('whenReady resolved')
  installCsp()
  setupWindowIPC()
  setupFSIPC()
  setupPtyIPC()
  setupSSHIPC()
  setupSkillsIPC()
  setupSftpIPC()
  perfMark('ipc setup done')
  // 启动性能优化：initDesktopPrefs 读盘不阻塞首屏；关窗隐藏按默认值（不隐藏）走，
  // prefs 就绪后 shouldHideOnClose / tray 的后续状态由模块内内存更新生效。
  initDesktopPrefs(() => mainWindow).catch(e => console.warn('[main] initDesktopPrefs 失败:', e))
  createWindow()
  perfMark('createWindow returned')

  // tar 形态没有安装器：首启自建桌面/开始菜单快捷方式（含 AUMID，Toast 归属
  // 注册依赖开始菜单项）。fire-and-forget，失败仅 warn，绝不占用启动关键路径。
  ensureShellShortcuts().catch((e) => console.warn('[main] ensureShellShortcuts:', e))

  // Start Electron bridge for backend → main process remote execution
  if (sshManager) {
    electronBridge = new ElectronBridge({ sshManager, sshStore: sshStore ?? undefined })
  }

  // Start Python backend
  backendManager = new BackendManager()
  // 桥接 env（token）提升到此处：WS 反向通道的重定向需要引用 token
  let bridgeEnv: { token: string } = { token: '' }
  // 创建（若尚未创建）并指向 WS 反向通道；后端首次启动与崩溃自动重启共用，
  // 避免首次 start 失败后重启时 client 仍为 null、通道整个会话不建立的漏洞
  const ensureBridgeWsClient = (port: number) => {
    if (!electronBridge || !bridgeEnv.token) return
    if (!bridgeWsClient) {
      const bridge = electronBridge
      bridgeWsClient = new BridgeWsClient(async (op, payload) =>
        bridge.dispatch(op as BridgeOp, payload),
      )
    }
    bridgeWsClient.setTarget(port, bridgeEnv.token)
  }
  // 后端状态变化（含崩溃自动重启）统一通过回调推送到前端
  backendManager.onStatusChange = (data) => {
    // 自动重启 ready（端口可能变化）时（重新）指向 WS 反向通道
    if (data.status === 'ready' && data.port) {
      ensureBridgeWsClient(data.port)
    }
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:status', data)
    }
  }
  try {
    if (electronBridge) {
      bridgeEnv = electronBridge.start()
    }
    const extraEnv: Record<string, string> = {}
    if (bridgeEnv.token) {
      extraEnv.ELECTRON_BRIDGE_TOKEN = bridgeEnv.token
    }
    const port = await backendManager.start(extraEnv)
    perfMark(`backend start resolved port=${port}`)

    // WS 反向通道：主进程出站拨号后端，定时任务命令的唯一链路（阶段 2 已移除 HTTP 端口桥）
    ensureBridgeWsClient(port)
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:status', { status: 'ready', port })
    }
  } catch (err) {
    console.error('[BackendManager] Failed to start Python backend:', err)
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:status', { status: 'error', error: String(err) })
    }
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
      return
    }
    // 隐藏到托盘后点 Dock 图标：窗口还在，只是不可见 —— 不 show 就是"点了没反应"
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) mainWindow.show()
  })
})

app.on('window-all-closed', () => {
  // macOS 关窗口不退出应用（Dock 保留，activate 重建窗口）；资源释放只在 before-quit 一处执行。
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  markQuitting()  // 防止 close 事件走隐藏分支
  ptyManager?.destroyAll()
  sshManager?.disconnectAll()
  tunnelManager?.stopAll()
  gatewayManager?.stopAll()
  githubAccel?.stopHealthWatch()
  backendManager?.stop()
  bridgeWsClient?.stop()
  electronBridge?.stop()
})
