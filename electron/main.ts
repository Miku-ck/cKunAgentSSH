import { app, BrowserWindow, ipcMain, shell, dialog, session, safeStorage, nativeImage } from 'electron'
import { join, normalize, resolve as pathResolve, dirname, basename, extname } from 'path'
import { is } from '@electron-toolkit/utils'
import { readdir, readFile, writeFile, stat, mkdir, rename, rm, realpath, copyFile } from 'fs/promises'
import { existsSync, readdirSync } from 'fs'
import { homedir } from 'os'
import { execSync, exec, execFile } from 'child_process'
import { BackendManager } from './backend-manager'
import { PtyManager } from './pty-manager'
import { SSHManager } from './ssh-manager'
import { SSHStore, isEncryptionAvailable, type SSHConnectionRecord } from './ssh-store'
import { ElectronBridge } from './electron-bridge'

let mainWindow: BrowserWindow | null = null
let backendManager: BackendManager | null = null
let ptyManager: PtyManager | null = null
let sshManager: SSHManager | null = null
let sshStore: SSHStore | null = null
let electronBridge: ElectronBridge | null = null

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

  // 注入 CSP 响应头，限制资源加载范围
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

  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
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
  ipcMain.handle('win:minimize', () => mainWindow?.minimize())
  ipcMain.handle('win:maximize', () => {
    if (mainWindow?.isMaximized()) {
      mainWindow.unmaximize()
    } else {
      mainWindow?.maximize()
    }
  })
  ipcMain.handle('win:close', () => mainWindow?.close())
}

// ─── File System IPC ────────────────────────────────

/** 主目录顶层敏感文件/目录（仅在用户主目录顶层时拦截） */
const HOME_TOPLEVEL_SENSITIVE = new Set([
  '.ssh', '.aws', '.gnupg', '.gpg', '.kube', '.docker',
  '.env', '.npmrc', '.yarnrc',
])

/**
 * 任意路径段都拦截的敏感目录名。
 * 这些目录几乎不会作为合法项目子目录出现，且包含凭证，因此任何位置都拒绝。
 * 注意：`.env` / `.npmrc` / `.yarnrc` / `.docker` / `.gpg` 不在此列——
 * 它们常作为项目内合法文件/目录存在，仅在主目录顶层拦截即可。
 */
const ANYWHERE_SENSITIVE = new Set([
  '.ssh', '.aws', '.gnupg', '.kube',
])

/**
 * 检查路径是否访问敏感目录，是则抛异常。
 * M2 修复：先通过 realpath 解析符号链接，防止渲染进程利用 symlink 绕过检查。
 * M6 修复：`.env` / `.npmrc` 等仅在主目录顶层拦截，不再误伤项目内同名文件。
 */
