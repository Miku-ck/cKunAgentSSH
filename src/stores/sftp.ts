/**
 * SFTP Store — 管理 SFTP 双栏文件传输状态
 * 左栏(本地)走现有 fs* IPC,右栏(远程)走 sftp:* IPC
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useToastStore } from './toast'
import { toFriendlyMessage, unwrapIpcErrorEnvelope } from '@/api/client'
import { extractSSHError } from '@/ssh-error-types'
import { isUnsafeRemoteName } from '@/utils/remote-name'
import { basenameLocal } from '@/utils/transfer-conflict'
import { describeTransferError } from '@/utils/transfer-error'
import { isActiveTransferStatus, type TransferKind, type TransferStatus } from '@/utils/transfer-format'

/** 并发上限的可调天花板。导出而不是让底栏再抄一份 8 —— 两边一旦分叉，
 *  「+」号会在 store 还在钳的时候先停住，或反过来按下去没反应。 */
export const MAX_CONCURRENT_CEIL = 8

export interface TransferItem {
  id: string
  direction: 'up' | 'down'
  /** 任务类别：同侧复制任务复用 up/down 的 direction 语义会造成暂停/恢复/重试错乱，
   *  必须显式记录真实类别（F-6 修复） */
  kind: TransferKind
  name: string
  from: string
  to: string
  transferred: number
  total: number
  // 状态联合类型来自 transfer-format（唯一来源）：判色与「是否不确定进度」的分支
  // 必须和格式化函数认同一份成员表，抄第二份迟早和 'paused' 分叉。
  status: TransferStatus
  error?: string
  startTime: number
  endTime?: number
  /** 目录传输的分组标识：一次目录展开产生的 N 条任务共享同一个 groupId，
   *  传输中心据此折叠成一行组头 + 组级汇总条。单文件/复制任务为 undefined（平铺显示）。
   *  只用于展示归并 —— 调度、取消、续传仍逐条按 id 走，不引入组级 IPC。 */
  groupId?: string
  /** 组头显示名（被展开的那个目录名，带尾斜杠） */
  groupLabel?: string
  /** 入队时已知的文件字节数（大文件优先排序用；未知为 undefined，视为 0 排末尾） */
  size?: number
}

// B20 修复：目录加载请求序号（模块级单调递增）。
// 快速连点 ".." 时慢的旧请求后返回会覆盖新结果，导致地址栏与列表错配。
// 参照 App.vue 背景图 bgSeq 模式：await 后序号不匹配即丢弃过期结果。
let localLoadSeq = 0
let remoteLoadSeq = 0
// 打开会话的代次。connect / connectFromTerminal 都要等一整套握手 + 列目录，期间
// 用户可以再发起一次（`online` 还是 false，图标与「连接远程」都照常可点），两条并发
// 完成时落败者会把全局身份抹成空串 —— 真正活着的那条会话因此再也关不掉（disconnect
// 见 sshSessionId 为空直接跳过 sftpClose/sshDisconnect），地址栏与列表还会分属两台主机。
let openSeq = 0

/** 带超时的 sshExec（新契约：第三参 timeoutSec，默认 30 上限 600）。
 *  本地类型断言 — env.d.ts 的 ElectronAPI.sshExec 签名由主进程代理同步更新 */
type SshExecWithTimeout = (id: string, command: string, timeoutSec?: number) =>
  Promise<{ stdout: string; stderr: string; exitCode: number | null }>
function sshExecWithTimeout(
  api: ElectronAPI,
  id: string,
  command: string,
  timeoutSec: number,
): Promise<{ stdout: string; stderr: string; exitCode: number | null }> {
  return (api as unknown as { sshExec: SshExecWithTimeout }).sshExec(id, command, timeoutSec)
}

