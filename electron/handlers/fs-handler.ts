/**
 * fs:* IPC —— 本地文件系统读写、背景图、盘符、ZIP 与递归复制。
 *
 * 从 electron/main.ts 的 setupFSIPC() 原样迁出（行为、文案、上限一律不变）：
 * 那 518 行让主进程入口涨到 2239 行，而其中九成 handler 与窗口、后端、SSH
 * 全无关系 —— 它们只在用户真的打开文件/文件夹/背景图时才该被读到。拆出来之后
 * main.ts 只剩编排，加一条 fs 通道不用再翻两千行。
 *
 * 依赖通过 deps 注入，不在本模块里回引 main.ts：handleTrusted 依赖 mainWindow
 * 与 normalizeIpcError，backgroundDir 依赖 app.getPath('userData')，二者都是
 * main.ts 的运行时状态。反向 import 会形成循环依赖（main → handlers/fs → main），
 * 而 main.ts 在模块顶层就要拿到 setupFSIPC —— 循环会让其中一侧拿到 undefined。
 */
import { BrowserWindow, dialog, shell, nativeImage } from 'electron'
import { safeSend } from '../utils/safe-send'
import { join, basename, extname, dirname } from 'path'
import { readdir, readFile, writeFile, stat, lstat, mkdir, rename, rm, realpath, copyFile } from 'fs/promises'
import { existsSync } from 'fs'
import { homedir } from 'os'
import { execFile, execFileSync } from 'child_process'
import { assertSafePath, allowDialogKeyPath, isCopyTargetSelfNested, rejectSymlinkTarget } from '../utils/path-guard'
import { beginCopy, endCopy } from '../utils/copy-cancel'
import { resolvePowerShell } from '../utils/powershell-path'

export interface FsHandlerDeps {
  /** 带 sender 校验 + SSHError 序列化的 handler 注册器（main.ts 的 handleTrusted） */
  handleTrusted: (channel: string, fn: (event: Electron.IpcMainInvokeEvent, ...args: any[]) => any) => void
  /** 背景图目录：userData/backgrounds。注入而非自己算 —— main.ts 已有同一口径 */
  backgroundDir: () => string
  /** 取当前主窗口（fs:selectFile / fs:selectImage 要拿它当对话框父窗口） */
  getMainWindow: () => BrowserWindow | null
  /** 留痕出口：敏感路径拒绝、复制时跳过的条目都经它进主进程日志 */
  logWarn: (domain: string, msg: string) => void
}

const IMAGE_MIME_MAP: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
}

/** 背景图文件名前缀（保存/清理/加载/删除四处共用，避免字面量散落） */
const BG_FILE_PREFIX = 'background.'

/** 递归复制上限：10 万文件 / 10GB */
const MAX_COPY_FILES = 100_000
const MAX_COPY_BYTES = 10 * 1024 * 1024 * 1024
const COPY_CONCURRENCY = 8

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

/** 保留原始 err.message，仅在缺失时兜底中文文案后重新抛出（fs IPC 处理器共用） */
function fsErrorWith(err: unknown, fallback: string): Error {
  return new Error((err as Error)?.message || fallback)
}

/** 程序名校验：只接受单段文件名。PATH 目录 + 任意相对段组合起来就是一次任意
 *  位置的存在性探测（预言机），`..`、`.` 与路径分隔符都在收紧之列；`:` 在
 *  Windows 上是盘符/ADS 分隔，同样拒绝。渲染层调用方只有 bash / pwsh /
 *  powershell 裸名解析（src/stores/ssh.ts），无带子目录的合法用法。 */
export function assertProgramName(name: string): void {
  if (typeof name !== 'string' || name.trim().length === 0) throw new Error('程序名不能为空')
  if (name === '.' || name === '..') throw new Error('非法程序名')
  if (name.includes('/') || name.includes('\\') || name.includes(':')) {
    throw new Error('程序名不能包含路径分隔符')
  }
}