async function assertSafePath(targetPath: string): Promise<void> {
  let resolved = pathResolve(targetPath)
  // M2：解析符号链接——目标存在时直接 realpath；不存在时回退到父目录 realpath + basename
  try {
    resolved = await realpath(resolved)
  } catch {
    try {
      const parentReal = await realpath(dirname(resolved))
      resolved = join(parentReal, basename(resolved))
    } catch {
      // 父目录也不存在：使用原始 resolve 结果（后续检查仍基于字面路径）
    }
  }
  const home = homedir()
  const homeResolved = pathResolve(home)
  // 检查是否直接访问主目录下的敏感子目录
  if (resolved.startsWith(homeResolved)) {
    const relative = resolved.slice(homeResolved.length).replace(/^[\\/]+/, '')
    const topLevel = relative.split(/[\\/]/)[0]
    if (topLevel && HOME_TOPLEVEL_SENSITIVE.has(topLevel)) {
      throw new Error(`拒绝访问敏感路径: ${targetPath}`)
    }
  }
  // 检查绝对路径中的任意位置敏感目录名（仅限凭证类目录）
  for (const seg of resolved.split(/[\\/]/)) {
    if (ANYWHERE_SENSITIVE.has(seg)) {
      throw new Error(`拒绝访问敏感路径: ${targetPath}`)
    }
  }
}

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
      const output = execSync(query, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'ignore'] })
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
  ipcMain.handle('fs:readDir', async (_event, dirPath: string) => {
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
  ipcMain.handle('fs:readFile', async (_event, filePath: string) => {
    try {
      await assertSafePath(filePath)
      const content = await readFile(filePath, 'utf-8')
      return content
    } catch (err: any) {
      throw new Error('读取文件失败，请检查文件是否存在或是否有访问权限')
    }
  })

  // 写入文件内容
  ipcMain.handle('fs:writeFile', async (_event, filePath: string, content: string) => {
    try {
      await assertSafePath(filePath)
      await writeFile(filePath, content, 'utf-8')
    } catch (err: any) {
      throw new Error('写入文件失败，请检查路径是否存在或是否有写入权限')
    }
  })

  // 为弹窗编辑器读取文件内容（带大小/二进制校验）
  ipcMain.handle('fs:readFileForEditor', async (_event, filePath: string) => {
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
  ipcMain.handle('fs:selectFolder', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory'],
    })
    if (result.canceled) return null
    return result.filePaths[0]
  })

  // 选择文件(用于私钥等)
  ipcMain.handle('fs:selectFile', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openFile'],
    })
    if (result.canceled) return null
    return result.filePaths[0]
  })

  // 选择背景图片并保存到 userData/backgrounds，返回文件名
  ipcMain.handle('fs:selectImage', async () => {
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
  ipcMain.handle('fs:loadBackgroundImage', async (_, fileName: string) => {
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
  ipcMain.handle('fs:deleteBackgroundImage', async (_, fileName: string) => {
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
  ipcMain.handle('fs:getHomePath', () => homedir())

  // 获取可用盘符（Windows 返回 ['C:\\', 'D:\\', ...]，其他平台返回 []）
  ipcMain.handle('fs:getDrives', async () => {
    if (process.platform !== 'win32') return []
    try {
      const { execFile } = await import('node:child_process')
      const { promisify } = await import('node:util')
      const execFileAsync = promisify(execFile)
      // 使用 wmic 获取本地磁盘盘符列表
      const { stdout } = await execFileAsync('wmic', ['logicaldisk', 'get', 'caption'])
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
  ipcMain.handle('fs:mkdir', async (_event, dirPath: string) => {
    await assertSafePath(dirPath)
    await mkdir(dirPath, { recursive: true })
  })

  // 重命名
  ipcMain.handle('fs:rename', async (_event, oldPath: string, newPath: string) => {
    await assertSafePath(oldPath)
    await assertSafePath(newPath)
    await rename(oldPath, newPath)
  })

  // 递归复制文件/目录（同侧复制粘贴），通过 sftp:progress 上报进度
  ipcMain.handle('fs:copy', async (_event, srcPath: string, destPath: string, transferId: string) => {
    await assertSafePath(srcPath)
    await assertSafePath(destPath)

    // 收集所有文件并统计总大小
    const files: Array<{ src: string; dest: string; size: number }> = []
    async function collect(src: string, dest: string): Promise<void> {
      const st = await stat(src)
      if (st.isFile()) {
        files.push({ src, dest, size: st.size })
      } else if (st.isDirectory()) {
        await mkdir(dest, { recursive: true })
        const entries = await readdir(src)
        for (const entry of entries) {
          await collect(join(src, entry), join(dest, entry))
        }
      }
    }
    await collect(srcPath, destPath)

    const total = files.reduce((sum, f) => sum + f.size, 0)
    let transferred = 0
    let lastReport = 0
    const report = () => {
      const now = Date.now()
      if (now - lastReport > 100) {
        lastReport = now
        mainWindow?.webContents.send('sftp:progress', { id: transferId, transferred, total })
      }
    }

    for (const f of files) {
      await mkdir(dirname(f.dest), { recursive: true })
      await copyFile(f.src, f.dest)
      transferred += f.size
      report()
    }
    mainWindow?.webContents.send('sftp:progress', { id: transferId, transferred: total, total })
  })

  // 移到回收站
  ipcMain.handle('fs:trash', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    await shell.trashItem(filePath)
  })

  // 写入二进制文件（用于技能包资源文件解压）
  // data 为 base64 编码的字节数据，避免 IPC 序列化丢失字节
  ipcMain.handle('fs:writeBinaryFile', async (_event, filePath: string, data: string) => {
    try {
      await assertSafePath(filePath)
      const buf = Buffer.from(data, 'base64')
      await writeFile(filePath, buf)
    } catch (err: any) {
      throw new Error(`Failed to write binary file: ${err.message}`)
    }
  })

  // 路径拼接（主进程使用 Node path，避免渲染进程引入 polyfill）
  ipcMain.handle('fs:pathJoin', async (_event, ...segments: string[]) => {
    return join(...segments)
  })

  // 技能目录根路径：{userData}/skills
  ipcMain.handle('fs:getSkillsDir', async () => {
    const dir = join(app.getPath('userData'), 'skills')
    await mkdir(dir, { recursive: true })
    return dir
  })

  // 递归删除目录（用于卸载技能时清理资源）
  ipcMain.handle('fs:rmRF', async (_event, targetPath: string) => {
    try {
      // 安全：只允许删除技能目录下的路径，避免误删
      // 用 realpath 解析符号链接 + 严格边界检查，防止 `...\skills\..\..\Windows\System32` 路径遍历绕过
      const skillsRoot = await realpath(join(app.getPath('userData'), 'skills'))
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
  ipcMain.handle('fs:listFilesRecursive', async (_event, dirPath: string): Promise<string[]> => {
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

  // 列出工作区下的文本类源码文件（递归），用于 @ 菜单文件提及。
  // 在主进程侧剪枝：跳过 node_modules/.git/dist 等巨型目录、仅收集文本扩展名、
  // 总数上限 2000。避免大工作区返回数十万路径阻塞渲染进程导致 UI 卡死。
  ipcMain.handle('fs:listTextFiles', async (_event, dirPath: string): Promise<string[]> => {
    try {
      await assertSafePath(dirPath)
      const SKIP_DIRS = new Set([
        'node_modules', '.git', '.svn', '.hg', 'dist', 'build', 'out',
        '.next', '.nuxt', '.cache', '.parcel-cache', 'coverage',
        '__pycache__', '.pytest_cache', '.mypy_cache', '.venv', 'venv',
        'target', '.gradle', '.idea', '.vscode', 'vendor',
      ])
      const TEXT_EXT = new Set([
        'txt','md','js','ts','vue','py','java','c','cpp','h','hpp',
        'go','rs','rb','php','sh','bat','ps1','sql','html','css',
        'json','yaml','yml','toml','xml','csv','tsv','log',
        'jsx','tsx','svelte','astro','lua','pl','r','m','swift',
        'kt','dart','scala','clj','hs','ml','fs','elm',
      ])
      const MAX_FILES = 2000
      const result: string[] = []
      let stopped = false
      async function walk(dir: string, base: string): Promise<void> {
        if (stopped || result.length >= MAX_FILES) { stopped = true; return }
        const entries = await readdir(dir, { withFileTypes: true })
        for (const entry of entries) {
          if (stopped || result.length >= MAX_FILES) { stopped = true; return }
          if (entry.isDirectory()) {
            if (SKIP_DIRS.has(entry.name)) continue
            await walk(join(dir, entry.name), join(base, entry.name))
          } else {
            const ext = entry.name.split('.').pop()?.toLowerCase() || ''
            if (!TEXT_EXT.has(ext)) continue
            result.push(join(base, entry.name))
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
  ipcMain.handle('fs:exists', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    return existsSync(filePath)
  })

  // 获取文件/目录信息（用于判断是文件还是目录）
  ipcMain.handle('fs:stat', async (_event, filePath: string) => {
    await assertSafePath(filePath)
    const s = await stat(filePath)
    return { isDirectory: s.isDirectory(), isFile: s.isFile(), size: s.size, modified: s.mtimeMs }
  })

  // 按环境变量 PATH 查找可执行文件；对 bash 额外读取 Git for Windows 注册表
  ipcMain.handle('fs:resolveProgram', (_event, name: string) => {
    if (name === 'bash') {
      const gitBash = findGitBashFromRegistry()
      if (gitBash) return gitBash
    }
    return findProgramInPath(name)
  })

  // 本地 ZIP 打包：sources 为本地路径数组，输出到 localZipPath
  // Windows 使用 PowerShell Compress-Archive，Linux/macOS 使用 zip 命令
  ipcMain.handle('fs:zipLocal', async (_event, sources: string[], localZipPath: string) => {
    if (!sources || sources.length === 0) throw new Error('请至少选择一个要压缩的文件/目录')
    for (const s of sources) await assertSafePath(s)
    await assertSafePath(localZipPath)

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
  ipcMain.handle('fs:unzipLocal', async (_event, localZipPath: string, localDestDir: string) => {
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

  ipcMain.handle('pty:create', (_event, opts?: { shell?: string; cwd?: string; cols?: number; rows?: number }) => {
    return ptyManager!.create(opts)
  })

  ipcMain.handle('pty:write', (_event, id: string, data: string) => {
    ptyManager!.write(id, data)
  })

  ipcMain.handle('pty:resize', (_event, id: string, cols: number, rows: number) => {
    ptyManager!.resize(id, cols, rows)
  })

  ipcMain.handle('pty:destroy', (_event, id: string) => {
    ptyManager!.destroy(id)
  })

  // 本地终端命令补全：在指定 cwd 执行补全
  // Windows PowerShell：使用 [System.Management.Automation.CommandCompletion]::CompleteInput
  // macOS/Linux bash：使用 compgen（识别 function/alias/command/file）
  ipcMain.handle('pty:completeCommand', (_event, opts: { ptyId: string; cmd: string; prefix: string; cwd: string; isCommand: boolean; prevSegments: string[]; shell?: string }) => {
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
        exec(`"${ps}" -NoProfile -NonInteractive -Command "${psScript.replace(/"/g, '\\"').replace(/\n/g, ' ')}"`,
          { cwd: actualCwd, timeout: 5000, windowsHide: true, shell: 'cmd.exe' },
          (err, stdout) => {
            if (err) resolve('')
            else resolve(stdout)
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
function setupSSHIPC(): void {
  sshManager = new SSHManager()
  sshStore = new SSHStore()
  if (mainWindow) sshManager.setWindow(mainWindow)

  ipcMain.handle('ssh:connect', async (_event, config: any) => {
    const { cols, rows, ...connConfig } = config
    const result = await sshManager!.connect(connConfig, cols || 80, rows || 24)
    // 连接成功后更新最后连接时间
    if (connConfig.id) {
      sshStore!.touchLastConnected(connConfig.id).catch(() => { /* ignore */ })
    }
    return result
  })

  ipcMain.handle('ssh:connectSftp', async (_event, config: any) => {
    const result = await sshManager!.connectSftp(config)
    if (config.id) {
      sshStore!.touchLastConnected(config.id).catch(() => { /* ignore */ })
    }
    return result
  })

  ipcMain.handle('ssh:write', (_event, id: string, data: string) => {
    sshManager!.write(id, data)
  })

  ipcMain.handle('ssh:resize', (_event, id: string, cols: number, rows: number) => {
    sshManager!.resize(id, cols, rows)
  })

  ipcMain.handle('ssh:disconnect', (_event, id: string) => {
    sshManager!.disconnect(id)
  })

  ipcMain.handle('ssh:getCwd', (_event, id: string) => {
    return sshManager!.getCwd(id)
  })

  ipcMain.handle('ssh:exec', async (_event, id: string, command: string) => {
    return sshManager!.execCommandWithExitCode(id, command)
  })

  // 连接配置管理
  ipcMain.handle('ssh:listConnections', () => sshStore!.list())

  ipcMain.handle('ssh:isEncryptionAvailable', () => isEncryptionAvailable())

  ipcMain.handle('ssh:saveConnection', (_event, conn: SSHConnectionRecord) => {
    return sshStore!.save(conn)
  })

  ipcMain.handle('ssh:deleteConnection', (_event, id: string) => {
    return sshStore!.delete(id)
  })

  ipcMain.handle('ssh:duplicateConnection', (_event, id: string) => {
    return sshStore!.duplicate(id)
  })

  ipcMain.handle('ssh:testConnection', async (_event, config: any) => {
    return sshManager!.testConnection(config)
  })

  ipcMain.handle('ssh:exportConnections', (_event, ids: string[]) => {
    return sshStore!.export(ids)
  })

  ipcMain.handle('ssh:importConnections', (_event, json: string) => {
    return sshStore!.import(json)
  })

  ipcMain.handle('ssh:migrateLegacy', (_event, legacy: any[]) => {
    return sshStore!.migrateLegacy(legacy)
  })

  // 后端端口查询（渲染进程启动时兜底获取，防止 backend:status 事件丢失）
  ipcMain.handle('backend:getPort', () => {
    return backendManager?.getPort() ?? 0
  })

  // 后端存活状态查询（渲染进程在 API 失败时用于确认后端是否崩溃/重启中）
  ipcMain.handle('backend:isAlive', () => {
    return backendManager?.isAlive() ?? false
  })
}

// ─── Skills IPC（转发到后端 HTTP API）─────────────────
// 主进程缓存的后端认证 token：渲染进程登录/登出时通过 'auth:setBackendToken' IPC 推送。
// 主进程无法直接读取 renderer 的 localStorage，故用模块级变量缓存。
// 仅用于 callBackendApi（主进程转发到后端的 HTTP 请求），不参与渲染进程自己的 fetch。
let backendToken: string | null = null

async function callBackendApi(path: string, options?: RequestInit): Promise<any> {
  const port = backendManager?.getPort() ?? 0
  if (!port) throw new Error('后端未就绪')
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> | undefined || {}),
  }
  // 注入 Authorization 头：后端 AuthMiddleware 对 /api/ 路径强制校验 Bearer token
  if (backendToken) {
    headers['Authorization'] = `Bearer ${backendToken}`
  }
  const res = await fetch(`http://127.0.0.1:${port}/api${path}`, {
    ...options,
    headers,
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
  ipcMain.handle('skills:list', async (_event, mode?: string) => {
    const qs = mode ? `?mode=${encodeURIComponent(mode)}` : ''
    return callBackendApi(`/skills${qs}`)
  })
  ipcMain.handle('skills:create', async (_event, skill: any) => {
    return callBackendApi('/skills', {
      method: 'POST',
      body: JSON.stringify(skill),
    })
  })
  ipcMain.handle('skills:updateEnabled', async (_event, id: string, enabled: boolean, mode?: string) => {
    const qs = mode ? `?mode=${encodeURIComponent(mode)}` : ''
    return callBackendApi(`/skills/${id}/enabled${qs}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
  })
  ipcMain.handle('skills:delete', async (_event, id: string) => {
    return callBackendApi(`/skills/${id}`, {
      method: 'DELETE',
    })
  })
  ipcMain.handle('skills:syncBuiltin', async (_event, skills: any[]) => {
    return callBackendApi('/skills/sync-builtin', {
      method: 'POST',
      body: JSON.stringify({ skills }),
    })
  })
}

// ─── Auth Credentials IPC ───────────────────────────
// 渲染进程负责把密文存入 localStorage；主进程只提供 safeStorage 加解密服务，
// 避免明文密码进入持久化存储。
function setupAuthIPC(): void {
  ipcMain.handle('auth:isEncryptionAvailable', () => safeStorage.isEncryptionAvailable())

  // 渲染进程推送后端 token 到主进程缓存，供 callBackendApi 注入 Authorization 头
  // （主进程无法直接读取 renderer 的 localStorage）。登出时传 null 清空。
  ipcMain.handle('auth:setBackendToken', (_event, token: string | null) => {
    backendToken = typeof token === 'string' && token ? token : null
    return { ok: true }
  })

  ipcMain.handle('auth:encryptString', (_event, plain: string) => {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('safeStorage 加密不可用，无法记住密码')
    }
    if (typeof plain !== 'string') throw new Error('参数必须是字符串')
    return 'enc:' + safeStorage.encryptString(plain).toString('base64')
  })

  ipcMain.handle('auth:decryptString', (_event, encrypted: string) => {
    if (typeof encrypted !== 'string') return null
    if (encrypted.startsWith('enc:')) {
      if (!safeStorage.isEncryptionAvailable()) return null
      try {
        return safeStorage.decryptString(Buffer.from(encrypted.slice(4), 'base64'))
      } catch {
        return null
      }
    }
    // 兼容旧版 plain: 前缀（仅迁移用）
    if (encrypted.startsWith('plain:')) {
      try {
        return Buffer.from(encrypted.slice(6), 'base64').toString('utf-8')
      } catch {
        return null
      }
    }
    return null
  })
}

// ─── SFTP IPC ───────────────────────────────────────
function setupSftpIPC(): void {
  ipcMain.handle('sftp:open', (_event, sshSessionId: string) => {
    return sshManager!.openSftp(sshSessionId)
  })
  ipcMain.handle('sftp:close', (_event, sshSessionId: string) => {
    sshManager!.closeSftp(sshSessionId)
  })
  ipcMain.handle('sftp:list', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpList(sshSessionId, remotePath)
  })
  ipcMain.handle('sftp:mkdir', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpMkdir(sshSessionId, remotePath)
  })
  ipcMain.handle('sftp:mkdirRecursive', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpMkdirRecursive(sshSessionId, remotePath)
  })
  // 检查远程路径是否存在（用于 ZIP 覆盖确认等场景）
  ipcMain.handle('sftp:exists', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpExists(sshSessionId, remotePath)
  })
  ipcMain.handle('sftp:rename', (_event, sshSessionId: string, oldPath: string, newPath: string) => {
    return sshManager!.sftpRename(sshSessionId, oldPath, newPath)
  })
  ipcMain.handle('sftp:delete', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpDelete(sshSessionId, remotePath)
  })
  ipcMain.handle('sftp:upload', (_event, transferId: string, sshSessionId: string, localPath: string, remotePath: string) => {
    return sshManager!.sftpUpload(transferId, sshSessionId, localPath, remotePath)
  })
  ipcMain.handle('sftp:download', (_event, transferId: string, sshSessionId: string, remotePath: string, localPath: string) => {
    return sshManager!.sftpDownload(transferId, sshSessionId, remotePath, localPath)
  })
  ipcMain.handle('sftp:cancel', (_event, transferId: string) => {
    sshManager!.sftpCancel(transferId)
  })
  ipcMain.handle('sftp:readFile', (_event, sshSessionId: string, remotePath: string) => {
    return sshManager!.sftpReadFile(sshSessionId, remotePath)
  })
  ipcMain.handle('sftp:writeFile', (_event, sshSessionId: string, remotePath: string, content: string) => {
    return sshManager!.sftpWriteFile(sshSessionId, remotePath, content)
  })
  // 远程 ZIP 打包：通过 SSH exec 执行 zip/python 命令
  ipcMain.handle('sftp:zipRemote', (_event, sshSessionId: string, sources: string[], remoteZipPath: string, cwd: string) => {
    return sshManager!.sftpZipRemote(sshSessionId, sources, remoteZipPath, cwd || '/')
  })
  // 远程 ZIP 解压
  ipcMain.handle('sftp:unzipRemote', (_event, sshSessionId: string, remoteZipPath: string, remoteDestDir: string) => {
    return sshManager!.sftpUnzipRemote(sshSessionId, remoteZipPath, remoteDestDir)
  })

  // 远程终端命令补全：在已建立的 SSH 连接上执行 compgen
  // 关键：使用 bash -lic 加载完整登录交互环境，识别 alias/function（conda 等 shell 函数）
  // 同时 source /etc/profile.d/conda.sh 兼容非 .bashrc 安装
  // 输出格式：每行 "type:text"，type ∈ {func,alias,cmd,file,arg}，让前端能排序/去重
  // 重要：SSH exec 通道的默认工作目录是用户主目录，不是交互式 shell 的当前目录。
  //       通过 OSC 7 跟踪交互式 shell 的 cwd，在补全命令前 cd 到该目录。
  ipcMain.handle('ssh:completeCommand', async (_event, sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments: string[] }) => {
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

app.whenReady().then(async () => {
  setupWindowIPC()
  setupFSIPC()
  createWindow()
  setupPtyIPC()
  setupSSHIPC()
  setupSkillsIPC()
  setupAuthIPC()
  setupSftpIPC()

  // Start Electron bridge for backend → main process remote execution
  if (sshManager) {
    electronBridge = new ElectronBridge({ sshManager })
  }

  // Start Python backend
  backendManager = new BackendManager()
  // 后端状态变化（含崩溃自动重启）统一通过回调推送到前端
  backendManager.onStatusChange = (data) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:status', data)
    }
  }
  try {
    let bridgeEnv = { port: 0, token: '' }
    if (electronBridge) {
      bridgeEnv = await electronBridge.start()
    }
    const extraEnv: Record<string, string> = {}
    if (bridgeEnv.port) {
      extraEnv.ELECTRON_BRIDGE_PORT = String(bridgeEnv.port)
      extraEnv.ELECTRON_BRIDGE_TOKEN = bridgeEnv.token
    }
    const port = await backendManager.start(extraEnv)
    console.log(`[BackendManager] Python backend ready on port ${port}`)
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
  ptyManager?.destroyAll()
  sshManager?.disconnectAll()
  backendManager?.stop()
  electronBridge?.stop()
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', () => {
  ptyManager?.destroyAll()
  sshManager?.disconnectAll()
  backendManager?.stop()
  electronBridge?.stop()
})