export const useSftpStore = defineStore('sftp', () => {
  const toast = useToastStore()

  /** 弱网下并发任务几乎同时失败：自动续传通知按 1s 窗口聚合，避免 5–8 个
   *  并发任务 × 2 轮重试把 toast 刷屏。窗口内第一条进来先起定时器，窗口关闭
   *  时把积攒的任务合成一条（按去重后数量）；单任务仍报名字，多任务报数量。 */
  let autoRetryToastTimer: ReturnType<typeof setTimeout> | null = null
  let autoRetryToastNames: string[] = []
  function toastAutoRetryQueued(name: string): void {
    autoRetryToastNames.push(name)
    if (autoRetryToastTimer) return
    autoRetryToastTimer = setTimeout(() => {
      autoRetryToastTimer = null
      const unique = [...new Set(autoRetryToastNames)]
      autoRetryToastNames = []
      const subject = unique.length === 1 ? unique[0] : `${unique.length} 个传输任务`
      toast.info(`网络不稳，正在自动续传: ${subject}…`)
    }, 1000)
  }

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
  /** 标记当前 SFTP 是否复用终端 SSH 会话（复用时 disconnect 不关闭 SSH 连接） */
  const reusingTerminal = ref(false)

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
  /**
   * 阶段 2（B4）：id → TransferItem 的 O(1) 索引，消掉 applyProgress 每次进度事件
   * 的 O(N) 全表 `find`（活跃任务 × 100ms 节流 × 大目录 = 高频热路径）。
   *
   * 刻意非响应式（普通 Map）：它只是查找加速器，渲染仍读 `transfers` 数组，两个
   * 容器存的是**同一批对象引用**，写 `t.transferred` 照样触发视图更新。
   *
   * 不变量「Map 与数组同增同删」只在 addTransfer / removeTransfer / reindexTransfers
   * 三个收敛点维护，其余一律不得直接 `transfers.value.push/splice/= []`（已全量走
   * 助手，见下方 addTransfer 调用点与 removeTransfer / clearCompleted / disconnect）。
   * 之前 B4 被判定"暂缓"正是因为裸影子 Map 要在散落各处的手写增删上同步；把增删
   * 收成单一写入者后，索引不再有可漏的更新点。
   */
  const transferById = new Map<string, TransferItem>()

  /** 唯一的「新增传输」写入口：入数组 + 入索引，返回同一对象供后续原地改状态。 */
  function addTransfer(item: TransferItem): TransferItem {
    transfers.value.push(item)
    transferById.set(item.id, item)
    return item
  }

  /** 数组被整体重建（disconnect 清空 / clearCompleted 过滤）后重算索引。 */
  function reindexTransfers(): void {
    transferById.clear()
    for (const t of transfers.value) transferById.set(t.id, t)
  }

  // m4 修复：transferSeq 移入 store state，避免模块级变量永不重置和多会话共享
  const transferSeq = ref(1)
  // 批量传输并发上限：独立传输连接落地后（主进程为每个并发传输各开一条 SSH
  // 连接，各占 2 MB 通道窗口），并发传输不再瓜分同一窗口，默认值由 3 上调到 5
  // —— 5 条独立窗口在高 RTT 链路上能把总吞吐拉近 5 倍带宽延迟积。仍设上限
  // 8：再高会撞服务端 MaxSessions（通常 10）与本机连接数，且单机上行带宽
  // 往往撑不住更多并行流。做成响应式是为了让传输中心底栏能就地调节。
  const MAX_CONCURRENT_DEFAULT = 5
  const maxConcurrent = ref(MAX_CONCURRENT_DEFAULT)
  function setMaxConcurrent(n: number): void {
    if (!Number.isFinite(n)) return
    maxConcurrent.value = Math.min(MAX_CONCURRENT_CEIL, Math.max(1, Math.round(n)))
  }
  // 审查R2修复：并发门跨批次唤醒注册表 —— gateCount 按全局 running 计数，
  // 但原实现的 scheduleNext 只在本批次任务 finally 里重入：先提交的批次占满
  // 3 个并发门时，后提交的批次初始派发 0 个且永无唤醒 → 其 transfer 永久
  // pending、Promise 永不 resolve。每个 runBatch 在跑期间登记自己的
  // scheduleNext，任一任务完成时唤醒全部登记批次，批次结束时注销。
  const batchSchedulers = new Set<() => void>()
  function wakeBatchSchedulers(): void {
    // 快照迭代：被唤醒的 scheduleNext 可能同步派发并即时完成（非 pending 跳过），
    // 触发 finish() 注销自身，避免迭代中被 delete 影响
    for (const fn of [...batchSchedulers]) fn()
  }
  // 待删除传输记录定时器登记表：scheduleRemoveTransfer 登记、removeTransfer 注销、
  // disconnect 统一清空。避免断线重连后残留定时器按旧 id 误删新传输记录。
  const removeTimers = new Map<string, ReturnType<typeof setTimeout>>()

  // ─── 剪贴板（跨侧复制粘贴） ─────────────────────
  const clipboard = ref<{ side: 'local' | 'remote'; paths: string[] } | null>(null)
  // B26 修复：并发门计数已移入 runBatch 的 gateCount（仅统计 upload/download），
  // 原全局 runningCount（含 copy 类）不再需要

  // 「活跃」= 正在传 + 暂停待续，刻意与传输中心 active tab（transfer-groups.inTab）同一口径：
  // 两处各自定义迟早分叉（徽标数与 active 列表数对不上就是前车）。此前这里多算了 pending，
  // 让排队中的任务混进「进行中」读数 —— pending 属于「排队」tab（queued），不计入活跃。
  // paused 仍算活跃：它们未结束、需要在面板保留「继续传输」入口、且行内 ETA 依赖秒级 tick。
  // 消费方（SftpView.syncTick / queueActiveCount）据此停表与计数：只剩 pending 时秒表可停，
  // 因为 pending 行 transferred=0、ETA 本就显示「—」，不需要走秒。
  const activeTransfers = computed(() =>
    transfers.value.filter(t => isActiveTransferStatus(t.status))
  )

  /** 队列计数 + 总进度 —— 供各 tab 的计数徽标与会话栏实时徽标消费。
   *
   *  刻意**不含**速度/剩余时间：那两个量是时间的函数，而 computed 拿不到会自己跳的
   *  `now`，收进来就会被缓存成一个死值。
   *
   *  百分比按**字节**算，不按任务数：一个 12 文件的目录里 11 个小文件传完、第 12 个
   *  800 MB 的还没动，按数计是 92%、按字节只有 8% —— 后者才是用户的体感。
   *  `total === 0`（未知总量）的条目不进分母，否则一条远端复制会把整条队列钉死在
   *  一个偏低的百分比上。
   *
   *  只暴露有人读的字段：已传/总字节与 indeterminate 都是算百分比的中间量，不对外
   *  （无消费方=失效设置项）。 */
  const queueStats = computed(() => {
    const s = {
      running: 0,
      pending: 0,
      paused: 0,
      done: 0,
      error: 0,
      cancelled: 0,
      bytesTransferred: 0,
      bytesTotal: 0,
    }
    for (const t of transfers.value) {
      s[t.status]++
      s.bytesTransferred += t.transferred
      if (t.total > 0) s.bytesTotal += t.total
    }
    return {
      running: s.running,
      pending: s.pending,
      paused: s.paused,
      done: s.done,
      error: s.error,
      cancelled: s.cancelled,
      percent: s.bytesTotal > 0
        ? Math.min(100, Math.round((s.bytesTransferred / s.bytesTotal) * 100))
        : 0,
    }
  })

  // ─── 绑定 / 解绑 SSH 会话 ────────────────────
  /** 直接通过 SSH 连接 + SFTP 子系统打开远程会话(独立于终端会话) */
  async function connect(connection: SSHConnectionRecord): Promise<void> {
    if (online.value) await disconnect()
    const myOpen = ++openSeq
    reusingTerminal.value = false
    const plain = JSON.parse(JSON.stringify(connection))
    const { id, name } = await getApi().sshConnectSftp(plain)
    if (myOpen !== openSeq) {
      // 握手期间后起的打开已接管全局身份：这条新建的连接没人引用，就地拆掉并静默
      // 退出。写全局状态会把新会话的身份改写成旧主机，抛错还会多弹一次失败。
      try { await getApi().sshDisconnect(id) } catch { }
      return
    }
    sshSessionId.value = id
    connectionName.value = name || `${connection.username}@${connection.host}`
    connectionHost.value = `${connection.username}@${connection.host}:${connection.port}`
    try {
      await getApi().sftpOpen(id)
      if (myOpen !== openSeq) {
        try { await getApi().sftpClose(id) } catch { }
        try { await getApi().sshDisconnect(id) } catch { }
        return
      }
      online.value = true
      remoteCwd.value = '/'
      await loadRemote('/')
    } catch (err: any) {
      if (myOpen !== openSeq) {
        try { await getApi().sshDisconnect(id) } catch { }
        return
      }
      online.value = false
      remoteError.value = unwrapIpcErrorEnvelope(err?.message || '') || 'SFTP 打开失败'
      // 失败时关闭 SSH
      try { await getApi().sshDisconnect(id) } catch { }
      // 只在身份仍是本次打开设进去的那条时清空：否则抹掉的是后起会话的身份
      if (sshSessionId.value === id) sshSessionId.value = ''
      throw err
    }
  }

  /**
   * 复用已有终端 SSH 会话打开 SFTP 子系统，并跳转到终端当前工作目录
   * 不新建 SSH 连接，disconnect 时只关闭 SFTP 通道，不断开 SSH
   */
  async function connectFromTerminal(
    terminalSshId: string,
    name: string,
    host: string,
  ): Promise<void> {
    if (online.value) await disconnect()
    const myOpen = ++openSeq
    reusingTerminal.value = true
    sshSessionId.value = terminalSshId
    connectionName.value = name
    connectionHost.value = host
    try {
      await getApi().sftpOpen(terminalSshId)
      if (myOpen !== openSeq) {
        // 后起那次已接管全局状态。只有当前身份不是本终端时才回收本终端的 SFTP
        // 子系统 —— 两次点开同一台时，关掉会把接管者一起打死。
        if (sshSessionId.value !== terminalSshId) {
          try { await getApi().sftpClose(terminalSshId) } catch { }
        }
        return
      }
      online.value = true
      // 获取终端当前工作目录：优先用 OSC 7 跟踪值，为空则 exec pwd
      let cwd = await getApi().sshGetCwd(terminalSshId)
      if (!cwd) {
        const result = await getApi().sshExec(terminalSshId, 'pwd')
        cwd = (result.stdout || '').trim()
      }
      if (myOpen !== openSeq) return
      const target = cwd || '/'
      remoteCwd.value = target
      await loadRemote(target)
    } catch (err: any) {
      if (myOpen !== openSeq) {
        // 旧代的失败不改全局在线态、也不弹给用户；自己那条子系统若没被接管就顺手回收
        if (sshSessionId.value !== terminalSshId) {
          try { await getApi().sftpClose(terminalSshId) } catch { }
        }
        return
      }
      online.value = false
      remoteError.value = unwrapIpcErrorEnvelope(err?.message || '') || 'SFTP 打开失败'
      // 复用模式下不关闭 SSH 连接（终端仍需要）
      try { await getApi().sftpClose(terminalSshId) } catch { }
      if (sshSessionId.value === terminalSshId) sshSessionId.value = ''
      reusingTerminal.value = false
      throw err
    }
  }

  async function disconnect(): Promise<void> {
    if (sshSessionId.value) {
      try { await getApi().sftpClose(sshSessionId.value) } catch { }
      // 复用终端模式不关闭 SSH 连接（终端仍需要），仅关闭 SFTP 子系统
      if (!reusingTerminal.value) {
        try { await getApi().sshDisconnect(sshSessionId.value) } catch { }
      }
    }
    sshSessionId.value = ''
    connectionName.value = ''
    connectionHost.value = ''
    online.value = false
    reusingTerminal.value = false
    remoteCwd.value = ''
    remoteEntries.value = []
    // 使在途远程列目录失效：loadRemote 用 `mySeq !== remoteLoadSeq` 丢弃过期结果，
    // 但 disconnect 不推进计数器 → 断线前发出的 sftpList 断线后才返回时会通过守卫，
    // 把死主机的目录列表和 remoteCwd 重新填回（online 已 false，列不可操作，且脏
    // cwd 带入下次连接）。bump 一下让那次 mySeq 失配即可。
    remoteLoadSeq++
    // 同理推进打开代次：断线时若在途的 connect 还在握手，那一次回来后不该把
    // online/sshSessionId 重新点亮成一台已经不存在的主机
    openSeq++
    selectedRemote.value = new Set()
    // 跨主机的远程剪贴板必须一起清：不清则换主机后「粘贴」会把 A 机的绝对路径
    // cp -r 到 B 机上（canPaste 只比 side，比不出主机）
    clipboard.value = null
    // m4 修复的补充：transferSeq 在 store 生命周期内单调递增，不再随 disconnect 重置，
    // 保证断线重连后新传输记录 id 不会与旧记录重复
    remoteError.value = ''
    // 清空所有待删除定时器：断线后旧定时器若不清除，重连后可能误删新传输记录
    for (const timer of removeTimers.values()) clearTimeout(timer)
    removeTimers.clear()
    // B10 修复：清空前先对在途（running）任务显式取消并置 cancelled，
    // 避免每个传输 Promise 的 reject 走通用错误路径连弹"上传/下载失败"。
    // 先置状态再发 sftpCancel：CANCELLED 拒绝到达 catch 时看到 cancelled 即静默 return。
    for (const t of transfers.value) {
      if (t.status === 'running') {
        t.status = 'cancelled'
        t.endTime = Date.now()
        // 审查修复（P2）：sftpCancel 返回 Promise — 原实现同步 try/catch
        // 只能捕获调用时的同步抛错，IPC reject 变成 unhandled rejection。
        // 改为对返回的 Promise 挂 .catch
        void getApi().sftpCancel(t.id).catch(() => { /* 忽略取消失败 */ })
      }
    }
    // 断开连接时清空传输记录
    transfers.value = []
    reindexTransfers()
  }

  /** 清空已完成的传输记录 */
  function clearCompleted(): void {
    transfers.value = transfers.value.filter(t => t.status === 'running' || t.status === 'paused' || t.status === 'pending')
    reindexTransfers()
  }

  // ─── 剪贴板操作 ───────────────────────────────
  function copyToClipboard(side: 'local' | 'remote', paths: string[]): void {
    clipboard.value = { side, paths: [...paths] }
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

  // ─── 同侧复制粘贴 ─────────────────────────────
  /**
   * 本地同侧复制：将 paths 复制到 targetDir
   * 支持目录递归，进度通过 sftp:progress 事件上报
   */
  async function localCopy(paths: string[], targetDir: string): Promise<void> {
    const tasks: Array<{ id: string; src: string; dest: string; name: string }> = []
    for (const src of paths) {
      const name = basenameLocal(src)
      const dest = await getApi().fsPathJoin(targetDir, name)
      const id = `t${transferSeq.value++}`
      addTransfer({
        id, direction: 'up', kind: 'copy-local', name,
        from: src, to: dest,
        transferred: 0, total: 0,
        status: 'pending', startTime: Date.now(),
      })
      tasks.push({ id, src, dest, name })
    }
    // F-6 修复：带上 id/kind — copy 类不受上传/下载并发门限制，
    // 且 runBatch 派发前按 id 检查 status 防竞态
    runBatch(tasks.map(t => ({ fn: () => runLocalCopy(t.id, t.src, t.dest), id: t.id, kind: 'copy-local' as const }))).then(async (results) => {
      if (results.some(r => r.ok)) await localRefresh()
    }).catch(() => {
      // C2 修复：防止 post-batch refresh 抛错导致未捕获 Promise rejection

    })
  }

  async function runLocalCopy(id: string, src: string, dest: string): Promise<void> {
    const t = transferById.get(id)
    if (!t) return
    // F-5 竞态双保险：任务在排队期间被取消则不再启动
    if (t.status === 'cancelled') return
    t.status = 'running'
    try {
      await getApi().fsCopy(src, dest, id)
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`复制成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      // S5-11：取消走 cancelTransfer（先置 cancelled 再发 sftpCancel），断开连接走
      // disconnect 同法，两者都会让 fs:copy 抛「复制已取消」。与 runUpload/runDownload
      // 的 B10 口径一致：看到 cancelled 即静默返回，别再弹一次「复制失败」盖掉「已取消」。
      // 记录已被删掉（deleteTransfer 对进行中的复制）同样静默 —— 用户要的就是它停。
      // （宽化为 string：TS 控制流分析看不到 cancelTransfer 跨函数的赋值）
      const statusNow: string = t.status
      if (statusNow === 'cancelled' || !transferById.has(id)) return
      t.status = 'error'
      // 兜底文案不能也叫「复制失败」：外层 toast 已经以「复制失败: 名字 —」开头，
      // 撞上这个兜底就原样说两遍。
      // S0-24：先剥 Electron 的 invoke 信封再落给用户 —— 否则内部通道名
      // （'fs:copy' 之类）连同 "Error invoking remote method" 一起进用户可见区
      t.error = unwrapIpcErrorEnvelope(err?.message || '') || '未返回错误详情'
      t.endTime = Date.now()
      toast.error(`复制失败: ${t.name} — ${t.error}`)
      scheduleRemoveTransfer(id, 10000)
    }
  }

  /**
   * 远程同侧复制：用 cp -r 复制，不支持细粒度进度（total=0 不定进度）
   */
  async function remoteCopy(paths: string[], targetDir: string): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const tasks: Array<{ id: string; src: string; dest: string; name: string }> = []
    for (const src of paths) {
      const name = basenameRemote(src)
      const dest = targetDir === '/' ? '/' + name : targetDir + '/' + name
      const id = `t${transferSeq.value++}`
      addTransfer({
        id, direction: 'down', kind: 'copy-remote', name,
        from: src, to: dest,
        transferred: 0, total: 0,
        status: 'pending', startTime: Date.now(),
      })
      tasks.push({ id, src, dest, name })
    }
    // F-6 修复：带上 id/kind（同 localCopy）
    runBatch(tasks.map(t => ({ fn: () => runRemoteCopy(t.id, t.src, t.dest), id: t.id, kind: 'copy-remote' as const }))).then(async (results) => {
      if (results.some(r => r.ok)) await remoteRefresh()
    }).catch(() => {
      // C2 修复：防止 post-batch refresh 抛错导致未捕获 Promise rejection

    })
  }

  async function runRemoteCopy(id: string, src: string, dest: string): Promise<void> {
    const t = transferById.get(id)
    if (!t) return
    // F-5 竞态双保险：任务在排队期间被取消则不再启动
    if (t.status === 'cancelled') return
    t.status = 'running'
    try {
      // 用 cp -r 递归复制，shellQuote 转义路径中的特殊字符
      const escapedSrc = `'${src.replace(/'/g, "'\\''")}'`
      const escapedDest = `'${dest.replace(/'/g, "'\\''")}'`
      // B7 修复：cp -r 大目录必超 sshExec 默认 30s 超时 — 显式传 300s
      const result = await sshExecWithTimeout(getApi(), sshSessionId.value, `cp -r ${escapedSrc} ${escapedDest}`, 300)
      // 未知退出码（通道未收到 exit-status，main 侧 exitCode 保持 null）不能算成功：
      // 那正是进程被杀/通道异常中断的形态，此时 cp -r 可能只拷了一半。
      // 与 main 进程其余 exec 判定（exitCode !== 0）保持一致。
      if (result.exitCode !== 0) {
        throw new Error(result.stderr || '远端未返回原因')
      }
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`复制成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      // 与 runLocalCopy 的 S5-11 口径一致（原注释误指向 runRemoteCopy 自己）：
      // 取消或断开让传输通道抛出时静默返回，别再用「复制失败」盖掉「已取消」，
      // 也不给已被摘掉的记录改状态
      const statusNow: string = t.status
      if (statusNow === 'cancelled' || !transferById.has(id)) return
      t.status = 'error'
      // 兜底文案不能也叫「复制失败」：外层 toast 已经以「复制失败: 名字 —」开头，
      // 撞上这个兜底就原样说两遍。
      // S0-24：先剥 Electron 的 invoke 信封再落给用户 —— 否则内部通道名
      // （'fs:copy' 之类）连同 "Error invoking remote method" 一起进用户可见区
      t.error = unwrapIpcErrorEnvelope(err?.message || '') || '未返回错误详情'
      t.endTime = Date.now()
      toast.error(`复制失败: ${t.name} — ${t.error}`)
      scheduleRemoveTransfer(id, 10000)
    }
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
    return result
  }

  /**
   * 批量上传：将所有任务加入队列，按并发上限调度执行
   * 支持目录递归：目录会被展开为多个文件上传任务，远程目录结构自动创建
   * 所有任务完成后再统一刷新目录，避免 N 个文件触发 N 次刷新
   */
  /**
   * 上传多个本地文件到远程目录（支持目录递归）
   * @param renameMap 冲突「重命名」落地下发的 原路径 → 新顶层名 映射；
   *        缺省即保持原名。目录上传改名只改顶层目录名，其下 relPath 原样保留。
   */
  async function uploadFiles(localPaths: string[], targetDir?: string, renameMap?: Record<string, string>): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    // 整批任务绑定「入队时」的会话：目录展开/建父目录要 await 多次 IPC，
    // 期间用户切换活动终端会改写 sshSessionId.value —— 继续按 ref 取值会把
    // 剩余文件上传到另一台主机的同一路径下
    const sid = sshSessionId.value
    const remoteBaseDir = targetDir ?? remoteCwd.value
    const tasks: Array<{ id: string; localPath: string; remotePath: string; size: number }> = []
    // 目录上传涉及的全部远程父目录（去重后并发创建，见循环后）
    const remoteDirs = new Set<string>()

    for (const lp of localPaths) {
      const name = renameMap?.[lp] ?? basenameLocal(lp)
      const remoteBase = remoteBaseDir === '/' ? '/' + name : remoteBaseDir + '/' + name

      // 判断是文件还是目录：优先查 localEntries（应用内拖拽），否则用 fsStat（OS 拖拽）
      let isDir = false
      // 入队时一并抓文件大小（大文件优先排序用；抓不到按 0 处理排末尾）
      let fileSize = 0
      const localEntry = localEntries.value.find(e => e.path === lp)
      if (localEntry) {
        isDir = localEntry.isDirectory
        fileSize = localEntry.size || 0
      } else {
        try {
          const st = await getApi().fsStat(lp)
          isDir = st.isDirectory
          fileSize = st.size || 0
        } catch {
          // 无法获取信息，当作文件处理
        }
      }

      if (isDir) {
        // 目录：递归展开。整批盖同一个 groupId，传输中心据此折叠成一行组头；
        // 调度与取消仍逐条按 id 走。
        const groupId = `g${transferSeq.value++}`
        const groupLabel = `${name}/`
        const files = await expandLocalDir(lp)
        for (const f of files) {
          const remotePath = remoteBase + '/' + f.relPath
          const remoteDir = remotePath.substring(0, remotePath.lastIndexOf('/'))
          // 父目录先记录，循环后去重并发创建 —— 此前在这里逐文件 await
          // sftpMkdirRecursive（每次还要逐段 stat），1000 文件 × 3 层深 =
          // 3000 次串行往返，任务还没开始传就先排几分钟队
          remoteDirs.add(remoteDir)
          const id = `t${transferSeq.value++}`
          addTransfer({
            id, direction: 'up', kind: 'upload', name: f.relPath,
            from: f.localPath, to: remotePath,
            transferred: 0, total: 0,
            status: 'pending', startTime: Date.now(),
            groupId, groupLabel,
            size: f.size,
          })
          tasks.push({ id, localPath: f.localPath, remotePath, size: f.size })
        }
      } else {
        // 文件：直接加入
        const id = `t${transferSeq.value++}`
        addTransfer({
          id, direction: 'up', kind: 'upload', name,
          from: lp, to: remoteBase,
          transferred: 0, total: 0,
          status: 'pending', startTime: Date.now(),
          size: fileSize,
        })
        tasks.push({ id, localPath: lp, remotePath: remoteBase, size: fileSize })
      }
    }

    // 去重后的父目录并发创建（sftpMkdirRecursive 幂等：mkdir 失败后 re-stat
    // 判定已存在，并发建同一父目录安全）。失败静默 —— 建不了的目录留给传输
    // 时暴露真实错误，不在入队期误伤整批。
    if (remoteDirs.size > 0) {
      await runPool([...remoteDirs], 8, d => getApi().sftpMkdirRecursive(sid, d).catch(() => { /* ignore */ }))
    }

    // 大文件优先：按 size 降序 —— 并发门先放大文件（各占一条独立连接的
    // 2 MB 窗口），小文件填缝在后，减少"大文件排在一堆小文件后面干等"的
    // 长尾延迟（稳定排序：同大小维持原顺序，目录内相对次序不乱）
    tasks.sort((a, b) => b.size - a.size)

    // F-6 修复：带上 id/kind（runBatch 派发前检查 + 并发门计数）
    runBatch(tasks.map(t => ({ fn: () => runUpload(t.id, sid, t.localPath, t.remotePath), id: t.id, kind: 'upload' as const }))).then(async (results) => {
      // 至少有一个成功才刷新目录，避免无谓请求
      if (results.some(r => r.ok)) await remoteRefresh()
    }).catch((err) => {
      // C2 修复：防止 post-batch refresh 抛错导致未捕获 Promise rejection
      console.warn('[sftp] 上传批次收尾刷新失败:', err)
    })
  }

  /**
   * 下载多个远程文件到本地目录（支持目录递归）
   * @param renameMap 冲突「重命名」落地下发的 原远程路径 → 新顶层名 映射；缺省保持原名。
   */
  async function downloadFiles(remotePaths: string[], targetDir?: string, renameMap?: Record<string, string>): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    // 同 uploadFiles：入队时的会话固化到本批任务，避免等待期间切换终端后
    // 从另一台主机读取同名路径
    const sid = sshSessionId.value
    const localBaseDir = targetDir ?? localCwd.value
    const tasks: Array<{ id: string; remotePath: string; localPath: string; size: number }> = []
    // 目录下载涉及的全部本地父目录（去重后并发创建，见循环后）
    const localDirs = new Set<string>()

    for (const rp of remotePaths) {
      const name = renameMap?.[rp] ?? basenameRemote(rp)
      // S5-13：末段名也是服务器书写的内容，与 expandRemoteDir 里同一判据
      if (isUnsafeRemoteName(name)) {
        toast.error(`远程文件名不安全，已跳过：${rp}`)
        continue
      }
      const localBase = await getApi().fsPathJoin(localBaseDir, name)

      // 判断是文件还是目录：优先查 remoteEntries 缓存；
      // B8 修复：路径不在当前列表（如粘贴/拖拽其他目录条目）时，
      // 用 sftpStat 探测兜底 —— 一次往返拿类型。此前用 sftpList 全量列目录，
      // 万级文件的目录要拉全量列表只为扔掉
      let isDir = false
      const remoteEntry = remoteEntries.value.find(e => e.path === rp)
      if (remoteEntry) {
        isDir = remoteEntry.isDirectory
      } else {
        try {
          const st = await getApi().sftpStat(sid, rp)
          isDir = st.isDirectory
        } catch {
          isDir = false
        }
      }

      if (isDir) {
        // 目录：递归展开（sid 钉死传入，S5-12）
        let files: Array<{ remotePath: string; relPath: string; size: number }>
        try {
          files = await expandRemoteDir(rp, sid)
        } catch (err) {
          // 会话断开这类致命原因：整批不派发，不给用户一份悄悄少了一半的清单
          toast.error((err as Error)?.message || '目录展开失败')
          return
        }
        // 同 uploadFiles：一次目录展开的 N 条任务共享 groupId，展示层折叠成组头
        const groupId = `g${transferSeq.value++}`
        const groupLabel = `${name}/`
        for (const f of files) {
          const localPath = await getApi().fsPathJoin(localBase, f.relPath)
          // 确保本地父目录存在（fsMkdir 是 recursive）：先记录，循环后去重并发创建
          const localDir = localPath.includes('\\')
            ? localPath.split('\\').slice(0, -1).join('\\')
            : localPath.split('/').slice(0, -1).join('/')
          localDirs.add(localDir)
          const id = `t${transferSeq.value++}`
          addTransfer({
            id, direction: 'down', kind: 'download', name: f.relPath,
            from: f.remotePath, to: localPath,
            transferred: 0, total: 0,
            status: 'pending', startTime: Date.now(),
            groupId, groupLabel,
            size: f.size,
          })
          tasks.push({ id, remotePath: f.remotePath, localPath, size: f.size })
        }
      } else {
        // 文件：直接加入（大小从 remoteEntries 缓存取；不在缓存里时 stat 一次
        // 拿真实大小参与大文件优先排序，取不到按 0 排末尾）
        const id = `t${transferSeq.value++}`
        const remoteEntry = remoteEntries.value.find(e => e.path === rp)
        let fileSize = remoteEntry?.size || 0
        if (!remoteEntry) {
          try { fileSize = (await getApi().sftpStat(sid, rp)).size || 0 } catch { /* 保持 0 */ }
        }
        addTransfer({
          id, direction: 'down', kind: 'download', name,
          from: rp, to: localBase,
          transferred: 0, total: 0,
          status: 'pending', startTime: Date.now(),
          size: fileSize,
        })
        tasks.push({ id, remotePath: rp, localPath: localBase, size: fileSize })
      }
    }

    // 去重后的本地父目录并发创建（同上传侧 remoteDirs 的去重并行化）
    if (localDirs.size > 0) {
      await runPool([...localDirs], 8, d => getApi().fsMkdir(d).catch(() => { /* ignore */ }))
    }

    // 大文件优先：同 uploadFiles —— 并发门先放大文件（各占一条独立连接的
    // 2 MB 窗口），小文件填缝在后，减少长尾延迟（稳定排序，同大小维持原序）
    tasks.sort((a, b) => b.size - a.size)

    // F-6 修复：带上 id/kind（runBatch 派发前检查 + 并发门计数）
    runBatch(tasks.map(t => ({ fn: () => runDownload(t.id, sid, t.remotePath, t.localPath), id: t.id, kind: 'download' as const }))).then(async (results) => {
      if (results.some(r => r.ok)) await localRefresh()
    }).catch((err) => {
      // C2 修复：防止 post-batch refresh 抛错导致未捕获 Promise rejection
      console.warn('[sftp] 下载批次收尾刷新失败:', err)
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
   * 并发调度器：限制同时运行的传输任务数为 maxConcurrent（底栏可调，钳到 MAX_CONCURRENT_CEIL）
   * 返回每个任务的执行结果（ok/错误），全部完成才 resolve
   *
   * B26 修复：并发门只统计 upload/download（SFTP 通道争抢型任务），
   * copy 类（本地 fsCopy / 远程 cp -r 单命令）不计入、直接派发。
   * F-5 修复：派发前按 id 检查对应 transfer 的 status 是否仍为 'pending'，
   * 排队期间被取消的任务直接跳过（防竞态双保险）。
   */
  interface BatchTask<T> {
    fn: () => Promise<T>
    /** 对应 transfer 记录 id（用于派发前状态检查） */
    id?: string
    /** 任务类别：copy 类不受并发门限制 */
    kind?: TransferKind
  }

  function runBatch<T>(
    taskDefs: Array<BatchTask<T>>,
  ): Promise<Array<{ ok: boolean; result?: T; error?: any }>> {
    return new Promise((resolve) => {
      const total = taskDefs.length
      // 空任务直接 resolve，避免 Promise 永久挂起
      if (total === 0) {
        resolve([])
        return
      }
      const results: Array<{ ok: boolean; result?: T; error?: any }> = []
      let nextIdx = 0
      let finishedCount = 0

      // B26：仅 upload/download 计入并发门。
      // PERF-003：原实现每次派发判定都对全量 transfers 做 filter（万级批次
      // 累计 O(n²) 次元素访问，传输期间渲染进程掉秒级帧），现在两处收口：
      // ① limit 早退——饱和态（批次常态）下扫到第 limit 个 running 即返回，
      //    每轮唤醒均摊 O(limit) 而非 O(n)；
      // ② 门计数按唤醒缓存 + 派发本地自增——while 循环同步执行、fn() 的
      //    同步前缀（runUpload/runDownload 在首个 await 前置 running）必然
      //    生效，故单轮内本地自增与全局真值恒一致；跨轮的外部增删（用户
      //    手动传/删任务）在下一轮唤醒时重新读真值兜底。
      function gateCount(limit?: number): number {
        let count = 0
        for (const t of transfers.value) {
          if (t.status === 'running' && (t.kind === 'upload' || t.kind === 'download')) {
            count++
            if (limit !== undefined && count >= limit) return count
          }
        }
        return count
      }

      function scheduleNext(): void {
        // 本轮唤醒的并发门缓存（NaN = 尚未读全局真值）
        let gate = Number.NaN
        while (nextIdx < total) {
          const def = taskDefs[nextIdx]
          const unlimited = def.kind === 'copy-local' || def.kind === 'copy-remote'
          if (!unlimited) {
            if (Number.isNaN(gate)) gate = gateCount(maxConcurrent.value)
            if (gate >= maxConcurrent.value) break
            gate++
          }
          const idx = nextIdx++
          // F-5 竞态双保险：任务已不再是 pending（含排队期间被删除移除）则跳过。
          // 记录不存在（!t）同样跳过 —— 否则 deleteTransfer 删掉的 pending 任务
          // 仍会派发 fn()，runUpload/runDownload 找不到记录后空转
          if (def.id) {
            const t = transferById.get(def.id)
            if (!t || t.status !== 'pending') {
              results[idx] = { ok: false, error: new Error('task no longer pending') }
              finishedCount++
              if (finishedCount >= total) finish()
              continue
            }
          }
          def.fn()
            .then((result) => { results[idx] = { ok: true, result } })
            .catch((error) => { results[idx] = { ok: false, error } })
            .finally(() => {
              finishedCount++
              // 审查R2修复：任务完成既推进本批次，也唤醒所有等待中的批次
              // （含本批次）——原实现只重入本批次 scheduleNext，后提交批次饿死
              if (finishedCount >= total) finish()
              else wakeBatchSchedulers()
            })
        }
      }
      // 审查R2修复：登记本批次调度器供其他批次唤醒；resolve 时注销。
      batchSchedulers.add(scheduleNext)
      function finish(): void {
        batchSchedulers.delete(scheduleNext)
        resolve(results)
      }
      scheduleNext()
    })
  }

  async function runUpload(id: string, sid: string, localPath: string, remotePath: string, attempt = 1): Promise<void> {
    const t = transferById.get(id)
    if (!t) return
    // F-5 竞态双保险：任务在排队期间被取消则不再启动
    if (t.status === 'cancelled') return
    t.status = 'running'
    try {
      // 审查 M-18 修复：IPC 契约只有 4 个参数（preload/env.d.ts/main.ts 三处一致）—
      // 原调用多传 resumeOffset + AbortSignal：preload 只透传前 4 参，两者被静默丢弃
      // （AbortSignal 也无法结构化克隆过 IPC）。续传由主进程扫描远端 .part 文件
      // 自动定位断点，暂停/取消走 sftp:cancel（pauseTransfer 已调用）。
      await getApi().sftpUpload(id, sid, localPath, remotePath)
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`上传成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      // S0-15：code 两条腿都读（Error.name 前缀是跨 IPC 保真性最强的通道），
      // retryable 改由 code 现推 —— 不再依赖那个可能已被结构化克隆丢掉的布尔属性
      const { code, retryable, message } = describeTransferError(err)
      // 审查 M-18：暂停路径 — pauseTransfer 已把状态置为 'paused' 并记录断点，
      // 随后 sftp:cancel 触发的 CANCELLED 拒绝不能覆盖暂停状态、不能弹错误、
      // 更不能把暂停中的任务 10s 后移除（原实现会，导致用户无法"恢复"）
      // （宽化为 string：TS 控制流分析看不到 pauseTransfer 跨函数的赋值）
      const statusNow: string = t.status
      if (statusNow === 'paused') return
      // B10：disconnect 已先行置 cancelled 并 sftpCancel — CANCELLED 拒绝静默吞掉
      if (statusNow === 'cancelled') return
      // NETWORK/STALLED 错误自动续传至多 AUTO_RETRY_MAX 次、按 1s/2s 指数退避
      // （断点续传，主进程按 .part 自动定位）。判定只看 retryable（由 code 经
      // isTransferRetryable 现推，与主进程保留/删除 .part 的判据同源）
      if (retryable && attempt <= AUTO_RETRY_MAX) {
        const delay = autoRetryDelayMs(attempt)
        // 通知走 1s 聚合窗口（弱网下成批任务同时失败，逐条弹会刷屏）
        toastAutoRetryQueued(t.name)
        // 退避期间保持 running 占住并发门（1–2s 间隙不值得重新排队）；
        // 若用户此刻暂停/取消/删除，醒来后按状态放弃重试
        await sleep(delay)
        if (transferById.get(id)?.status !== 'running') return
        return runUpload(id, sid, localPath, remotePath, attempt + 1)
      }
      t.status = code === 'CANCELLED' ? 'cancelled' : 'error'
      t.error = message || '上传失败'
      t.endTime = Date.now()
      // 取消是用户行为，不弹错误 toast
      if (code !== 'CANCELLED') {
        // F-3：重试耗尽后区分停滞 — 主进程心跳停滞且自动续传失败
        const detail = code === 'STALLED' ? '传输停滞且自动续传失败，请手动重试' : t.error
        toast.error(`上传失败: ${t.name} — ${detail}`)
      }
      // Bug 6 修复：错误/取消任务也自动移除（延长到 10s 让用户看到错误信息）
      scheduleRemoveTransfer(id, 10000)
    }
  }

  async function runDownload(id: string, sid: string, remotePath: string, localPath: string, attempt = 1): Promise<void> {
    const t = transferById.get(id)
    if (!t) return
    // F-5 竞态双保险：任务在排队期间被取消则不再启动
    if (t.status === 'cancelled') return
    t.status = 'running'
    try {
      // 审查 M-18：同 runUpload — 严格按 4 参 IPC 契约调用（续传主进程按本地
      // .part 自动定位，取消走 sftp:cancel）
      await getApi().sftpDownload(id, sid, remotePath, localPath)
      t.status = 'done'
      t.endTime = Date.now()
      toast.success(`下载成功: ${t.name}`)
      scheduleRemoveTransfer(id)
    } catch (err: any) {
      // S0-15：code 两条腿都读（Error.name 前缀是跨 IPC 保真性最强的通道），
      // retryable 改由 code 现推 —— 不再依赖那个可能已被结构化克隆丢掉的布尔属性
      const { code, retryable, message } = describeTransferError(err)
      // 审查 M-18：暂停路径保护（同 runUpload）— 已暂停的任务不被 CANCELLED
      // 覆盖状态、不弹错误、不移除（宽化为 string：TS 看不到跨函数赋值）
      const statusNow: string = t.status
      if (statusNow === 'paused') return
      // B10：disconnect 已先行置 cancelled 并 sftpCancel — CANCELLED 拒绝静默吞掉
      if (statusNow === 'cancelled') return
      // NETWORK/STALLED 错误自动续传至多 AUTO_RETRY_MAX 次、按 1s/2s 指数退避
      // （断点续传，主进程按 .part 自动定位）。
      // 判定只看 retryable（由 code 现推，见 describeTransferError）
      if (retryable && attempt <= AUTO_RETRY_MAX) {
        const delay = autoRetryDelayMs(attempt)
        // 通知走 1s 聚合窗口（同 runUpload）
        toastAutoRetryQueued(t.name)
        // 退避期间保持 running 占住并发门；被暂停/取消/删除则醒来后放弃
        await sleep(delay)
        if (transferById.get(id)?.status !== 'running') return
        return runDownload(id, sid, remotePath, localPath, attempt + 1)
      }
      t.status = code === 'CANCELLED' ? 'cancelled' : 'error'
      t.error = message || '下载失败'
      t.endTime = Date.now()
      if (code !== 'CANCELLED') {
        // F-3：重试耗尽后区分停滞
        const detail = code === 'STALLED' ? '传输停滞且自动续传失败，请手动重试' : t.error
        toast.error(`下载失败: ${t.name} — ${detail}`)
      }
      // Bug 6 修复：错误/取消任务也自动移除（延长到 10s 让用户看到错误信息）
      scheduleRemoveTransfer(id, 10000)
    }
  }

async function cancelTransfer(id: string): Promise<void> {
  const t = transferById.get(id)
  // F-5 修复：pending（排队中）任务主进程无记录，sftpCancel 是静默空操作 —
  // 直接本地置 cancelled（runBatch 派发循环按 id 检查 status，会自动跳过该任务）
  if (t && t.status === 'pending') {
    t.status = 'cancelled'
    t.endTime = Date.now()
    toast.info(`已取消排队任务: ${t.name}`)
    scheduleRemoveTransfer(id, 10000)
    return
  }
  // S5-11：远程同侧复制是一条 `cp -r`，走 sshExec — 关掉通道并不会让远端进程停下，
  // 主进程本就没有可取消的句柄。原实现把 id 直接发给 sftpCancel，等于静默空操作，
  // 行一直停在「复制中」。与 pauseTransfer 的 F-6 同一手法：显式判定 + 说清楚。
  if (t && t.kind === 'copy-remote') {
    toast.info(`远端复制无法中途取消，会一直跑到完成: ${t.name}`)
    return
  }
  // S5-11：本地复制是主进程的逐文件循环，现在真能取消（fs:copy 查协作标记后抛错）。
  // 与上传/下载同一顺序：先置 cancelled 再发 sftpCancel，runLocalCopy 的 catch
  // 看到 cancelled 即静默返回，不会二次弹「复制失败」。已复制的部件留在目标目录。
  if (t && t.kind === 'copy-local' && t.status === 'running') {
    t.status = 'cancelled'
    t.endTime = Date.now()
    toast.info(`已取消复制: ${t.name}`)
    scheduleRemoveTransfer(id, 10000)
  }
  // 退避间隙（自动续传 sleep 中）没有在途 IPC 可拒绝，不先置位的话「取消」会被吞：
  // 醒来后状态仍是 running，任务会继续续传。先本地置 cancelled 再发 sftpCancel，
  // CANCELLED 拒绝到达 catch 时看到 cancelled 即静默返回（B10 同款顺序）；
  // 记录移除在此排程（常规路径靠 catch 排程，该路径到不了了）
  if (t && t.status === 'running' && (t.kind === 'upload' || t.kind === 'download')) {
    t.status = 'cancelled'
    t.endTime = Date.now()
    scheduleRemoveTransfer(id, 10000)
  }
  try { await getApi().sftpCancel(id) } catch { /* ignore */ }
}

/** 暂停传输任务（断点续传） */
function pauseTransfer(id: string): void {
  const t = transferById.get(id)
  if (!t || t.status !== 'running') return
  // F-6 修复：同侧复制（单命令 cp/fsCopy）不支持暂停，不置 paused
  if (t.kind === 'copy-local' || t.kind === 'copy-remote') {
    toast.info('复制任务不支持暂停')
    return
  }
  // 暂停走主进程 sftp:cancel（AbortSignal 无法跨 IPC，前端不再持控制器）
  t.status = 'paused'
  // 审查修复（P2）：同 disconnect 路径 — 同步 try/catch 捕获不到
  // sftpCancel 返回 Promise 的 reject，改为挂 .catch
  void getApi().sftpCancel(id).catch(() => { /* 忽略取消失败 */ })
}

/** 恢复传输任务（断点续传） */
function resumeTransfer(id: string): void {
  const t = transferById.get(id)
  if (!t || t.status !== 'paused') return
  // F-6 修复：复制任务不支持断点恢复 — 按方向重发会触发 SFTP 上传/下载产生垃圾文件
  if (t.kind === 'copy-local' || t.kind === 'copy-remote') {
    toast.warning('复制任务不支持断点恢复')
    t.status = 'error'
    t.error = '复制任务不支持断点恢复'
    t.endTime = Date.now()
    scheduleRemoveTransfer(id, 10000)
    return
  }
  t.status = 'running'
  t.error = undefined
  // P3 修复：续传后重置 startTime — 原实现沿用首次传输的 startTime，
  // 速度/剩余时间按总耗时（含暂停间隙）计算而失真。重置后速度/ETA
  // 只反映本段传输。
  t.startTime = Date.now()
  // 根据方向重新启动传输，从断点处继续
  // （断点位置由主进程扫描 .part 文件确定，前端无需传递）
  // 恢复/重试是用户在当前会话上的显式动作 → 用当前活动会话 id
  if (t.kind === 'upload') {
    runUpload(id, sshSessionId.value, t.from, t.to)
  } else {
    // 审查 M-18 修复：原实现参数写反 runDownload(id, t.to, t.from, ...) —
    // runDownload 签名是 (id, sid, remotePath, localPath)，而下载记录 from=远程/to=本地，
    // 写反后断点恢复会把本地路径当远程路径发出去，恢复永远失败
    runDownload(id, sshSessionId.value, t.from, t.to)
  }
}

/**
 * F-6 修复：按原始 from → to 精确重试失败任务（store 统一分发）。
 * 组件只负责调它 — 旧项由此处移除，新记录沿用原类别与路径，
 * 不再"取 item.from 重发到当前 cwd"导致用户换目录后传错位置。
 */
function retryTransfer(id: string): void {
  const t = transferById.get(id)
  if (!t) return
  const { kind, name, from, to, groupId, groupLabel } = t
  // direction 由 kind 唯一决定：upload/copy-local→up、download/copy-remote→down
  // （见各 push 点）。原式只判 'upload'，重试同侧本地复制会把方向图标翻成 'down'。
  const direction: 'up' | 'down' = kind === 'upload' || kind === 'copy-local' ? 'up' : 'down'
  removeTransfer(id)
  const newId = `t${transferSeq.value++}`
  addTransfer({
    id: newId, direction, kind, name,
    from, to,
    transferred: 0, total: 0,
    status: 'pending', startTime: Date.now(),
    // 分组随原任务一起续命：重试目录里的单个文件不该把它甩出组头变成一条平铺行
    groupId, groupLabel,
  })
  const task: () => Promise<void> =
    // 手动重试是当前上下文里的显式动作：投递到此刻的活动会话
    kind === 'upload' ? () => runUpload(newId, sshSessionId.value, from, to)
    : kind === 'download' ? () => runDownload(newId, sshSessionId.value, from, to)
    : kind === 'copy-local' ? () => runLocalCopy(newId, from, to)
    : () => runRemoteCopy(newId, from, to)
  runBatch([{ fn: task, id: newId, kind }]).catch(() => {

  })
}

function removeTransfer(id: string): void {
    // 注销该 id 的待删除定时器，防止定时器回调再次操作已移除的记录
    const timer = removeTimers.get(id)
    if (timer) {
      clearTimeout(timer)
      removeTimers.delete(id)
    }
    const idx = transfers.value.findIndex(t => t.id === id)
    if (idx >= 0) transfers.value.splice(idx, 1)
    transferById.delete(id)
  }

  /**
   * 删除传输任务（含进行中/暂停中的任务）：
   * running/paused 先发 sftpCancel 停掉主进程传输再移除记录，
   * 避免直接 removeTransfer 留下后台还在传的孤儿任务。
   * pending 主进程无记录，直接移除即可；随后 CANCELLED 拒绝到达时
   * runUpload/runDownload 按找不到记录静默返回。
   * 注：S5-11 之后本地复制（fsCopy）已有取消通道 —— 删行会真的让它停下；
   * 远程 cp -r 仍会在远端跑到完成（记录已移除，但远端进程不受我们控制）。
   */
  function deleteTransfer(id: string): void {
    const t = transferById.get(id)
    if (t && isActiveTransferStatus(t.status)) {
      void getApi().sftpCancel(id).catch(() => { /* 忽略取消失败 */ })
    }
    removeTransfer(id)
  }

  function scheduleRemoveTransfer(id: string, delay = 3000): void {
    // 同一 id 重复调度时先清旧定时器，避免多个定时器重复回调
    const existing = removeTimers.get(id)
    if (existing) clearTimeout(existing)
    const timer = setTimeout(() => {
      removeTimers.delete(id)
      removeTransfer(id)
    }, delay)
    removeTimers.set(id, timer)
  }

  /** 由 SftpView 在 onMounted 订阅 onSftpProgress 后调用 */
  function applyProgress(data: { id: string; transferred: number; total: number }): void {
    const t = transferById.get(data.id)
    if (!t) return
    // 终态守卫：主进程进度事件按 100ms 节流推送，任务落定（done/error/cancelled）
    // 后仍可能有一条在途事件迟到抵达，裸写会把已完成行的字节数改回中间值，
    // 使「100% 完成」的行闪回 87% 或让已取消行重新涨字节。终态只读，直接丢弃。
    if (t.status === 'done' || t.status === 'error' || t.status === 'cancelled') return
    t.transferred = data.transferred
    t.total = data.total
  }

  return {
    // state
    sshSessionId, connectionName, connectionHost, online,
    localCwd, remoteCwd, localEntries, remoteEntries,
    loadingLocal, loadingRemote, localError, remoteError,
    selectedLocal, selectedRemote, transfers, clipboard,
    // computed
    activeTransfers, queueStats,
    // session
    connect, connectFromTerminal, disconnect,
    // clipboard
    copyToClipboard,
    // local
    loadLocal, initLocalHome, getDrives, localUp, localRefresh, localMkdir, localRename, localDelete,
    localParent,
    // remote
    loadRemote, remoteUp, remoteRefresh, remoteMkdir, remoteRename, remoteDelete,
    remoteParent,
    // selection
    toggleSelectLocal, toggleSelectRemote, clearSelection,
    // transfer
    uploadFiles, downloadFiles, cancelTransfer, pauseTransfer, resumeTransfer, retryTransfer, removeTransfer, deleteTransfer, applyProgress, clearCompleted,
    // 传输队列单一写入者：addTransfer 是唯一「新增任务」入口（同时登记索引），
    // 导出供测试与任何需要直接入队的调用方复用，杜绝绕过索引的裸 push 造成 desync
    addTransfer,
    maxConcurrent, setMaxConcurrent,
    // copy
    localCopy, remoteCopy,
    // zip
    zipLocal, unzipLocal, zipRemote, unzipRemote,
    // 路径辅助（用于 ZIP 覆盖确认等场景）
    joinRemotePath, joinLocalPath, remotePathExists, localPathExists,
    // 冲突对话框取目标元信息（大小/修改时间对比）
    statRemote, statLocal,
  }
})

function basenameRemote(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx >= 0 ? path.substring(idx + 1) : path
}

/** 有界并发池：limit 路 worker 逐个消费 items。入队期建目录这类轻量 IPC 的
 *  并行化专用，与传输并发门（runBatch）语义无关 —— 别混用。 */
async function runPool<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let idx = 0
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (idx < items.length) {
      const item = items[idx++]
      await fn(item)
    }
  })
  await Promise.all(workers)
}

/** 自动续传策略：NETWORK/STALLED 类错误在初始尝试后最多再自动续传 AUTO_RETRY_MAX
 *  次，间隔按 1s/2s 指数退避（±30% 抖动防齐步重试）。立即重试大概率撞上同一段
 *  抖动白耗机会 —— 稍等让瞬断/拥塞窗口过去；断点由主进程按 .part 自动定位，
 *  重试只补没传完的尾巴。 */
const AUTO_RETRY_MAX = 2
const AUTO_RETRY_BASE_MS = 1000
function autoRetryDelayMs(attempt: number): number {
  const base = AUTO_RETRY_BASE_MS * Math.pow(2, attempt - 1)
  return Math.round(base * (0.7 + Math.random() * 0.6))
}
function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}