/** 在 PATH 环境变量中查找可执行文件 */
export function findProgramInPath(name: string): string | null {
  assertProgramName(name)
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

/** 敏感路径拒绝事件不在 catch 中改写成通用文案（透传 + warn）。 */
function rethrowIfSensitivePath(err: any, logWarn: (d: string, m: string) => void): void {
  if (typeof err?.message === 'string' && err.message.startsWith('拒绝访问敏感路径')) {
    logWarn('fs', `敏感路径访问被拒绝: ${err.message}`)
    throw err
  }
}

/** 读取 Git for Windows 安装路径（优先 HKLM，fallback HKCU） */
function findGitBashFromRegistry(): string | null {
  if (process.platform !== 'win32') return null
  // 注册表读取走 PowerShell Get-ItemProperty（与 utils/proxy.ts 同口径），不再
  // spawn reg.exe：reg.exe 是安全软件/命令黑名单的高频盯防对象，被拦时整棵
  // Electron 进程树会被连带终止。仍用 argv 数组形式 execFileSync（不经 cmd.exe
  // 拼接解析），resolvePowerShell 出绝对路径避免 PATH 裁剪环境下裸名 ENOENT。
  const psKeys = ['HKLM:\\SOFTWARE\\GitForWindows', 'HKCU:\\SOFTWARE\\GitForWindows']
  for (const key of psKeys) {
    try {
      const output = execFileSync(
        resolvePowerShell(),
        ['-NoProfile', '-NonInteractive', '-Command',
          `(Get-ItemProperty -Path '${key}' -ErrorAction Stop).InstallPath`],
        {
          encoding: 'utf-8',
          stdio: ['pipe', 'pipe', 'ignore'],
          timeout: 3000,
          windowsHide: true,
        },
      )
      const installPath = String(output ?? '').trim()
      if (!installPath) continue
      const bashPath = join(installPath, 'bin', 'bash.exe')
      if (existsSync(bashPath)) return bashPath
    } catch {
      // ignore registry errors
    }
  }
  return null
}

export function setupFSIPC(deps: FsHandlerDeps): void {
  const { handleTrusted, backgroundDir, getMainWindow, logWarn } = deps

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
      rethrowIfSensitivePath(err, logWarn)
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
      rethrowIfSensitivePath(err, logWarn)
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
      rethrowIfSensitivePath(err, logWarn)
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
      rethrowIfSensitivePath(err, logWarn)
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
    const win = getMainWindow()
    if (!win) throw new Error('主窗口不存在，无法打开文件选择对话框')
    const result = await dialog.showOpenDialog(win, {
      properties: ['openFile'],
    })
    if (result.canceled) return null
    const filePath = result.filePaths[0]
    try { allowDialogKeyPath(await realpath(filePath)) } catch { /* 路径消失则跳过白名单 */ }
    return filePath
  })

  // 选择背景图片并保存到 userData/backgrounds，返回文件名
  handleTrusted('fs:selectImage', async () => {
    const win = getMainWindow()
    if (!win) throw new Error('主窗口不存在，无法打开文件选择对话框')
    const result = await dialog.showOpenDialog(win, {
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
      // 复用模块级已静态导入的 execFile，不再重复动态 import
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
            logWarn('fs:copy', `跳过无法访问的条目: ${src} (${e?.message || e})`)
            return null
          })
      if (!st) return
      if (!isRoot && st.isSymbolicLink()) {
        logWarn('fs:copy', `跳过符号链接（防止泄露链接目标内容）: ${src}`)
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
        safeSend(getMainWindow(), 'sftp:progress', data)
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
    // 段校验前置：bash 特例分支也过同一道门，防渲染层把本 IPC 当任意路径探测用
    assertProgramName(name)
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
      // 与 backend-manager 孤儿检测同源：PATH 被裁剪的环境 spawn 裸名 'powershell' 会 ENOENT，
      // 用 resolvePowerShell 解析绝对路径（回退口径见 utils/powershell-path.ts）
      await execFileAsync(resolvePowerShell(), ['-NoProfile', '-Command', psCmd], 300)
    } else {
      // Linux/macOS: zip -r -y -- <zip> <src1> <src2> ...
      // `--` 结束选项解析：以 `-` 开头的源路径（如 `-out`）否则会被 zip 当作选项吞掉
      const args = ['-r', '-y', '--', localZipPath, ...sources]
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
      // 同 zipLocal：resolvePowerShell 出绝对路径，避免 PATH 裁剪环境下裸名 ENOENT
      await execFileAsync(resolvePowerShell(), ['-NoProfile', '-Command', psCmd], 300)
    } else {
      // -o 覆盖不询问，-q 安静，-d 指定目录
      await execFileAsync('unzip', ['-o', '-q', localZipPath, '-d', localDestDir], 300)
    }
  })
}
