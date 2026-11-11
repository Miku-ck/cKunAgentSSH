/**
 * SFTP 文件面 composable — 双栏目录、排序/筛选、选择、导航操作、ZIP 与路径辅助。
 *
 * 从 stores/sftp.ts 拆出（docs/structure-refactor-plan.md P1）：会话态（sshSessionId /
 * online）由 store 持有并经只读 computed 注入，本模块不写会话状态；函数体自
 * stores/sftp.ts 原样平移，行为由 sftp-sort-filter / sftp-open-race / sftp 三个
 * 测试文件经由 useSftpStore 门面钉住。
 */
import { computed, ref } from 'vue'
import { runWithActionToast } from '@/utils/toast-action'
import { toFriendlyMessage, unwrapIpcErrorEnvelope } from '@/api/client'
import { extractSSHError } from '@/types/ssh-error-types'
import { isUnsafeRemoteName } from '@/utils/remote-name'
import type { useToastStore } from '@/stores/toast'

type ToastStore = ReturnType<typeof useToastStore>

// B20 修复：目录加载请求序号（模块级单调递增）。
// 快速连点 ".." 时慢的旧请求后返回会覆盖新结果，导致地址栏与列表错配。
// 参照 App.vue 背景图 bgSeq 模式：await 后序号不匹配即丢弃过期结果。
let localLoadSeq = 0
let remoteLoadSeq = 0

export interface SftpFilesDeps {
  /** 安全获取 electronAPI，未就绪时抛出明确错误 */
  getApi: () => ElectronAPI
  sshSessionId: () => string
  online: () => boolean
  toast: ToastStore
}

