/**
 * SFTP 传输面 composable — 传输队列、并发调度、上传/下载/复制执行体与记录管理。
 *
 * 从 stores/sftp.ts 拆出（docs/structure-refactor-plan.md P1）：会话态由 store 持有，
 * 经只读 computed 注入；文件面（目录缓存 / 刷新 / 目录展开）经 files 依赖注入。
 * 函数体自 stores/sftp.ts 原样平移，行为由 sftp / sftp-open-race / sftp-sort-filter
 * 测试文件经由 useSftpStore 门面钉住。
 */
import { computed, ref, type Ref } from 'vue'
import { useToastStore } from '@/stores/toast'
import { unwrapIpcErrorEnvelope } from '@/api/client'
import { basenameLocal } from '@/utils/transfer-conflict'
import { isUnsafeRemoteName } from '@/utils/remote-name'
import { describeTransferError } from '@/utils/transfer-error'
import { isActiveTransferStatus, type TransferKind, type TransferStatus } from '@/utils/transfer-format'
import { sortForRestore, toTransferItem } from '@/utils/upload-task-restore'
import { nextTransferSeqFrom } from '../../shared/upload-task-core'
import { shSingleQuote } from '../../shared/sh-quote-core'

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
  /** 上次运行被中断（崩溃/断线/应用退出），恢复后为 paused，展示「未完成 · 可继续」。
   *  与 error 分开：中断不是失败，点继续会接着远端 .part 传。 */
  interrupted?: boolean
}

// ─── 模块级纯辅助（与会话/文件面无耦合） ──────────

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

export interface SftpTransferDeps {
  getApi: () => ElectronAPI
  sshSessionId: () => string
  online: () => boolean
  /** 当前会话对应的连接配置 id（恢复上传历史用；store 持有真实状态） */
  activeConnectionId: () => string
  files: {
    localEntries: Ref<FileEntry[]>
    remoteEntries: Ref<SftpEntry[]>
    localCwd: Ref<string>
    remoteCwd: Ref<string>
    localRefresh: () => Promise<void>
    remoteRefresh: () => Promise<void>
    expandLocalDir: (localDir: string) => Promise<Array<{ localPath: string; relPath: string; size: number }>>
    expandRemoteDir: (remoteDir: string, sid: string) => Promise<Array<{ remotePath: string; relPath: string; size: number }>>
  }
}

