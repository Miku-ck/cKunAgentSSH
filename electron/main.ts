import { app, BrowserWindow, ipcMain, shell, dialog, session, nativeImage } from 'electron'
import { join, normalize, resolve as pathResolve, dirname, basename, extname, sep as pathSep } from 'path'
import { is } from '@electron-toolkit/utils'
import { readdir, readFile, writeFile, stat, lstat, mkdir, rename, rm, realpath, copyFile } from 'fs/promises'
import { existsSync, readdirSync, mkdirSync, realpathSync } from 'fs'
import { homedir } from 'os'
import { execSync, execFile, spawn } from 'child_process'
import { BackendManager } from './backend-manager'
import { PtyManager, clampPtyDimension } from './pty-manager'
import { SSHManager } from './ssh-manager'
import { SSHError, toIpcTransport } from './ssh-error'
import { SSHStore, type SSHConnectionRecord } from './ssh-store'
import { ElectronBridge, BridgeOp } from './electron-bridge'
import { BridgeWsClient } from './bridge-ws-client'
import { assertSafePath, allowDialogKeyPath } from './utils/path-guard'

let mainWindow: BrowserWindow | null = null
let backendManager: BackendManager | null = null
let ptyManager: PtyManager | null = null
let sshManager: SSHManager | null = null
let sshStore: SSHStore | null = null
let electronBridge: ElectronBridge | null = null
let bridgeWsClient: BridgeWsClient | null = null

// 审查修复：IPC sender 校验 — 原实现所有 ipcMain.handle 均不校验请求来源，
// 任何受控渲染进程（XSS/外部 file:// 导航/未来新增窗口）都能直调
// ssh:connect、fs:*、backend:getToken 等危险 API。
// 本应用只有唯一主窗口（单实例锁 + 固定 renderer 入口），校验策略：
// 请求必须来自当前主窗口的 webContents。
function assertTrustedSender(event: Electron.IpcMainInvokeEvent): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    throw new Error('主窗口不存在，拒绝 IPC 请求')
  }
  if (event.sender !== mainWindow.webContents) {
    throw new Error(`拒绝来自非主窗口渲染进程的 IPC 请求: ${event.senderFrame?.url || 'unknown'}`)
  }
}

/**
 * 审查修复：带 sender 校验的 IPC handler 注册（见 assertTrustedSender）。
 * 用法：所有 ipcMain.handle 一律改走本函数，事件原样透传给业务 handler
 * （业务侧 `_event` 参数签名不受影响）。
 *
 * v2 改进：自动识别 SSHError 并序列化为 transport 格式透传 Electron IPC。
 * 原始 SSHError 经过 IPC 后会退化为普通 Error（丢失 name/code），
 * handleTrusted 在边界拦截并以 __sshError 哨兵字段携带完整错误信息，
 * 前端 extractSSHError()（src/ssh-error-types.ts）据此还原结构化错误。
 */
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

/** 把业务错误标准化为 IPC 透传格式：SSHError → 结构化 object；普通 Error → 同类 object *
 * 双重冗余设计确保跨 Electron 版本兼容：
 *   1. e.name = 'SSHError:<code>' — name 在所有 Electron 版都能透传（已验证 32+保真）
 *   2. e.__sshError = transport — 自定义属性（Electron 32+ 序列化保真性已改进，保留作为增强）
 *   前端 detectSSHError() 优先读 __sshError；缺失时回退解析 e.name 前缀 */