export function useSftpFiles(deps: SftpFilesDeps) {
  const { getApi } = deps
  const toast = deps.toast
  // 会话态只读视图：让平移过来的函数体保持 `.value` 读法不变
  const sshSessionId = computed(deps.sshSessionId)
  const online = computed(deps.online)

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
  /**
   * Shift 连续选择的锚点（S5-23）：记录每侧「最后单独点选」的路径，
   * 与 displayEntries 的可见序（已排序/筛选）配合做区间选中。
   * null = 尚未在本侧点过项（首次 Shift 按 0 处理）。
   */
  const lastSelectedLocal = ref<string | null>(null)
  const lastSelectedRemote = ref<string | null>(null)

  // ─── 列表排序 / 筛选（S5-23 / S5-24） ──────────
  type SortKey = 'name' | 'size' | 'modified'
  type SortDir = 'asc' | 'desc'
  const localSort = ref<{ key: SortKey; dir: SortDir }>({ key: 'name', dir: 'asc' })
  const remoteSort = ref<{ key: SortKey; dir: SortDir }>({ key: 'name', dir: 'asc' })
  // 空串 = 不筛选。只匹配名称（大小写不敏感），与常见文件管理器口径一致
  const localFilter = ref('')
  const remoteFilter = ref('')

  function sortEntries<T extends { name: string; size: number; modified: number; isDirectory: boolean }>(
    list: T[],
    key: SortKey,
    dir: SortDir,
  ): T[] {
    const sign = dir === 'asc' ? 1 : -1
    // 目录恒在前（与 Windows 资源管理器 / Finder 同口径），组内再按所选列排序
    return [...list].sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1
      let cmp = 0
      if (key === 'name') {
        // localeCompare 认大小写与数字的自然序（a2 排在 a10 前）
        cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
      } else if (key === 'size') {
        cmp = a.size - b.size
      } else {
        cmp = a.modified - b.modified
      }
      // 同值按名称兜底，保证排序稳定（否则同大小文件在反复切换排序方向时抖动）
      return cmp !== 0 ? cmp * sign : a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
    })
  }

  function filterEntries<T extends { name: string }>(list: T[], keyword: string): T[] {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return list
    return list.filter(e => e.name.toLowerCase().includes(kw))
  }

  function setSort(side: 'local' | 'remote', key: SortKey): void {
    const cur = side === 'local' ? localSort.value : remoteSort.value
    // 同列再点 → 翻转方向；换列 → 默认升序（名称列首次点击即升序）
    const dir: SortDir = cur.key === key ? (cur.dir === 'asc' ? 'desc' : 'asc') : 'asc'
    const next = { key, dir }
    if (side === 'local') localSort.value = next
    else remoteSort.value = next
  }

  // ─── 本地侧操作 ───────────────────────────────
  /** @param dirPath 目标目录（默认当前目录） @param silent 静默刷新（不触发 loading 指示，防闪烁） */
  async function loadLocal(dirPath?: string, silent = false): Promise<void> {
    const target = dirPath ?? localCwd.value
    if (!target) return
    // B20 修复：请求序号 — 过期请求（快速连点 ".."）返回时直接丢弃，
    // 防止慢的旧列表覆盖新目录、地址栏与列表错配
    const mySeq = ++localLoadSeq
    if (!silent) loadingLocal.value = true
    localError.value = ''
    try {
      const entries = await getApi().fsReadDir(target)
      if (mySeq !== localLoadSeq) return // 已有更新请求，丢弃过期结果
      localEntries.value = entries
      localCwd.value = target
      selectedLocal.value = new Set()
    } catch (err: any) {
      if (mySeq !== localLoadSeq) return
      localError.value = unwrapIpcErrorEnvelope(err?.message || '') || '读取本地目录失败'
    } finally {
      // 仅最新请求负责复位 loading（&& !silent 会让「非静默导航 + 期间发起的静默刷新」
      // 两条请求都跳过复位 —— 列表已加载完却永久停在「加载中...」）
      if (mySeq === localLoadSeq) loadingLocal.value = false
    }
  }

  async function initLocalHome(): Promise<void> {
    const home = await getApi().fsGetHomePath()
    await loadLocal(home)
  }

  /** 获取 Windows 可用盘符列表，非 Windows 返回空数组 */
  async function getDrives(): Promise<string[]> {
    try {
      return await getApi().fsGetDrives()
    } catch {
      return []
    }
  }

  function localParent(): string {
    const sep = localCwd.value.includes('\\') ? '\\' : '/'
    const parts = localCwd.value.split(sep).filter(Boolean)
    if (parts.length <= 1) {
      // POSIX 一级绝对目录（/home、/tmp、/root）仍可再上到根 '/'；原实现一刀切
      // 返回原值，使「上一级」在深度 1 处空转（远端 remoteParent 已正确处理）。
      // 仅收紧这一种情形：Windows 盘符根 C:\ 已在根、保持原样返回。
      if (sep === '/' && parts.length === 1 && localCwd.value.startsWith('/')) return '/'
      return localCwd.value
    }
    // Windows 盘符根: C:\ → 保留
    if (/^[a-z]:$/i.test(parts[0]) && parts.length === 2) {
      return parts[0] + sep
    }
    // POSIX 下 split 把开头的 '/' 当作空段丢掉，直接 join 会得到 'home' 这种
    // 相对路径（readDir 报错或落到错误目录）—— 根分隔符必须补回
    const root = localCwd.value.startsWith(sep) ? sep : ''
    return root + parts.slice(0, -1).join(sep) || sep
  }

  async function localUp(): Promise<void> {
    await loadLocal(localParent())
  }

  async function localRefresh(): Promise<void> {
    await loadLocal(localCwd.value, true) // 静默刷新，避免闪烁
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

  /**
   * 删除本地条目。Q-F7：批量删除逐项调用时会触发 N 次全量刷新 ——
   * 传 skipRefresh 抑制内部刷新，由调用方在循环结束后统一刷新一次。
   * 默认不传时行为不变（删完即刷新）。
   */
  async function localDelete(path: string, opts?: { skipRefresh?: boolean }): Promise<void> {
    await getApi().fsTrash(path)
    if (!opts?.skipRefresh) await localRefresh()
  }

  // ─── 远程侧操作 ───────────────────────────────
  function remoteParent(): string {
    if (!remoteCwd.value || remoteCwd.value === '/' || remoteCwd.value === '.') return remoteCwd.value
    const parts = remoteCwd.value.split('/').filter(Boolean)
    if (parts.length === 0) return '/'
    return '/' + parts.slice(0, -1).join('/')
  }

  /**
   * @param dirPath 目标目录
   * @param silent 是否静默加载（不显示 loading 状态，用于刷新操作避免闪烁）
   */
  async function loadRemote(dirPath?: string, silent = false): Promise<void> {
    if (!sshSessionId.value) return
    const target = dirPath ?? remoteCwd.value
    if (!target) return
    // B20 修复：请求序号（同 loadLocal）
    const mySeq = ++remoteLoadSeq
    if (!silent) loadingRemote.value = true
    remoteError.value = ''
    try {
      const entries = await getApi().sftpList(sshSessionId.value, target)
      if (mySeq !== remoteLoadSeq) return // 已有更新请求，丢弃过期结果
      remoteEntries.value = entries
      remoteCwd.value = target
      selectedRemote.value = new Set()
    } catch (err: any) {
      if (mySeq !== remoteLoadSeq) return
      remoteError.value = unwrapIpcErrorEnvelope(err?.message || '') || '读取远程目录失败'
    } finally {
      // 与 loadLocal 同步：最新请求一律复位，静默与否只影响是否显示 loading
      if (mySeq === remoteLoadSeq) loadingRemote.value = false
    }
  }

  async function remoteUp(): Promise<void> {
    await loadRemote(remoteParent())
  }

  async function remoteRefresh(): Promise<void> {
    await loadRemote(remoteCwd.value, true) // 静默刷新，避免闪烁
  }

  async function remoteMkdir(name: string): Promise<void> {
    const newPath = remoteCwd.value === '/'
      ? '/' + name
      : remoteCwd.value + '/' + name
    await getApi().sftpMkdir(sshSessionId.value, newPath)
    await remoteRefresh()
  }

  /**
   * 修改远程文件权限位（S5-22 chmod）。成功后静默刷新列表，让权限列立刻反映新值。
   */
  async function remoteChmod(path: string, mode: number): Promise<void> {
    await getApi().sftpChmod(sshSessionId.value, path, mode)
    await remoteRefresh()
  }

  async function remoteRename(oldPath: string, newName: string): Promise<void> {
    const parent = oldPath.split('/').slice(0, -1).join('/') || '/'
    const newPath = parent === '/' ? '/' + newName : parent + '/' + newName
    await getApi().sftpRename(sshSessionId.value, oldPath, newPath)
    await remoteRefresh()
  }

  /** 删除远程条目。skipRefresh 语义与 localDelete 相同（批量删除合批刷新用）。 */
  async function remoteDelete(path: string, opts?: { skipRefresh?: boolean }): Promise<void> {
    await getApi().sftpDelete(sshSessionId.value, path)
    if (!opts?.skipRefresh) await remoteRefresh()
  }

  // ─── 选择 ─────────────────────────────────────
  /** 单选/普通点选：只翻转该路径，并把 Shift 锚点钉到它 */
  function toggleSelectLocal(path: string): void {
    const s = new Set(selectedLocal.value)
    if (s.has(path)) s.delete(path)
    else s.add(path)
    selectedLocal.value = s
    lastSelectedLocal.value = path
  }

  function toggleSelectRemote(path: string): void {
    const s = new Set(selectedRemote.value)
    if (s.has(path)) s.delete(path)
    else s.add(path)
    selectedRemote.value = s
    lastSelectedRemote.value = path
  }

  /**
   * Shift 连续选择（S5-23）：在**当前可见序**里取锚点与目标之间的闭区间，全部选中。
   * 锚点缺失（用户没在本侧点过项）时按列表首项处理 —— 这是各文件管理器的通行口径。
   * 注意 visible 必须与渲染层 displayEntries 的口径一致（筛选 → 排序 → 截断），
   * 否则区间会选到用户看不见的项，看起来像「多选了几个」。
   */
  function selectRange(side: 'local' | 'remote', targetPath: string, visible: string[]): void {
    if (visible.length === 0) return
    const anchorPath = side === 'local' ? lastSelectedLocal.value : lastSelectedRemote.value
    const a = anchorPath ? visible.indexOf(anchorPath) : 0
    const b = visible.indexOf(targetPath)
    const from = a >= 0 ? a : 0
    const to = b >= 0 ? b : visible.length - 1
    const [lo, hi] = from <= to ? [from, to] : [to, from]
    const next = side === 'local' ? new Set(selectedLocal.value) : new Set(selectedRemote.value)
    for (let i = lo; i <= hi; i++) next.add(visible[i])
    if (side === 'local') {
      selectedLocal.value = next
      lastSelectedLocal.value = targetPath
    } else {
      selectedRemote.value = next
      lastSelectedRemote.value = targetPath
    }
  }

  /** Ctrl/Cmd+A 全选（S5-23）：只选当前可见项 —— 筛选后全选应只覆盖筛选结果 */
  function selectAll(side: 'local' | 'remote', visible: string[]): void {
    if (side === 'local') selectedLocal.value = new Set(visible)
    else selectedRemote.value = new Set(visible)
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

  // ─── 目录展开（传输入队用） ────────────────────
  /**
   * 递归展开本地目录，返回所有文件的本地绝对路径 + 相对根目录的相对路径
   * 用于目录上传场景
   */
  async function expandLocalDir(localDir: string): Promise<Array<{ localPath: string; relPath: string; size: number }>> {
    const result: Array<{ localPath: string; relPath: string; size: number }> = []
    const stack: Array<{ dir: string; rel: string }> = [{ dir: localDir, rel: '' }]
    // P3 修复：收集遍历中因不可读（无权限）而跳过的子目录，结束后统一告警
    // —— 原实现静默 continue，用户无从得知上传的目录结构不完整
    const skipped: string[] = []
    while (stack.length > 0) {
      const { dir, rel } = stack.pop()!
      let entries: FileEntry[]
      try {
        entries = await getApi().fsReadDir(dir)
      } catch {
        skipped.push(dir)  // 无权限的子目录跳过
        continue
      }
      for (const e of entries) {
        const childRel = rel ? `${rel}/${e.name}` : e.name
        if (e.isDirectory) {
          stack.push({ dir: e.path, rel: childRel })
        } else {
          // 保留 FileEntry.size（fsReadDir 已带）：大文件优先排序要用
          result.push({ localPath: e.path, relPath: childRel, size: e.size || 0 })
        }
      }
    }
    if (skipped.length > 0) {
      toast.warning(`部分子目录因无权限被跳过（${skipped.length} 个）：${skipped.slice(0, 3).join('、')}${skipped.length > 3 ? ' …' : ''}`)
    }
    return result
  }

  /**
   * 递归展开远程目录，返回所有文件的远程绝对路径 + 相对根目录的相对路径
   * 用于目录下载场景
   *
   * S5-12 修复：会话 id 由调用方钉死后传入，不再读实时的 `sshSessionId.value` ——
   * 展开大目录要 await 多次 IPC，期间切换活动终端会让后续 readdir 打到另一台主机
   * 的同名路径上（uploadFiles 早就钉 sid，这里漏了）。同一修法下，「会话已经没
   * 了」不再被宽 catch 记成「目录不可读」而静默少收一半文件：那是两种要告诉用户
   * 完全不同的事，前者必须中断整批。
   */
  async function expandRemoteDir(remoteDir: string, sid: string): Promise<Array<{ remotePath: string; relPath: string; size: number }>> {
    const result: Array<{ remotePath: string; relPath: string; size: number }> = []
    const stack: Array<{ dir: string; rel: string }> = [{ dir: remoteDir, rel: '' }]
    // P3 修复：同 expandLocalDir — 收集不可读而跳过的子目录并统一告警
    const skipped: string[] = []
    const unsafe: string[] = []
    const symlinks: string[] = []
    while (stack.length > 0) {
      const { dir, rel } = stack.pop()!
      let entries: SftpEntry[]
      try {
        entries = await getApi().sftpList(sid, dir)
      } catch (err) {
        const code = extractSSHError(err)?.code
        if (code === 'SFTP_SESSION_REQUIRED' || code === 'SESSION_NOT_FOUND') {
          throw new Error('SFTP 会话已断开，目录展开中断')
        }
        skipped.push(dir)
        continue
      }
      for (const e of entries) {
        // S5-13：条目名由服务器书写，畸形名（含分隔符 / `..`）会经 relPath 拼进
        // 本地写路径并把落点挪出用户选的下载目录，这里逐项跳过而不是落地
        if (isUnsafeRemoteName(e.name)) {
          unsafe.push(`${dir}/${e.name}`)
          continue
        }
        // S5-20：符号链接不进展开结果。链接的 size 是目标路径串的长度（POSIX），
        // 当文件下载会把目标内容拉成本地普通文件、丢失链接；指向目录的链接也
        // 不会被误判成目录（isDirectory 已只认目录位），但语义同样不可逆。
        // 与本地侧 fs:copy 的 lstat 拒绝、zip 的 fail-closed 扫描同口径。
        if (e.isSymlink) {
          symlinks.push(`${dir}/${e.name}`)
          continue
        }
        const childRel = rel ? `${rel}/${e.name}` : e.name
        if (e.isDirectory) {
          stack.push({ dir: e.path, rel: childRel })
        } else {
          // 保留 SftpEntry.size（sftpList 已带）：大文件优先排序要用
          result.push({ remotePath: e.path, relPath: childRel, size: e.size || 0 })
        }
      }
    }
    if (skipped.length > 0) {
      toast.warning(`部分远程子目录因不可读被跳过（${skipped.length} 个）：${skipped.slice(0, 3).join('、')}${skipped.length > 3 ? ' …' : ''}`)
    }
    if (unsafe.length > 0) {
      toast.warning(`部分远程条目因文件名不安全被跳过（${unsafe.length} 个）：${unsafe.slice(0, 3).join('、')}${unsafe.length > 3 ? ' …' : ''}`)
    }
    if (symlinks.length > 0) {
      toast.warning(`部分远程符号链接被跳过（${symlinks.length} 个）：${symlinks.slice(0, 3).join('、')}${symlinks.length > 3 ? ' …' : ''}`)
    }
    return result
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
    await runWithActionToast({
      toast,
      action: () => getApi().fsZipLocal(localPaths, zipPath),
      pending: `正在压缩: ${zipName}…`,
      success: `已压缩: ${zipName}`,
      failure: (err) => toFriendlyMessage(err, '压缩失败'),
      afterSuccess: localRefresh,
      rethrow: true,
    })
  }

  /**
   * 本地解压：将当前本地目录下的 zip 文件解压到同名目录
   * @param localZipPath zip 文件本地路径
   * @param destDirName 解压目标目录名（会创建在当前本地目录下）
   */
  async function unzipLocal(localZipPath: string, destDirName: string): Promise<void> {
    const destDir = await getApi().fsPathJoin(localCwd.value, destDirName)
    await runWithActionToast({
      toast,
      action: () => getApi().fsUnzipLocal(localZipPath, destDir),
      pending: `正在解压: ${destDirName}…`,
      success: `已解压: ${destDirName}`,
      failure: (err) => toFriendlyMessage(err, '解压失败'),
      afterSuccess: localRefresh,
      rethrow: true,
    })
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
    await runWithActionToast({
      toast,
      action: () => getApi().sftpZipRemote(sshSessionId.value, sources, remoteZipPath, remoteCwd.value),
      pending: `正在远程压缩: ${zipName}…`,
      success: `已压缩: ${zipName}`,
      failure: (err) => toFriendlyMessage(err, '远程压缩失败'),
      afterSuccess: remoteRefresh,
      rethrow: true,
    })
  }

  /**
   * 远程解压：将远程 zip 文件解压到同名目录
   * @param remoteZipPath zip 文件远程路径
   * @param destDirName 解压目标目录名（会创建在当前远程目录下）
   */
  async function unzipRemote(remoteZipPath: string, destDirName: string): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const destDir = remoteCwd.value === '/' ? '/' + destDirName : remoteCwd.value + '/' + destDirName
    await runWithActionToast({
      toast,
      action: () => getApi().sftpUnzipRemote(sshSessionId.value, remoteZipPath, destDir),
      pending: `正在远程解压: ${destDirName}…`,
      success: `已解压: ${destDirName}`,
      failure: (err) => toFriendlyMessage(err, '远程解压失败'),
      afterSuccess: remoteRefresh,
      rethrow: true,
    })
  }

  // ─── 路径辅助（用于 ZIP 覆盖确认等场景） ────────
  /** 拼接远程子路径（基于当前 remoteCwd，S5-21 起可显式传基目录 —— 同侧复制目标不是 cwd） */
  function joinRemotePath(name: string, base: string = remoteCwd.value): string {
    if (!name) return base
    if (name.startsWith('/')) return name
    return base === '/' ? '/' + name : base + '/' + name
  }

  /** 拼接本地子路径（基于当前 localCwd，S5-21 起可显式传基目录 —— 同侧复制目标不是 cwd） */
  async function joinLocalPath(name: string, base: string = localCwd.value): Promise<string> {
    if (!name) return base
    if (/^[a-z]:[\\/]/i.test(name) || name.startsWith('/')) return name
    return await getApi().fsPathJoin(base, name)
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

  /** 取远程文件元信息（冲突对话框展示 大小/修改时间 用）。不存在/未连接/stat 失败一律返回 null。 */
  async function statRemote(path: string): Promise<{ size: number; modified: number; isDirectory: boolean } | null> {
    if (!sshSessionId.value || !online.value || !path) return null
    try {
      const st = await getApi().sftpStat(sshSessionId.value, path)
      return { size: st.size, modified: st.modified, isDirectory: st.isDirectory }
    } catch {
      return null
    }
  }

  /** 取本地文件元信息（同 statRemote，失败返回 null）。 */
  async function statLocal(path: string): Promise<{ size: number; modified: number; isDirectory: boolean } | null> {
    if (!path) return null
    try {
      const st = await getApi().fsStat(path)
      return { size: st.size, modified: st.modified, isDirectory: st.isDirectory }
    } catch {
      return null
    }
  }

  /**
   * 断开连接时由 store 调用：清空远程列表态并使在途列目录失效。
   * loadRemote 用 `mySeq !== remoteLoadSeq` 丢弃过期结果，但 disconnect 不推进
   * 计数器 → 断线前发出的 sftpList 断线后才返回时会通过守卫，把死主机的目录
   * 列表和 remoteCwd 重新填回（online 已 false，列不可操作，且脏 cwd 带入下次
   * 连接）。bump 一下让那次 mySeq 失配即可。
   */
  function clearRemoteForDisconnect(): void {
    remoteCwd.value = ''
    remoteEntries.value = []
    remoteLoadSeq++
    selectedRemote.value = new Set()
    remoteError.value = ''
  }

  return {
    // state
    localCwd, remoteCwd, localEntries, remoteEntries,
    loadingLocal, loadingRemote, localError, remoteError,
    selectedLocal, selectedRemote, lastSelectedLocal, lastSelectedRemote,
    localSort, remoteSort, localFilter, remoteFilter,
    // sort / filter
    sortEntries, filterEntries, setSort,
    // local
    loadLocal, initLocalHome, getDrives, localParent, localUp, localRefresh,
    localMkdir, localRename, localDelete,
    // remote
    loadRemote, remoteParent, remoteUp, remoteRefresh, remoteMkdir, remoteChmod,
    remoteRename, remoteDelete,
    clearRemoteForDisconnect,
    // selection
    toggleSelectLocal, toggleSelectRemote, selectRange, selectAll, clearSelection,
    // 目录展开（传输入队用）
    expandLocalDir, expandRemoteDir,
    // zip
    zipLocal, unzipLocal, zipRemote, unzipRemote,
    // 路径辅助（用于 ZIP 覆盖确认等场景）
    joinRemotePath, joinLocalPath, remotePathExists, localPathExists,
    // 冲突对话框取目标元信息（大小/修改时间对比）
    statRemote, statLocal,
  }
}