export function useSftpTransfer(deps: SftpTransferDeps) {
  const { getApi } = deps
  const toast = useToastStore()
  // 会话态只读视图：让平移过来的函数体保持 `.value` 读法不变
  const sshSessionId = computed(deps.sshSessionId)
  const online = computed(deps.online)
  const activeConnectionId = computed(deps.activeConnectionId)

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
      // 字节只累计未结束的任务：上传记录跨重启保留，完成行永久留在列表里，
      // 若把它们的字节一起算进分母，新队列的百分比会被历史记录稀释，
      // 全完成时又钉死在 100%（看起来像还在传）。
      if (t.status === 'running' || t.status === 'pending' || t.status === 'paused') {
        s.bytesTransferred += t.transferred
        if (t.total > 0) s.bytesTotal += t.total
      }
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
  /**
   * 把主进程持有的上传记录并回传输中心（按 id 幂等，不会产生重复行）。
   * 换主机时先丢掉内存里的上传行：主进程已持久化，属于上一台的行不该和新主机混在一起。
   */
  async function restoreUploads(): Promise<void> {
    const connId = activeConnectionId.value
    if (!connId) return
    // 「在跑/在排队」的上传行以内存为准：online 置真到这次列表往返返回之间，
    // 用户可能已经发起一条新上传，而计数器重启后从 1 重来（bump 在本次循环后才做），
    // 它的 id 会撞上历史记录。删掉会把在途传输从列表里抹掉，完成后写进一个已脱钩的对象。
    const isLive = (t: TransferItem): boolean => t.status === 'running' || t.status === 'pending'
    // S5-25：上传与下载都以内存为准（两类都有主进程记录）
    const kept = transfers.value.filter(t => t.kind !== 'upload' && t.kind !== 'download' || isLive(t))
    if (kept.length !== transfers.value.length) {
      transfers.value = kept
      reindexTransfers()
    }
    const records = await getApi().sftpListUploadTasks(connId)
    // 列表返回前用户又切了主机：这批属于上一台，丢弃
    if (activeConnectionId.value !== connId) return
    if (!Array.isArray(records) || records.length === 0) return

    for (const rec of sortForRestore(records)) {
      const live = transferById.get(rec.id)
      if (live && isLive(live)) continue
      const item = toTransferItem(rec)
      if (live) Object.assign(live, item)
      else addTransfer(item)
      // 主进程仍显示 running/pending：只可能是渲染进程重载而主进程还在传。
      // 先停住（同 id 重入由主进程的取消闭包查重保证安全），点「继续」再跑。
      if (rec.status === 'running' || rec.status === 'pending') {
        const row = live ?? item
        row.status = 'paused'
        row.interrupted = true
        void getApi().sftpPauseUploadTask(rec.id, true).catch(() => {})
        void getApi().sftpCancel(rec.id).catch(() => {})
      }
    }
    // 历史 id 会撞上重启后从 1 开始的计数器：新上传会覆盖旧记录
    transferSeq.value = Math.max(transferSeq.value, nextTransferSeqFrom(records))
  }

  /**
   * 清空已结束（done/error/cancelled）的传输记录。
   * 上传的历史在主进程持久化，离线时也能按 activeConnectionId 清掉，否则重连后
   * 「已清除」的行会原样恢复回来。
   */
  function clearCompleted(): void {
    transfers.value = transfers.value.filter(
      t => t.status === 'running' || t.status === 'paused' || t.status === 'pending',
    )
    reindexTransfers()
    if (activeConnectionId.value) {
      void getApi().sftpClearUploadTasks(activeConnectionId.value).catch(() => { /* 记录侧失败不影响展示 */ })
    }
  }

  /**
   * 断开连接时由 store 调用：清空待删除定时器，并把在途任务按类别收尾。
   * （自 stores/sftp.ts 的 disconnect 原样平移，会话态复位仍在 store。）
   */
  function handleDisconnect(): void {
    // 清空所有待删除定时器：断线后旧定时器若不清除，重连后可能误删新传输记录
    for (const timer of removeTimers.values()) clearTimeout(timer)
    removeTimers.clear()
    // B10 修复：清空前先对在途（running）任务显式取消并置 cancelled，
    // 避免每个传输 Promise 的 reject 走通用错误路径连弹"上传/下载失败"。
    // 先置状态再发 sftpCancel：CANCELLED 拒绝到达 catch 时看到 cancelled 即静默 return。
    //
    // S5-33：下载与上传同等待遇。原实现只给上传「断线≠任务结束」的豁免，理由是
    // 「下载没有可恢复的远端半成品」—— 那是 S5-25 之前的口径。现在下载也会写
    // 本地 .part 并登记记录，断线时把行清空 + 记录标 cancelled，等于把用户已经
    // 下载了一半的字节和续传入口一起丢掉：重启后不会显示为「可继续」，那个 .part
    // 永远对不上任何记录，只能靠用户自己去本地目录里翻。
    // 同侧复制仍按取消处理（cp -r 没有断点语义，记录也不参与续传）。
    for (const t of transfers.value) {
      if (t.kind === 'upload' || t.kind === 'download') {
        if (t.status === 'running' || t.status === 'pending') {
          t.status = 'paused'
          t.interrupted = true
          t.endTime = undefined
          void getApi().sftpPauseUploadTask(t.id, true).catch(() => { /* 记录侧失败不影响断开 */ })
          void getApi().sftpCancel(t.id).catch(() => { /* 忽略取消失败 */ })
        }
        continue
      }
      if (t.status === 'running') {
        t.status = 'cancelled'
        t.endTime = Date.now()
        // 审查修复（P2）：sftpCancel 返回 Promise — 原实现同步 try/catch
        // 只能捕获调用时的同步抛错，IPC reject 变成 unhandled rejection。
        // 改为对返回的 Promise 挂 .catch
        void getApi().sftpCancel(t.id).catch(() => { /* 忽略取消失败 */ })
      }
    }
    // 上传/下载行保留（记录在断开后仍可见、可继续）；同侧复制行清空
    transfers.value = transfers.value.filter(t => t.kind === 'upload' || t.kind === 'download')
    reindexTransfers()
  }

  // ─── 同侧复制粘贴 ─────────────────────────────
  /**
   * 本地同侧复制：将 paths 复制到 targetDir
   * 支持目录递归，进度通过 sftp:progress 事件上报
   * @param renameMap 冲突「重命名」落地下发的 原路径 → 新顶层名 映射（S5-21 起
   *   由 doTransfer 的冲突确认产出，缺省保持原名 = 覆盖）
   */
  async function localCopy(paths: string[], targetDir: string, renameMap?: Record<string, string>): Promise<void> {
    const tasks: Array<{ id: string; src: string; dest: string; name: string }> = []
    for (const src of paths) {
      const name = renameMap?.[src] ?? basenameLocal(src)
      const dest = await getApi().fsPathJoin(targetDir, name)
      const id = `t${transferSeq.value++}`
      // S5-26：入队时就能知道单文件的字节数（目录仍为 0 —— 展开要递归 stat，
      // 那正是主进程 fs:copy 顺手做完的事）。有值就直接给，省得先画一帧滑块
      // 再跳百分比。先查列表缓存：同 uploadFiles 的口径，避免每个源多一次 IPC。
      const hit = deps.files.localEntries.value.find(e => e.path === src)
      const knownTotal = hit && !hit.isDirectory ? hit.size || 0 : 0
      addTransfer({
        id, direction: 'up', kind: 'copy-local', name,
        from: src, to: dest,
        transferred: 0, total: knownTotal,
        status: 'pending', startTime: Date.now(),
      })
      tasks.push({ id, src, dest, name })
    }
    // F-6 修复：带上 id/kind — copy 类不受上传/下载并发门限制，
    // 且 runBatch 派发前按 id 检查 status 防竞态
    runBatch(tasks.map(t => ({ fn: () => runLocalCopy(t.id, t.src, t.dest), id: t.id, kind: 'copy-local' as const }))).then(async (results) => {
      if (results.some(r => r.ok)) await deps.files.localRefresh()
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
   * @param renameMap 同 localCopy：冲突确认产出的 原路径 → 新顶层名
   */
  async function remoteCopy(paths: string[], targetDir: string, renameMap?: Record<string, string>): Promise<void> {
    if (!sshSessionId.value || !online.value) return
    const tasks: Array<{ id: string; src: string; dest: string; name: string }> = []
    for (const src of paths) {
      const name = renameMap?.[src] ?? basenameRemote(src)
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
      if (results.some(r => r.ok)) await deps.files.remoteRefresh()
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
      // S5-20：cp -r 会跟随符号链接（GNU cp 无 -P 时默认跟随命令行给出的链接），
      // 把链接目标的内容拷成真实文件/目录 —— 与本地侧 fs:copy 的 lstat fail-closed
      // 口径对齐，这里先 stat 探测，命中链接即拒绝该条而不是静默跟随。
      // 目录内部的链接由 cp 自己的语义决定，管不到也不该管（那会变成逐文件拦截）。
      const st = await getApi().sftpStat(sshSessionId.value, src)
      if (st.isSymbolicLink) {
        throw new Error('符号链接不支持远端复制（会丢失链接语义），请改用下载后手动重建')
      }
      // 用 cp -r 递归复制，路径统一经 POSIX shell 单引号转义
      // B7 修复：cp -r 大目录必超 sshExec 默认 30s 超时 — 显式传 300s
      const result = await sshExecWithTimeout(getApi(), sshSessionId.value, `cp -r ${shSingleQuote(src)} ${shSingleQuote(dest)}`, 300)
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

  // ─── 传输 ─────────────────────────────────────
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
    const remoteBaseDir = targetDir ?? deps.files.remoteCwd.value
    const tasks: Array<{
      id: string
      localPath: string
      remotePath: string
      size: number
      name: string
      groupId?: string
      groupLabel?: string
    }> = []
    // 目录上传涉及的全部远程父目录（去重后并发创建，见循环后）
    const remoteDirs = new Set<string>()

    for (const lp of localPaths) {
      const name = renameMap?.[lp] ?? basenameLocal(lp)
      const remoteBase = remoteBaseDir === '/' ? '/' + name : remoteBaseDir + '/' + name

      // 判断是文件还是目录：优先查 localEntries（应用内拖拽），否则用 fsStat（OS 拖拽）
      let isDir = false
      // 入队时一并抓文件大小（大文件优先排序用；抓不到按 0 处理排末尾）
      let fileSize = 0
      const localEntry = deps.files.localEntries.value.find(e => e.path === lp)
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
        const files = await deps.files.expandLocalDir(lp)
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
          tasks.push({ id, localPath: f.localPath, remotePath, size: f.size, name: f.relPath, groupId, groupLabel })
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
        tasks.push({ id, localPath: lp, remotePath: remoteBase, size: fileSize, name })
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

    // 入队即把整批登记为 pending：崩溃后「还没轮到」的文件也在历史里，否则用户
    // 只看到一条断点，其余文件已被遗忘。登记失败不阻断传输（记录是附加能力）。
    const registered = tasks.filter(t => transferById.get(t.id)?.status === 'pending')
    if (registered.length > 0) {
      try {
        await getApi().sftpRegisterUploadTasks(sid, registered.map(t => ({
          id: t.id,
          kind: 'upload' as const,
          localPath: t.localPath,
          remotePath: t.remotePath,
          name: t.name,
          total: t.size,
          groupId: t.groupId,
          groupLabel: t.groupLabel,
        })))
      } catch (e) {
        console.warn('[sftp] 上传任务登记失败（不影响传输）:', e)
      }
      // 登记往返期间用户已取消/删除的行：取消的补发 sftpCancel 让主进程同步终态，
      // 删除的移除刚登记进去的记录 —— 否则「删掉后重连又冒出来」
      for (const t of registered) {
        const row = transferById.get(t.id)
        if (!row) void getApi().sftpRemoveUploadTask(t.id).catch(() => { /* ignore */ })
        else if (row.status === 'cancelled') void getApi().sftpCancel(t.id).catch(() => { /* ignore */ })
      }
    }

    // F-6 修复：带上 id/kind（runBatch 派发前检查 + 并发门计数）
    runBatch(tasks.map(t => ({ fn: () => runUpload(t.id, sid, t.localPath, t.remotePath), id: t.id, kind: 'upload' as const }))).then(async (results) => {
      // 至少有一个成功才刷新目录，避免无谓请求
      if (results.some(r => r.ok)) await deps.files.remoteRefresh()
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
    const localBaseDir = targetDir ?? deps.files.localCwd.value
    // S5-25：带上 name/groupId/groupLabel —— 下载记录要与上传记录同构，
    // 展示层（组头折叠）与恢复逻辑才能共用一套映射
    const tasks: Array<{ id: string; remotePath: string; localPath: string; size: number; name: string; groupId?: string; groupLabel?: string }> = []
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
      let isSymlink = false
      const remoteEntry = deps.files.remoteEntries.value.find(e => e.path === rp)
      if (remoteEntry) {
        isDir = remoteEntry.isDirectory
        isSymlink = remoteEntry.isSymlink
      } else {
        try {
          const st = await getApi().sftpStat(sid, rp)
          isDir = st.isDirectory
          isSymlink = st.isSymbolicLink
        } catch {
          isDir = false
        }
      }
      // S5-20：单文件下载前拦截符号链接。sftpDownload 走 sftp.open + read，
      // 跟随链接会把目标内容写成本地普通文件 —— 链接语义丢失且用户以为拿到了
      // 那个文件本身。目录下载已在 expandRemoteDir 里逐项跳过。
      if (isSymlink) {
        toast.error(`符号链接不支持下载（会丢失链接语义），已跳过：${rp}`)
        continue
      }

      if (isDir) {
        // 目录：递归展开（sid 钉死传入，S5-12）
        let files: Array<{ remotePath: string; relPath: string; size: number }>
        try {
          files = await deps.files.expandRemoteDir(rp, sid)
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
          tasks.push({
            id, remotePath: f.remotePath, localPath, size: f.size,
            name: f.relPath, groupId, groupLabel,
          })
        }
      } else {
        // 文件：直接加入（大小从 remoteEntries 缓存取；不在缓存里时 stat 一次
        // 拿真实大小参与大文件优先排序，取不到按 0 排末尾）
        const id = `t${transferSeq.value++}`
        const remoteEntry = deps.files.remoteEntries.value.find(e => e.path === rp)
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
        tasks.push({ id, remotePath: rp, localPath: localBase, size: fileSize, name })
      }
    }

    // 去重后的本地父目录并发创建（同上传侧 remoteDirs 的去重并行化）
    if (localDirs.size > 0) {
      await runPool([...localDirs], 8, d => getApi().fsMkdir(d).catch(() => { /* ignore */ }))
    }

    // 大文件优先：同 uploadFiles —— 并发门先放大文件（各占一条独立连接的
    // 2 MB 窗口），小文件填缝在后，减少长尾延迟（稳定排序，同大小维持原序）
    tasks.sort((a, b) => b.size - a.size)

    // S5-25：入队即登记为 pending（同上传侧口径）—— 崩溃后「还没轮到」的
    // 下载也在历史里，本地 .part 才有入口续上。登记失败不阻断传输。
    const registeredDown = tasks.filter(t => transferById.get(t.id)?.status === 'pending')
    if (registeredDown.length > 0) {
      try {
        await getApi().sftpRegisterUploadTasks(sid, registeredDown.map(t => ({
          id: t.id,
          kind: 'download' as const,
          localPath: t.localPath,
          remotePath: t.remotePath,
          name: t.name,
          total: t.size,
          groupId: t.groupId,
          groupLabel: t.groupLabel,
        })))
      } catch (e) {
        console.warn('[sftp] 下载任务登记失败（不影响传输）:', e)
      }
      for (const t of registeredDown) {
        const row = transferById.get(t.id)
        if (!row) void getApi().sftpRemoveUploadTask(t.id).catch(() => { /* ignore */ })
        else if (row.status === 'cancelled') void getApi().sftpCancel(t.id).catch(() => { /* ignore */ })
      }
    }

    // F-6 修复：带上 id/kind（runBatch 派发前检查 + 并发门计数）
    runBatch(tasks.map(t => ({ fn: () => runDownload(t.id, sid, t.remotePath, t.localPath), id: t.id, kind: 'download' as const }))).then(async (results) => {
      if (results.some(r => r.ok)) await deps.files.localRefresh()
    }).catch((err) => {
      // C2 修复：防止 post-batch refresh 抛错导致未捕获 Promise rejection
      console.warn('[sftp] 下载批次收尾刷新失败:', err)
    })
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
          if (t.kind !== 'upload' && t.kind !== 'download') continue
          if (t.status === 'running') {
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

  async function runUpload(
    id: string,
    sid: string,
    localPath: string,
    remotePath: string,
    attempt = 1,
    opts?: { resume?: boolean },
  ): Promise<void> {
    const t = transferById.get(id)
    if (!t) return
    // F-5 竞态双保险：任务在排队期间被取消则不再启动
    if (t.status === 'cancelled') return
    t.status = 'running'
    t.interrupted = undefined
    t.endTime = undefined
    try {
      // 审查 M-18 修复：IPC 契约只有 4 个参数（preload/env.d.ts/main.ts 三处一致）—
      // 原调用多传 resumeOffset + AbortSignal：preload 只透传前 4 参，两者被静默丢弃
      // （AbortSignal 也无法结构化克隆过 IPC）。续传由主进程扫描远端 .part 文件
      // 自动定位断点，暂停/取消走 sftp:cancel（pauseTransfer 已调用）。
      // 第 5 参 meta 只携带展示字段与「是否显式继续」，归属 id 由主进程解析。
      await getApi().sftpUpload(id, sid, localPath, remotePath, {
        name: t.name,
        groupId: t.groupId,
        groupLabel: t.groupLabel,
        size: t.size ?? t.total,
        resume: opts?.resume,
      })
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
        return runUpload(id, sid, localPath, remotePath, attempt + 1, opts)
      }
      t.status = code === 'CANCELLED' ? 'cancelled' : 'error'
      t.error = message || '上传失败'
      t.interrupted = undefined
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
      // .part 自动定位，取消走 sftp:cancel）。
      // S5-25：第 5 参 meta 携带展示字段，主进程据此写下载记录（跨重启可续传）
      await getApi().sftpDownload(id, sid, remotePath, localPath, {
        name: t.name,
        groupId: t.groupId,
        groupLabel: t.groupLabel,
        size: t.size ?? t.total,
      })
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
      // S5-33：上传与下载都已在主进程登记为 pending，都要补发 sftpCancel 让记录落成
      // cancelled（保留为历史），否则下次启动它会被归一化为「未完成 · 可继续」重新
      // 冒出来。原实现只判 upload —— 下载行取消后记录仍是 pending，重启就多一条假的
      // 「可继续」断点，而本地 .part 早没了。
      if (t.kind === 'upload' || t.kind === 'download') {
        void getApi().sftpCancel(id).catch(() => { /* ignore */ })
      }
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
    // 用户主动暂停不是「中断」：不展示「未完成 · 可继续」的警示语义
    t.interrupted = undefined
    // 上传/下载都要让主进程把记录标成 paused，否则崩溃/退出后会被归一化当成中断，
    // 且「继续」与自动重试的 CANCELLED 谁先到都不会把 paused 覆盖成 cancelled
    // （S5-25：下载记录同一条通道，主进程按记录自身的 kind 判定方向）
    if (t.kind === 'upload' || t.kind === 'download') {
      void getApi().sftpPauseUploadTask(id, false).catch(() => { /* ignore */ })
    }
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
    // 记录可以离线展示，但恢复必须落到一台活着的会话上（sid 已随断开清空）
    if (!online.value || !sshSessionId.value) {
      toast.warning('请先连接远程主机再继续传输')
      return
    }
    // 置 pending 交给并发门：恢复一个大目录时会一次性出现几十上百条 paused，
    // 直接逐条 runUpload 会瞬间开满传输连接（现实现原就有的缺陷）
    t.status = 'pending'
    t.error = undefined
    t.interrupted = undefined
    t.endTime = undefined
    // P3 修复：续传后重置 startTime — 原实现沿用首次传输的 startTime，
    // 速度/剩余时间按总耗时（含暂停间隙）计算而失真。重置后速度/ETA
    // 只反映本段传输。
    t.startTime = Date.now()
    const upload = t.kind === 'upload'
    // 断点位置由主进程扫描 .part 文件确定，前端只需声明「这是显式继续」
    const task = upload
      ? () => runUpload(id, sshSessionId.value, t.from, t.to, 1, { resume: true })
      : () => runDownload(id, sshSessionId.value, t.from, t.to)
    void runBatch([{ fn: task, id, kind: t.kind }]).catch(() => { /* 单条失败已由 run* 内部分类 */ })
  }

  /**
   * F-6 修复：按原始 from → to 精确重试失败任务（store 统一分发）。
   * 组件只负责调它 — 旧项由此处移除，新记录沿用原类别与路径，
   * 不再"取 item.from 重发到当前 cwd"导致用户换目录后传错位置。
   */
  function retryTransfer(id: string): void {
    const t = transferById.get(id)
    if (!t) return
    const { kind, name, from, to, groupId, groupLabel, size } = t
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
      // 保留入队时的大小，重试记录（主进程侧）才不会把总量写成 0
      size,
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
    // S5-33：上传与下载行都有主进程持久记录，删行 = 用户不想再看到它，两类都要清。
    // 原实现只判 upload —— 下载行删掉后记录还留在磁盘上，重启又冒出来一条历史。
    // 远端/本地的 .part 刻意保留（清记录是只读展示操作，不替用户删数据）。
    if (transferById.get(id)?.kind === 'upload' || transferById.get(id)?.kind === 'download') {
      void getApi().sftpRemoveUploadTask(id).catch(() => { /* 记录侧失败不影响本地移除 */ })
    }
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
    // S5-33：上传/下载记录跨重启保留、只有手动清除才删除：行自动消失而记录留在
    // 磁盘上，重启后那条历史会自己冒回来 —— 用户看到的是「删了又出现」。
    // 同侧复制没有持久记录，保持原有的自动清理（3s/10s）。
    if (transferById.get(id)?.kind === 'upload' || transferById.get(id)?.kind === 'download') return
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
    // S5-26：total 允许被进度事件「撑开」。本地复制（fs:copy）的总量是主进程
    // 递归收集阶段结束后才知道的，任务入队时只能写 0（渲染层此刻还没法知道）。
    // 原实现直接丢弃 total=0 的事件，导致复制行永远画成「不定进度」的滑块 ——
    // 主进程明明在报真实数字，渲染层一次都没用上。只在「当前为 0」时采纳，
    // 已经知道的总量不会被后续事件改小（文件不会越收越多，只会越收越少）。
    if (data.total > 0 && t.total === 0) t.total = data.total
    t.transferred = data.transferred
  }

  return {
    // state
    transfers, maxConcurrent, addTransfer, reindexTransfers,
    // computed
    activeTransfers, queueStats,
    // session 配合
    restoreUploads, handleDisconnect,
    // queue 管理
    clearCompleted, setMaxConcurrent,
    // transfer
    uploadFiles, downloadFiles, cancelTransfer, pauseTransfer, resumeTransfer,
    retryTransfer, removeTransfer, deleteTransfer, applyProgress,
    // S5-33：导出调度入口供测试直接驱动（自动清理的豁免判据是可测行为）
    scheduleRemoveTransfer,
    // copy
    localCopy, remoteCopy,
  }
}
