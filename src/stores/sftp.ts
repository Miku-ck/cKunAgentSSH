/**
 * SFTP Store — 管理 SFTP 双栏文件传输状态
 * 左栏(本地)走现有 fs* IPC,右栏(远程)走 sftp:* IPC
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useToastStore } from './toast'
import { toFriendlyMessage } from '@/api/client'

export interface TransferItem {
  id: string
  direction: 'up' | 'down'
  name: string
  from: string
  to: string
  transferred: number
  total: number
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  error?: string
  startTime: number
  endTime?: number
}

export const useSftpStore = defineStore('sftp', () => {
  const toast = useToastStore()

  /** 安全获取 electronAPI，未就绪时抛出明确错误 */
  function getApi(): ElectronAPI {
    if (!window.electronAPI) throw new Error('Electron API 未就绪')
    return window.electronAPI
  }

  // ─── 会话状态 ─────────────────────────────────
  const sshSessionId = ref('')
  const connectionName = ref('')
  const connectionHost = ref('')
  const online = ref(false)

  // ─── 双栏目录 ─────────────────────────────────
  const localCwd = ref('')
  const remoteCwd = ref('')
  const localEntries = ref<FileEntry[]>([])
  const remoteEntries = ref<SftpEntry[]>([])
  const loadingLocal = ref(false)
  const loadingRemote = ref(false)
  const localError = ref('')
  const remoteError = ref('')

  // ─── 选择 ─────────────────────────────────────
  const selectedLocal = ref<Set<string>>(new Set())
  const selectedRemote = ref<Set<string>>(new Set())

  // ─── 传输队列 ─────────────────────────────────
  const transfers = ref<TransferItem[]>([])
  let transferSeq = 1
  // 批量传输并发上限：同一 SFTP 通道同时传输多个文件会互相争抢导致整体吞吐降低
  // 经验值 2-3 较优，超过 4 后单文件速度明显下降
  const MAX_CONCURRENT_TRANSFERS = 3
  // 当前正在运行的传输数
  const runningCount = computed(() => transfers.value.filter(t => t.status === 'running').length)

  const activeTransfers = computed(() =>
    transfers.value.filter(t => t.status === 'running' || t.status === 'pending')
  )

  // ─── 绑定 / 解绑 SSH 会话 ────────────────────
  /** 直接通过 SSH 连接 + SFTP 子系统打开远程会话(独立于终端会话) */
  async function connect(connection: SSHConnectionRecord): Promise<void> {
    if (online.value) await disconnect()
    const plain = JSON.parse(JSON.stringify(connection))
    const { id, name } = await getApi().sshConnectSftp(plain)
    sshSessionId.value = id
    connectionName.value = name || `${connection.username}@${connection.host}`
    connectionHost.value = `${connection.username}@${connection.host}:${connection.port}`
    try {
      await getApi().sftpOpen(id)
      online.value = true
      remoteCwd.value = '/'
      await loadRemote('/')
    } catch (err: any) {
      online.value = false
      remoteError.value = err?.message || 'SFTP 打开失败'
      // 失败时关闭 SSH
      try { await getApi().sshDisconnect(id) } catch { /* ignore */ }
      sshSessionId.value = ''
      throw err
    }
  }

  async function disconnect(): Promise<void> {
    if (sshSessionId.value) {
      try { await getApi().sftpClose(sshSessionId.value) } catch { /* ignore */ }
      try { await getApi().sshDisconnect(sshSessionId.value) } catch { /* ignore */ }
    }
    sshSessionId.value = ''
    connectionName.value = ''
    connectionHost.value = ''
    online.value = false
    remoteCwd.value = ''
    remoteEntries.value = []
    selectedRemote.value = new Set()
    remoteError.value = ''
  }

  // ─── 本地侧操作 ───────────────────────────────
  async function loadLocal(dirPath?: string): Promise<void> {
    const target = dirPath ?? localCwd.value
    if (!target) return
    loadingLocal.value = true
    localError.value = ''
    try {
      const entries = await getApi().fsReadDir(target)
      localEntries.value = entries
      localCwd.value = target
      selectedLocal.value = new Set()
    } catch (err: any) {
      localError.value = err?.message || '读取本地目录失败'
    } finally {
      loadingLocal.value = false
    }
  }

  async function initLocalHome(): Promise<void> {
    const home = await getApi().fsGetHomePath()
    await loadLocal(home)
  }

  function localParent(): string {
    const sep = localCwd.value.includes('\\') ? '\\' : '/'
    const parts = localCwd.value.split(sep).filter(Boolean)
    if (parts.length <= 1) return localCwd.value
    // Windows 盘符根: C:\ → 保留
    if (/^[a-z]:$/i.test(parts[0]) && parts.length === 2) {
      return parts[0] + sep
    }
    return parts.slice(0, -1).join(sep) || (sep)
  }

  async function localUp(): Promise<void> {
    await loadLocal(localParent())
  }

  async function localRefresh(): Promise<void> {
    await loadLocal(localCwd.value)
  }

  async function localMkdir(name: string): Promise<void> {
    const newPath = await getApi().fsPathJoin(localCwd.value, name)
    await getApi().fsMkdir(newPath)
    await localRefresh()
  }

  async function localRename(oldPath: string, newName: string): Promise<void> {
    const parent = oldPath.includes('\\')
      ? oldPath.split('\\').slice(0, -1).join('\\')
      : oldPath.split('/').slice(0, -1).join('/')
    const newPath = await getApi().fsPathJoin(parent, newName)
    await getApi().fsRename(oldPath, newPath)
    await localRefresh()
  }

  async function localDelete(path: string): Promise<void> {
    await getApi().fsTrash(path)
    await localRefresh()
  }

  // ─── 远程侧操作 ───────────────────────────────
  function remoteParent(): string {
    if (!remoteCwd.value || remoteCwd.value === '/' || remoteCwd.value === '.') return remoteCwd.value
    const parts = remoteCwd.value.split('/').filter(Boolean)
    if (parts.length === 0) return '/'
    return '/' + parts.slice(0, -1).join('/')
  }

  async function loadRemote(dirPath?: string): Promise<void> {
    if (!sshSessionId.value) return
    const target = dirPath ?? remoteCwd.value
    if (!target) return
    loadingRemote.value = true
    remoteError.value = ''
    try {
      const entries = await getApi().sftpList(sshSessionId.value, target)
      remoteEntries.value = entries
      remoteCwd.value = target
      selectedRemote.value = new Set()
    } catch (err: any) {
      remoteError.value = err?.message || '读取远程目录失败'
    } finally {
      loadingRemote.value = false
    }
  }

  async function remoteUp(): Promise<void> {
    await loadRemote(remoteParent())
  }

  async function remoteRefresh(): Promise<void> {
    await loadRemote(remoteCwd.value)
  }

  async function remoteMkdir(name: string): Promise<void> {
    const newPath = remoteCwd.value === '/'
      ? '/' + name
      : remoteCwd.value + '/' + name
    await getApi().sftpMkdir(sshSessionId.value, newPath)
    await remoteRefresh()
  }

  async function remoteRename(oldPath: string, newName: string): Promise<void> {
    const parent = oldPath.split('/').slice(0, -1).join('/') || '/'
    const newPath = parent === '/' ? '/' + newName : parent + '/' + newName
    await getApi().sftpRename(sshSessionId.value, oldPath, newPath)
    await remoteRefresh()
  }

  async function remoteDelete(path: string): Promise<void> {
    await getApi().sftpDelete(sshSessionId.value, path)
    await remoteRefresh()
  }

  // ─── 选择 ─────────────────────────────────────
  function toggleSelectLocal(path: string): void {
    const s = new Set(selectedLocal.value)
    if (s.has(path)) s.delete(path)
    else s.add(path)
    selectedLocal.value = s
  }

  function toggleSelectRemote(path: string): void {
    const s = new Set(selectedRemote.value)
    if (s.has(path)) s.delete(path)
    else s.add(path)
    selectedRemote.value = s
  }

  function clearSelection(side?: 'local' | 'remote'): void {
    if (side === 'local') {
      selectedLocal.value = new Set()
    } else if (side === 'remote') {
      selectedRemote.value = new Set()
    } else {
      selectedLocal.value = new Set()
      selectedRemote.value = new Set()
    }
  }

  // ─── 传输 ─────────────────────────────────────
  /**
   * 递归展开本地目录，返回所有文件的本地绝对路径 + 相对根目录的相对路径
   * 用于目录上传场景
   */
  async function expandLocalDir(localDir: string): Promise<Array<{ localPath: string; relPath: string }>> {
    const result: Array<{ localPath: string; relPath: string }> = []
    const stack: Array<{ dir: string; rel: string }> = [{ dir: localDir, rel: '' }]
    while (stack.length > 0) {
      const { dir, rel } = stack.pop()!
      let entries: FileEntry[]
      try {
        entries = await getApi().fsReadDir(dir)
      } catch {
        continue  // 无权限的子目录跳过
      }
      for (const e of entries) {
        const childRel = rel ? `${rel}/${e.name}` : e.name
        if (e.isDirectory) {
          stack.push({ dir: e.path, rel: childRel })
        } else {
          result.push({ localPath: e.path, relPath: childRel })
        }
      }
    }
    return result
  }

  /**
   * 递归展开远程目录，返回所有文件的远程绝对路径 + 相对根目录的相对路径
   * 用于目录下载场景
   */
  async function expandRemoteDir(remoteDir: string): Promise<Array<{ remotePath: string; relPath: string }>> {
    const result: Array<{ remotePath: string; relPath: string }> = []
    const stack: Array<{ dir: string; rel: string }> = [{ dir: remoteDir, rel: '' }]
    while (stack.length > 0) {
      const { dir, rel } = stack.pop()!
      let entries: SftpEntry[]
      try {
        entries = await getApi().sftpList(sshSessionId.value, dir)
      } catch {
        continue
      }
      for (const e of entries) {
        const childRel = rel ? `${rel}/${e.name}` : e.name
        if (e.isDirectory) {
          stack.push({ dir: e.path, rel: childRel })
        } else {
          result.push({ remotePath: e.path, relPath: childRel })
        }
      }
    }
    return result
  }

  /**
   * 批量上传：将所有任务加入队列，按并发上限调度执行
   * 支持目录递归：目录会被展开为多个文件上传任务，远程目录结构自动创建
   * 所有任务完成后再统一刷新目录，避免 N 个文件触发 N 次刷新
   */
  async function uploadFiles(localPaths: string[]): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const tasks: Array<{ id: string; localPath: string; remotePath: string }> = []

    for (const lp of localPaths) {
      const name = basenameLocal(lp)
      const remoteBase = remoteCwd.value === '/' ? '/' + name : remoteCwd.value + '/' + name

      // 判断是文件还是目录：优先查 localEntries（应用内拖拽），否则用 fsStat（OS 拖拽）
      let isDir = false
      const localEntry = localEntries.value.find(e => e.path === lp)
      if (localEntry) {
        isDir = localEntry.isDirectory
      } else {
        try {
          const st = await getApi().fsStat(lp)
          isDir = st.isDirectory
        } catch {
          // 无法获取信息，当作文件处理
        }
      }

      if (isDir) {
        // 目录：递归展开
        const files = await expandLocalDir(lp)
        for (const f of files) {
          const remotePath = remoteBase + '/' + f.relPath
          const remoteDir = remotePath.substring(0, remotePath.lastIndexOf('/'))
          // 确保远程父目录存在
          try { await getApi().sftpMkdirRecursive(sshSessionId.value, remoteDir) } catch { /* ignore */ }
          const id = `t${transferSeq++}`
          transfers.value.push({
            id, direction: 'up', name: f.relPath,
            from: f.localPath, to: remotePath,
            transferred: 0, total: 0,
            status: 'pending', startTime: Date.now(),
          })
          tasks.push({ id, localPath: f.localPath, remotePath })
        }
      } else {
        // 文件：直接加入
        const id = `t${transferSeq++}`
        transfers.value.push({
          id, direction: 'up', name,
          from: lp, to: remoteBase,
          transferred: 0, total: 0,
          status: 'pending', startTime: Date.now(),
        })
        tasks.push({ id, localPath: lp, remotePath: remoteBase })
      }
    }

    runBatch(tasks.map(t => () => runUpload(t.id, t.localPath, t.remotePath))).then(async (results) => {
      // 至少有一个成功才刷新目录，避免无谓请求
      if (results.some(r => r.ok)) await remoteRefresh()
    })
  }

  /** 下载多个远程文件到当前本地目录（支持目录递归） */
  async function downloadFiles(remotePaths: string[]): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const tasks: Array<{ id: string; remotePath: string; localPath: string }> = []

    for (const rp of remotePaths) {
      const name = basenameRemote(rp)
      const localBase = await getApi().fsPathJoin(localCwd.value, name)

      // 判断是文件还是目录：优先查 remoteEntries，否则用 sftpStat
      let isDir = false
      const remoteEntry = remoteEntries.value.find(e => e.path === rp)
      if (remoteEntry) {
        isDir = remoteEntry.isDirectory
      }

      if (isDir) {
        // 目录：递归展开
        const files = await expandRemoteDir(rp)
        for (const f of files) {
          const localPath = await getApi().fsPathJoin(localBase, f.relPath)
          // 确保本地父目录存在（fsMkdir 是 recursive）
          const localDir = localPath.includes('\\')
            ? localPath.split('\\').slice(0, -1).join('\\')
            : localPath.split('/').slice(0, -1).join('/')
          try { await getApi().fsMkdir(localDir) } catch { /* ignore */ }
          const id = `t${transferSeq++}`
          transfers.value.push({
            id, direction: 'down', name: f.relPath,
            from: f.remotePath, to: localPath,
            transferred: 0, total: 0,
            status: 'pending', startTime: Date.now(),
          })
          tasks.push({ id, remotePath: f.remotePath, localPath })
        }
      } else {
        // 文件：直接加入
        const id = `t${transferSeq++}`
        transfers.value.push({
          id, direction: 'down', name,
          from: rp, to: localBase,
          transferred: 0, total: 0,
          status: 'pending', startTime: Date.now(),
        })
        tasks.push({ id, remotePath: rp, localPath: localBase })
      }
    }

    runBatch(tasks.map(t => () => runDownload(t.id, t.remotePath, t.localPath))).then(async (results) => {
      if (results.some(r => r.ok)) await localRefresh()
    })
  }

  // ─── ZIP 打包/解压 ──────────────────────────────
  /**
   * 本地压缩：将选中的本地文件/目录打包为 zip
   * @param localPaths 要压缩的本地路径列表
   * @param zipName zip 文件名（不含路径，会放在当前本地目录）
   */
  async function zipLocal(localPaths: string[], zipName: string): Promise<void> {
    if (!localPaths.length) return
    const zipPath = await getApi().fsPathJoin(localCwd.value, zipName)
    toast.info(`正在压缩: ${zipName}…`)
    try {
      await getApi().fsZipLocal(localPaths, zipPath)
      toast.success(`已压缩: ${zipName}`)
      await localRefresh()
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '压缩失败'))
      throw err
    }
  }

  /**
   * 本地解压：将当前本地目录下的 zip 文件解压到同名目录
   * @param localZipPath zip 文件本地路径
   * @param destDirName 解压目标目录名（会创建在当前本地目录下）
   */
  async function unzipLocal(localZipPath: string, destDirName: string): Promise<void> {
    const destDir = await getApi().fsPathJoin(localCwd.value, destDirName)
    toast.info(`正在解压: ${destDirName}…`)
    try {
      await getApi().fsUnzipLocal(localZipPath, destDir)
      toast.success(`已解压: ${destDirName}`)
      await localRefresh()
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '解压失败'))
      throw err
    }
  }

  /**
   * 远程压缩：将选中的远程文件/目录打包为 zip
   * @param remotePaths 要压缩的远程路径列表
   * @param zipName zip 文件名（不含路径，会放在当前远程目录）
   */
  async function zipRemote(remotePaths: string[], zipName: string): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    if (!remotePaths.length) return
    const remoteZipPath = remoteCwd.value === '/' ? '/' + zipName : remoteCwd.value + '/' + zipName
    // sources 用相对 remoteCwd 的路径（这样 zip 内部路径更短）
    // Bug 2 修复：remoteCwd === '/' 时不能直接拼 '//', 否则 startsWith 永远 false,
    // 导致 sources 全是绝对路径, zip 内部结构会以 / 开头
    const sources = remotePaths.map(p => {
      if (p === remoteCwd.value) return '.'
      if (remoteCwd.value === '/') {
        // 根目录：去掉开头的 / 即为相对路径
        return p.startsWith('/') ? p.substring(1) : p
      }
      const rel = p.startsWith(remoteCwd.value + '/')
        ? p.substring(remoteCwd.value.length + 1)
        : p
      return rel || p
    })
    toast.info(`正在远程压缩: ${zipName}…`)
    try {
      await getApi().sftpZipRemote(sshSessionId.value, sources, remoteZipPath, remoteCwd.value)
      toast.success(`已压缩: ${zipName}`)
      await remoteRefresh()
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '远程压缩失败'))
      throw err
    }
  }

  /**
   * 远程解压：将远程 zip 文件解压到同名目录
   * @param remoteZipPath zip 文件远程路径
   * @param destDirName 解压目标目录名（会创建在当前远程目录下）
   */
  async function unzipRemote(remoteZipPath: string, destDirName: string): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const destDir = remoteCwd.value === '/' ? '/' + destDirName : remoteCwd.value + '/' + destDirName
    toast.info(`正在远程解压: ${destDirName}…`)
    try {
      await getApi().sftpUnzipRemote(sshSessionId.value, remoteZipPath, destDir)
      toast.success(`已解压: ${destDirName}`)
      await remoteRefresh()
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, '远程解压失败'))
      throw err
    }
  }

  // ─── 路径辅助（用于 ZIP 覆盖确认等场景） ────────
  /** 拼接远程子路径（基于当前 remoteCwd） */
  function joinRemotePath(name: string): string {
    if (!name) return remoteCwd.value
    if (name.startsWith('/')) return name
    return remoteCwd.value === '/' ? '/' + name : remoteCwd.value + '/' + name
  }

  /** 拼接本地子路径（基于当前 localCwd） */
  async function joinLocalPath(name: string): Promise<string> {
    if (!name) return localCwd.value
    if (/^[a-z]:[\\/]/i.test(name) || name.startsWith('/')) return name
    return await getApi().fsPathJoin(localCwd.value, name)
  }

  /** 检查远程路径是否存在（文件或目录）。未连接时返回 false */
  async function remotePathExists(path: string): Promise<boolean> {
    if (!sshSessionId.value || !online.value || !path) return false
    try {
      return await getApi().sftpExists(sshSessionId.value, path)
    } catch {
      return false
    }
  }

  /** 检查本地路径是否存在（文件或目录） */
  async function localPathExists(path: string): Promise<boolean> {
    if (!path) return false
    try {
      await getApi().fsStat(path)
      return true
    } catch {
      return false
    }
  }

  /**
   * 并发调度器：限制同时运行的传输任务数为 MAX_CONCURRENT_TRANSFERS
   * 返回每个任务的执行结果（ok/错误），全部完成才 resolve
   */
  function runBatch<T>(
    taskFns: Array<() => Promise<T>>,
  ): Promise<Array<{ ok: boolean; result?: T; error?: any }>> {
    return new Promise((resolve) => {
      const total = taskFns.length
      // 空任务直接 resolve，避免 Promise 永久挂起
      if (total === 0) {
        resolve([])
        return
      }
      const results: Array<{ ok: boolean; result?: T; error?: any }> = []
      let nextIdx = 0
      let finishedCount = 0

      function scheduleNext(): void {
        // 队列已空或并发已满，停止派发
        while (
          runningCount.value < MAX_CONCURRENT_TRANSFERS &&
          nextIdx < total
        ) {
          const idx = nextIdx++
          const task = taskFns[idx]
          task()
            .then((result) => { results[idx] = { ok: true, result } })
            .catch((error) => { results[idx] = { ok: false, error } })
            .finally(() => {
              finishedCount++
              if (finishedCount >= total) resolve(results)
              else scheduleNext()
            })
        }
      }
      scheduleNext()
    })
  }

  async function runUpload(id: string, localPath: string, remotePath: string, retried = false): Promise<void> {
    const t = transfers.value.find(x => x.id === id)
    if (!t) return
    t.status = 'running'
    try {
      await getApi().sftpUpload(id, sshSessionId.value, localPath, remotePath)
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`上传成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      const code = err?.code as string | undefined
      const retryable = err?.retryable as boolean | undefined
      // NETWORK/STALLED 错误自动重试一次（断点续传）
      if (retryable && !retried) {
        toast.info(`正在自动续传: ${t.name}…`)
        return runUpload(id, localPath, remotePath, true)
      }
      t.status = code === 'CANCELLED' ? 'cancelled' : 'error'
      t.error = err?.message || '上传失败'
      t.endTime = Date.now()
      // 取消是用户行为，不弹错误 toast
      if (code !== 'CANCELLED') {
        toast.error(`上传失败: ${t.name} — ${t.error}`)
      }
      // Bug 6 修复：错误/取消任务也自动移除（延长到 10s 让用户看到错误信息）
      scheduleRemoveTransfer(id, 10000)
    }
  }

  async function runDownload(id: string, remotePath: string, localPath: string, retried = false): Promise<void> {
    const t = transfers.value.find(x => x.id === id)
    if (!t) return
    t.status = 'running'
    try {
      await getApi().sftpDownload(id, sshSessionId.value, remotePath, localPath)
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`下载成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      const code = err?.code as string | undefined
      const retryable = err?.retryable as boolean | undefined
      // NETWORK/STALLED 错误自动重试一次（断点续传）
      if (retryable && !retried) {
        toast.info(`正在自动续传: ${t.name}…`)
        return runDownload(id, remotePath, localPath, true)
      }
      t.status = code === 'CANCELLED' ? 'cancelled' : 'error'
      t.error = err?.message || '下载失败'
      t.endTime = Date.now()
      if (code !== 'CANCELLED') {
        toast.error(`下载失败: ${t.name} — ${t.error}`)
      }
      // Bug 6 修复：错误/取消任务也自动移除（延长到 10s 让用户看到错误信息）
      scheduleRemoveTransfer(id, 10000)
    }
  }

  async function cancelTransfer(id: string): Promise<void> {
    try { await getApi().sftpCancel(id) } catch { /* ignore */ }
  }

  function removeTransfer(id: string): void {
    const idx = transfers.value.findIndex(t => t.id === id)
    if (idx >= 0) transfers.value.splice(idx, 1)
  }

  function scheduleRemoveTransfer(id: string, delay = 3000): void {
    setTimeout(() => removeTransfer(id), delay)
  }

  /** 由 SftpView 在 onMounted 订阅 onSftpProgress 后调用 */
  function applyProgress(data: { id: string; transferred: number; total: number }): void {
    const t = transfers.value.find(x => x.id === data.id)
    if (!t) return
    t.transferred = data.transferred
    t.total = data.total
  }

  return {
    // state
    sshSessionId, connectionName, connectionHost, online,
    localCwd, remoteCwd, localEntries, remoteEntries,
    loadingLocal, loadingRemote, localError, remoteError,
    selectedLocal, selectedRemote, transfers,
    // computed
    activeTransfers,
    // session
    connect, disconnect,
    // local
    loadLocal, initLocalHome, localUp, localRefresh, localMkdir, localRename, localDelete,
    // remote
    loadRemote, remoteUp, remoteRefresh, remoteMkdir, remoteRename, remoteDelete,
    // selection
    toggleSelectLocal, toggleSelectRemote, clearSelection,
    // transfer
    uploadFiles, downloadFiles, cancelTransfer, removeTransfer, applyProgress,
    // zip
    zipLocal, unzipLocal, zipRemote, unzipRemote,
    // 路径辅助（用于 ZIP 覆盖确认等场景）
    joinRemotePath, joinLocalPath, remotePathExists, localPathExists,
  }
})

function basenameLocal(path: string): string {
  const idx = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'))
  return idx >= 0 ? path.substring(idx + 1) : path
}

function basenameRemote(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx >= 0 ? path.substring(idx + 1) : path
}