function normalizeIpcError(err: unknown): Error {
  if ((err as any)?.__sshError) {
    // 已是 transport（二次穿透防護）
    return err as Error
  }
  if (isSSHError(err)) {
    const transport = toIpcTransport(err as any)
    const e = new Error(transport.message) as Error & { __sshError?: any; name: string }
    e.name = `SSHError:${transport.code}`  // name 字段跨平台保真性最强
    e.__sshError = transport
    // 保留原始 SSHError 堆栈（指向 ssh-manager.ts 中的真实抛出点），
    // 而非指向 normalizeIpcError 本身，方便开发期诊断
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

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.webContents.setWindowOpenHandler((details) => {
    // M3 修复：details.url 可能是非标准格式（如 file://、about:blank、javascript:、
    // 自定义协议），new URL 对某些畸形 URL 会抛异常，导致整个处理器崩溃。
    // 用 try/catch 保护，无法解析的 URL 一律 deny，不再用 shell.openExternal 打开。
    let protocol = ''
    try {
      protocol = new URL(details.url).protocol
    } catch {
      // 畸形 URL：拒绝打开
      return { action: 'deny' }
    }
    if (protocol === 'http:' || protocol === 'https:') {
      shell.openExternal(details.url)
    }
    return { action: 'deny' }
  })

  // 审查 M-2：阻止主窗口被导航到任意外部地址（原实现完全没有 will-navigate 守卫，
  // <a href> 点击、location 赋值、XSS 均可把整个应用窗口劫持到外部站点）。
  // 只允许同源导航（dev 为 Vite URL，prod 为 file:// 本地文件），其余一律 preventDefault。
  const allowedOrigins = new Set<string>()
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    try { allowedOrigins.add(new URL(process.env['ELECTRON_RENDERER_URL']).origin) } catch { /* ignore */ }
  }
  mainWindow.webContents.on('will-navigate', (event, url) => {
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      event.preventDefault()
      return
    }
    // file:// 的 origin 是 "file://"，prod 模式允许（loadFile 加载的本地资源）
    const isAllowed =
      (is.dev && allowedOrigins.has(parsed.origin)) ||
      (!is.dev && parsed.protocol === 'file:') ||
      url === mainWindow?.webContents.getURL()
    if (!isAllowed) {
      event.preventDefault()
      // 外部 http(s) 链接交给系统浏览器，保持与 windowOpenHandler 一致的行为
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        shell.openExternal(url)
      }
    }
  })

  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

// D16 修复：CSP 响应头注入原注册在 createWindow() 内 — 窗口重建（macOS
// activate）会重复叠加 onHeadersReceived 回调。移到 whenReady 一次性执行，
// 模块级标志防重
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
}

// ─── File System IPC ────────────────────────────────
// 敏感路径拦截逻辑（HOME_TOPLEVEL_SENSITIVE / ANYWHERE_SENSITIVE / assertSafePath）
// 已迁至 ./utils/path-guard 并从此处导入，行为保持一致。

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

/** 在 PATH 中查找以 prefix 开头的可执行文件，返回去重后的命令名列表（不含扩展名，用于 CMD 弱补全） */
function findProgramsInPathByPrefix(prefix: string): string[] {
  // m5 修复：根据平台选择正确的 PATH 分隔符（Windows 用 ;，其他用 :）
  const isWindows = process.platform === 'win32'
  const paths = (process.env.PATH || '').split(isWindows ? ';' : ':')
  const exts = (process.env.PATHEXT || '.EXE;.CMD;.BAT;.COM').split(isWindows ? ';' : ':')
  const found = new Set<string>()
  for (const dir of paths) {
    if (!dir) continue
    try {
      const entries = readdirSync(dir, { withFileTypes: true })
      for (const entry of entries) {
        if (!entry.isFile() && !entry.isSymbolicLink()) continue
        const name = entry.name
        const dotIndex = name.lastIndexOf('.')
        if (dotIndex <= 0) continue
        const baseName = name.slice(0, dotIndex)
        const ext = name.slice(dotIndex)
        if (baseName.toLowerCase().startsWith(prefix.toLowerCase()) && exts.some((e) => e.toLowerCase() === ext.toLowerCase())) {
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
function listFilesByPrefix(dir: string, prefix: string): string[] {
  try {
    const entries = readdirSync(dir, { withFileTypes: true })
    const result: string[] = []
    for (const entry of entries) {
      if (entry.name.toLowerCase().startsWith(prefix.toLowerCase())) {
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
      // 审查 MINOR：原 execSync 无 timeout — 注册表被第三方 provider 锁定时 reg 进程
      // 会挂起，主进程（同步阻塞）随之整体冻死；3s 超时兜底
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
              isDirectory: entry.isDirectory(),
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
      throw new Error('读取目录失败，请检查路径是否存在或是否有访问权限')
    }
  })

  // 读取文件内容
  handleTrusted('fs:readFile', async (_event, filePath: string) => {
    try {
      await assertSafePath(filePath)
      // 审查 M-10：大小上限（与 fs:readFileForEditor 的 10MB 一致）。原实现无上限，
      // 渲染进程可让主进程把任意大的文件整体读入内存（字符串）再 IPC 传回，
      // 大文件直接打爆主/渲染两侧内存
      const s = await stat(filePath)
      if (s.size > 10 * 1024 * 1024) {
        throw new Error('文件过大（超过 10MB），无法读取')
      }
      const content = await readFile(filePath, 'utf-8')
      return content
    } catch (err: any) {
      throw new Error(err?.message && err.message.includes('文件过大')
        ? err.message
        : '读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // C9：读取文件为 base64（渲染层二进制资源上传/预览用）
  // assertSafePath + 10MB 上限，返回 { base64, size }
  handleTrusted('fs:readFileBase64', async (_event, filePath: string): Promise<{ base64: string; size: number }> => {
    await assertSafePath(filePath)
    const s = await stat(filePath)
    if (s.size > 10 * 1024 * 1024) {
      throw new Error('文件过大（超过 10MB），无法读取')
    }
    const buf = await readFile(filePath)
    return { base64: buf.toString('base64'), size: s.size }
  })

  // 写入文件内容
  handleTrusted('fs:writeFile', async (_event, filePath: string, content: string) => {
    try {
      await assertSafePath(filePath)
      // E21：写入内容大小上限（50MB）— 原实现无上限，渲染进程可传入超大
      // 字符串耗尽内存/磁盘
      if (Buffer.byteLength(content, 'utf-8') > 50 * 1024 * 1024) {
        throw new Error('内容过大（超过 50MB），无法写入')
      }
      await writeFile(filePath, content, 'utf-8')
    } catch (err: any) {
      throw new Error(err?.message && err.message.includes('内容过大')
        ? err.message
        : '写入文件失败，请检查路径是否存在或是否有写入权限')
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
      throw new Error(err.message || '读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // 选择文件夹
  handleTrusted('fs:selectFolder', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory'],
    })
    if (result.canceled) return null
    return result.filePaths[0]
  })

  // 选择文件(用于私钥等)
  handleTrusted('fs:selectFile', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
    })
    if (result.canceled) return null
    const filePath = result.filePaths[0]
    // 修复 1：把用户通过对话框选取的文件加入私钥白名单（realpath 后），
    // 使 ssh-manager 的 assertKeyFileSafe 放行该路径（支持符号链接场景）。
    try {
      allowDialogKeyPath(await realpath(filePath))
    } catch {
      // realpath 失败（如文件在对话框关闭后消失）：不加入白名单，选择结果仍返回
    }
    return filePath
  })

  // 选择背景图片并保存到 userData/backgrounds，返回文件名
  handleTrusted('fs:selectImage', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
      filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'] }],
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
      // finalExt/mime 在压缩分支可能被重写，确保扩展名与实际字节流一致
      let finalExt = origExt
      let mime = IMAGE_MIME_MAP[origExt] || 'image/png'
      const stripDot = (e: string): string => (e.startsWith('.') ? e.slice(1) : e)
      const bgDir = join(app.getPath('userData'), 'backgrounds')
      await mkdir(bgDir, { recursive: true })
      // 清理旧背景文件（不同扩展名残留），避免磁盘泄漏
      try {
        const existing = await readdir(bgDir)
        for (const f of existing) {
          if (f.startsWith('background.')) {
            await rm(join(bgDir, f))
          }
        }
      } catch { /* 目录读取失败忽略 */ }
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
      const fileName = `background.${stripDot(finalExt) || 'png'}`
      const targetPath = join(bgDir, fileName)
      await writeFile(targetPath, finalBuf)
      return { fileName, mime, name: basename(filePath) }
    } catch (err: any) {
      throw new Error(err.message || '读取图片失败')
    }
  })

  // 校验背景图文件名：必须为基础名、以 background. 开头、在 backgrounds 目录内
  function resolveBackgroundPath(fileName: string): string {
    const bgDir = join(app.getPath('userData'), 'backgrounds')
    const base = basename(fileName)
    if (!base.startsWith('background.')) {
      throw new Error('无效的背景图文件名')
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
      return `data:${mime};base64,${buf.toString('base64')}`
    } catch (err: any) {
      throw new Error(err.message || '加载背景图失败')
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
      throw new Error(err.message || '删除背景图失败')
    }
  })

  // 获取用户主目录
  handleTrusted('fs:getHomePath', () => homedir())

  // 获取可用盘符（Windows 返回 ['C:\\', 'D:\\', ...]，其他平台返回 []）
  handleTrusted('fs:getDrives', async () => {
    if (process.platform !== 'win32') return []
    try {
      const { execFile } = await import('node:child_process')
      const { promisify } = await import('node:util')
      // P3 修复：局部改名 drivesExecFileAsync — 原同名 const 遮蔽了模块级
      // execFileAsync（压缩/解压用 Promise 封装），消除变量遮蔽，行为不变
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

  // 创建目录
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

  // 递归复制文件/目录（同侧复制粘贴），通过 sftp:progress 上报进度
  handleTrusted('fs:copy', async (_event, srcPath: string, destPath: string, transferId: string) => {
    await assertSafePath(srcPath)
    await assertSafePath(destPath)

    // 收集所有文件并统计总大小
    const files: Array<{ src: string; dest: string; size: number }> = []
    async function collect(src: string, dest: string, isRoot: boolean): Promise<void> {
      // E-5 修复：递归条目用 lstat（不跟随符号链接）— 原实现 stat 跟随符号
      // 链接，源树中的符号链接（如指向 ~/.ssh）会被 copyFile 跟随复制出
      // 链接目标内容造成泄露。符号链接条目跳过并 warn；根节点仍可跟随
      // （根路径已经 assertSafePath 的 realpath 校验，不可能是指向敏感目录的链接）
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
      } else if (st.isDirectory()) {
        await mkdir(dest, { recursive: true })
        const entries = await readdir(src)
        for (const entry of entries) {
          await collect(join(src, entry), join(dest, entry), false)
        }
      }
    }
    await collect(srcPath, destPath, true)

    // 审查修复（P2）：复制总量上限 — 原实现无上限，递归收集百万文件的整棵树
    // 全部驻留内存（files 数组 + 逐文件 copyFile），可 OOM 崩主进程。
    // 10 万文件或 10GB 已远超手工复制场景，超出提示用 sftp/远端命令处理。
    const MAX_COPY_FILES = 100_000
    const MAX_COPY_BYTES = 10 * 1024 * 1024 * 1024
    if (files.length > MAX_COPY_FILES) {
      throw new Error(`文件数过多（${files.length}，上限 ${MAX_COPY_FILES}），请分批复制或用远端命令处理`)
    }
    const total = files.reduce((sum, f) => sum + f.size, 0)
    if (total > MAX_COPY_BYTES) {
      throw new Error(`总大小超过 10GB 上限，请分批复制或用远端命令处理`)
    }
    let transferred = 0
    let lastReport = 0
    // 修复 5：窗口销毁后 webContents.send 会抛异常，加守卫 + try/catch
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

    for (const f of files) {
      await mkdir(dirname(f.dest), { recursive: true })
      await copyFile(f.src, f.dest)
      transferred += f.size
      report()
    }
    safeSendProgress({ id: transferId, transferred: total, total })
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

  // 技能目录根路径：{userData}/skills
  handleTrusted('fs:getSkillsDir', async () => {
    const dir = join(app.getPath('userData'), 'skills')
    await mkdir(dir, { recursive: true })
    return dir
  })

  // 递归删除目录（用于卸载技能时清理资源）
  handleTrusted('fs:rmRF', async (_event, targetPath: string) => {
    try {
      // 安全：只允许删除技能目录下的路径，避免误删
      // 用 realpath 解析符号链接 + 严格边界检查，防止 `...\skills\..\..\Windows\System32` 路径遍历绕过
      const skillsRootPath = join(app.getPath('userData'), 'skills')
      // E18：skills 根不存在时无从谈起边界（realpath 会抛 ENOENT），直接 no-op 成功
      if (!existsSync(skillsRootPath)) return
      const skillsRoot = await realpath(skillsRootPath)
      let resolved: string
      try {
        resolved = await realpath(targetPath)
      } catch {
        // 目标不存在时回退到 normalize 的字面路径，但仍做边界检查
        resolved = normalize(targetPath)
      }
      // 严格边界：resolved 必须 === skillsRoot 或在 skillsRoot + 路径分隔符 之下
      const sep = process.platform === 'win32' ? '\\' : '/'
      const isInside = resolved === skillsRoot || resolved.startsWith(skillsRoot + sep)
      if (!isInside) {
        throw new Error('只允许删除技能目录下的文件')
      }
      await rm(resolved, { recursive: true, force: true })
    } catch (err: any) {
      throw new Error('删除失败，请检查文件是否存在或是否有权限')
    }
  })

  // 列出目录下所有文件（递归），返回相对路径列表，用于技能导出收集资源文件。
  // 仅用于小型目录（技能文件夹），不做剪枝。
  handleTrusted('fs:listFilesRecursive', async (_event, dirPath: string): Promise<string[]> => {
    try {
      await assertSafePath(dirPath)
      const result: string[] = []
      async function walk(dir: string, base: string): Promise<void> {
        const entries = await readdir(dir, { withFileTypes: true })
        for (const entry of entries) {
          const fullPath = join(dir, entry.name)
          const relPath = join(base, entry.name)
          if (entry.isDirectory()) {
            await walk(fullPath, relPath)
          } else {
            result.push(relPath)
          }
        }
      }
      await walk(dirPath, '')
      return result
    } catch (err: any) {
      throw new Error('读取文件列表失败，请检查目录是否存在或是否有访问权限')
    }
  })

  // 检查文件是否存在
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
  if (mainWindow) ptyManager.setWindow(mainWindow)

  handleTrusted('pty:create', (_event, opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => {
    return ptyManager!.create(opts)
  })

  handleTrusted('pty:write', (_event, id: string, data: string) => {
    ptyManager!.write(id, data)
  })

  handleTrusted('pty:resize', (_event, id: string, cols: number, rows: number) => {
    ptyManager!.resize(id, cols, rows)
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
    return new Promise<string>((resolve) => {
      const { ptyId, cmd, prefix, cwd, isCommand, prevSegments, shell } = opts
      const isPowerShell = process.platform === 'win32' && (!shell || /powershell|pwsh/i.test(shell))
      const isCmd = process.platform === 'win32' && !!shell && /cmd/i.test(shell)
      // 若前端未提供 cwd，回退到 PTY 会话的初始工作目录
      const actualCwd: string = cwd || ptyManager!.getCwd(ptyId) || process.cwd()

      if (isCmd) {
        // CMD 弱补全：命令名在 PATH 中查找，参数仅做路径/文件名补全
        if (isCommand) {
          const cmds = findProgramsInPathByPrefix(prefix)
          resolve(cmds.map((c) => `cmd:${c}`).join('\n'))
        } else {
          const sepIndex = Math.max(prefix.lastIndexOf('\\'), prefix.lastIndexOf('/'))
          let dir = actualCwd
          let filePrefix = prefix
          if (sepIndex >= 0) {
            dir = pathResolve(dir, prefix.slice(0, sepIndex))
            filePrefix = prefix.slice(sepIndex + 1)
          }
          const files = listFilesByPrefix(dir, filePrefix)
          resolve(files.map((f) => `file:${f}`).join('\n'))
        }
        return
      }

      if (isPowerShell) {
        // PowerShell 补全：调用内置 CompleteInput API
        const safePrefix = prefix.replace(/'/g, "''")
        const safeCmd = cmd.replace(/'/g, "''")
        const segmentsJson = JSON.stringify(prevSegments || [])
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
  if (mainWindow) sshManager.setWindow(mainWindow)

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
    sshManager!.resize(id, cols, rows)
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

  // 后端端口查询（渲染进程启动时兜底获取，防止 backend:status 事件丢失）
  handleTrusted('backend:getPort', () => {
    return backendManager?.getPort() ?? 0
  })

  // 后端 API token 查询（渲染层据此向后端请求携带 X-API-Token；未启动时返回空字符串）
  handleTrusted('backend:getToken', () => {
    if (!backendManager || backendManager.getPort() === 0) return ''
    return backendManager.getApiToken()
  })

}

// ─── Skills IPC（转发到后端 HTTP API）─────────────────
async function callBackendApi(path: string, options?: RequestInit): Promise<any> {
  const port = backendManager?.getPort() ?? 0
  if (!port) throw new Error('后端未就绪')
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
  /**
   * C-1：技能路径边界收容（渲染层技能 ZIP 解压写入用）。
   * 返回 skills 根的真实路径；调用方对 resolve 后的路径做
   * `=== skillsRoot || startsWith(skillsRoot + sep)` 断言，防 zip-slip 越界写。
   */
  function getSkillsRoot(): string {
    const dir = join(app.getPath('userData'), 'skills')
    // 根由 fs:getSkillsDir 保证存在；此处 mkdir 兜底（已存在时 no-op），
    // 避免 realpathSync 对不存在目录抛 ENOENT
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    return realpathSync(dir)
  }

  /** C-1：断言 p resolve 后位于 skills 根内（含根本身），否则抛"非法技能路径" */
  function assertSkillsPath(p: string): void {
    const skillsRoot = getSkillsRoot()
    const resolved = pathResolve(p)
    if (resolved !== skillsRoot && !resolved.startsWith(skillsRoot + pathSep)) {
      throw new Error('非法技能路径')
    }
  }

  // C-1：创建技能目录（渲染层技能 ZIP 解压用）— assertSafePath + 边界断言
  handleTrusted('skills:mkdir', async (_event, dirPath: string) => {
    await assertSafePath(dirPath)
    assertSkillsPath(dirPath)
    await mkdir(pathResolve(dirPath), { recursive: true })
  })

  // C-1：写入技能文件（base64 字节流，渲染层技能 ZIP 解压用）
  // assertSafePath + 对 filePath 与其 dirname 都 resolve 后做 skills 根边界断言
  handleTrusted('skills:writeFile', async (_event, filePath: string, base64: string) => {
    await assertSafePath(filePath)
    const skillsRoot = getSkillsRoot()
    const fileResolved = pathResolve(filePath)
    const dirResolved = pathResolve(dirname(filePath))
    if (fileResolved !== skillsRoot && !fileResolved.startsWith(skillsRoot + pathSep)) {
      throw new Error('非法技能路径')
    }
    if (dirResolved !== skillsRoot && !dirResolved.startsWith(skillsRoot + pathSep)) {
      throw new Error('非法技能路径')
    }
    const buf = Buffer.from(base64, 'base64')
    await writeFile(filePath, buf)
  })

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

// 审查清理：旧 safeStorage 凭证 IPC（auth:isEncryptionAvailable/encryptString/
// decryptString/setBackendToken）与 Bearer token 缓存链路已移除 — 渲染层零调用，
// 且后端只校验 X-API-Token（callBackendApi 注入），Bearer 路径为旧鉴权契约残留。
// 连接凭据加解密现由 ssh-store 在主进程内直接用 safeStorage 完成。

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
  handleTrusted('sftp:rename', (_event, sshSessionId: string, oldPath: string, newPath: string) => {
    return sshManager!.sftpRename(sshSessionId, oldPath, newPath)
  })
  handleTrusted('sftp:delete', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpDelete(sshSessionId, remotePath)
  })
  // 上传/下载：审查修复 — 本地路径须过 assertSafePath（原实现 remotePath 走
  // 远程侧无本地风险，但 localPath 是渲染进程任意字符串，受控渲染进程可写
  // 到系统任意位置如 C:\Windows\Temp 或读敏感文件内容上传到远程）
  handleTrusted('sftp:upload', async (_event, transferId: string, sshSessionId: string, localPath: string, remotePath: string) => {
    await assertSafePath(localPath)
    return sshManager!.sftpUpload(transferId, sshSessionId, localPath, remotePath)
  })
  handleTrusted('sftp:download', async (_event, transferId: string, sshSessionId: string, remotePath: string, localPath: string) => {
    await assertSafePath(localPath)
    return sshManager!.sftpDownload(transferId, sshSessionId, remotePath, localPath)
  })
  handleTrusted('sftp:cancel', (_event, transferId: string) => {
    sshManager!.sftpCancel(transferId)
  })
  handleTrusted('sftp:readFile', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpReadFile(sshSessionId, remotePath)
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

  // 远程终端命令补全：在已建立的 SSH 连接上执行 compgen
  // 关键：使用 bash -lic 加载完整登录交互环境，识别 alias/function（conda 等 shell 函数）
  // 同时 source /etc/profile.d/conda.sh 兼容非 .bashrc 安装
  // 输出格式：每行 "type:text"，type ∈ {func,alias,cmd,file,arg}，让前端能排序/去重
  // 重要：SSH exec 通道的默认工作目录是用户主目录，不是交互式 shell 的当前目录。
  //       通过 OSC 7 跟踪交互式 shell 的 cwd，在补全命令前 cd 到该目录。
  handleTrusted('ssh:completeCommand', async (_event, sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments: string[] }) => {
    const { prefix, isCommand, cmd, prevSegments } = opts
    const scriptBody = buildBashCompletionScript({ isCommand, cmd, prefix, prevSegments })
    // 获取交互式 shell 的当前工作目录，将 cd 前缀合并到 scriptBody 中统一转义
    // 避免 shellQuote 产生的单引号未转义就嵌入外层单引号导致路径断裂
    const cwd = sshManager!.getCwd(sshSessionId)
    const fullScript = cwd ? `cd ${shellQuote(cwd)} && ${scriptBody}` : scriptBody
    // 远程通过 SSH 执行命令字符串，需要把脚本体安全嵌入单引号参数
    const shellCmd = `bash -lic '${escapeBashScript(fullScript)}' 2>/dev/null`
    return sshManager!.execCommand(sshSessionId, shellCmd)
  })
}

// ─── App Lifecycle ──────────────────────────────────
// 修复 GPU cache 权限问题：禁用 GPU 磁盘缓存（Windows GPU 进程无写权限时的拒绝访问错误）
// 注意：不使用 setPath('userData') 以避免破坏数据库/SSH 连接等现有数据路径
app.commandLine.appendSwitch('disable-gpu-disk-cache')

// 审查 M-9：单实例锁（原实现完全没有，grep 验证零 requestSingleInstanceLock）。
// 原行为：可同时启动多个应用实例 — 第二个实例再拉起一套 Python 后端（端口冲突/
// 双写同一 SQLite）、再开一组 PTY/SSH 连接、重复注册 IPC 处理器。
// 现在：第二个实例退出，并把第一个实例的窗口带到前台。
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    }
  })
}

app.whenReady().then(async () => {
  // 双保险：非主实例不启动任何服务（quit 与 ready 的时序竞态下仍然安全）
  if (!gotSingleInstanceLock) return
  installCsp()
  setupWindowIPC()
  setupFSIPC()
  createWindow()
  setupPtyIPC()
  setupSSHIPC()
  setupSkillsIPC()
  setupSftpIPC()

  // 审查修复：顶层异常兜底 — 原实现无 uncaughtException/unhandledRejection
  // 处理器，主进程任何未捕获异常（IPC handler 竞态、异步回调遗漏的 reject）
  // 直接崩掉整个应用（丢 PTY/SSH 连接 + 后端进程变孤儿）。
  // 策略：记日志 + 尽力保活；连续 3 次 uncaught 视为不可恢复，主动退出
  let uncaughtCount = 0
  process.on('uncaughtException', (err) => {
    console.error('[main] uncaughtException:', err)
    if (++uncaughtCount >= 3) {
      console.error('[main] 连续 3 次未捕获异常，退出应用')
      app.quit()
    }
  })
  process.on('unhandledRejection', (reason) => {
    // 只记日志不退出 — 未捕获的 promise rejection 多为业务侧遗漏 catch，
    // 直接退出会把一次可恢复错误放大成应用崩溃
    console.error('[main] unhandledRejection:', reason)
  })

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
    console.log(`[BackendManager] Python backend ready on port ${port}`)
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
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  // 审查修复：darwin 分支不再销毁 PTY/SSH/后端 — 原实现关最后一个窗口即
  // disconnectAll + stop 后端，但 macOS 上应用仍驻留 Dock，点图标 activate
  // 重建窗口后后端已死且不重启（whenReady 只跑一次），应用半残。
  // 资源释放统一交给 before-quit（真正退出时才执行）。
  if (process.platform !== 'darwin') {
    ptyManager?.destroyAll()
    sshManager?.disconnectAll()
    backendManager?.stop()
    bridgeWsClient?.stop()
    electronBridge?.stop()
    app.quit()
  }
})

app.on('before-quit', () => {
  ptyManager?.destroyAll()
  sshManager?.disconnectAll()
  backendManager?.stop()
  bridgeWsClient?.stop()
  electronBridge?.stop()
})
