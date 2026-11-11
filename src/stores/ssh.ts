/**
 * SSH Store — 管理 SSH 连接配置和终端会话
 * 连接配置通过 IPC 持久化到主进程(safeStorage 加密),不再走 localStorage
 */
import { acceptHMRUpdate, defineStore } from 'pinia'
import { ref, computed, onScopeDispose, getCurrentScope } from 'vue'
import { useToastStore } from './toast'
import { toFriendlyMessage } from '@/api/client'

export type SSHConnection = SSHConnectionRecord

/**
 * 连接备注的单一来源读法：优先 note 字段，兼容旧数据（备注曾存进 tags[0]）。
 * 所有展示/搜索连接备注的地方都必须走这里 —— 此前 ConnectionRow 与 RemoteManager
 * 搜索各抄了一份 fallback，而仪表盘快连/新建终端菜单/定时任务下拉/Docker 选择器
 * 干脆不显示，造成「这里显示那里不显示」。
 */
export function connNote(conn: Pick<SSHConnection, 'note' | 'tags'>): string {
  return conn.note || conn.tags?.[0] || ''
}

export interface TerminalSession {
  id: string
  name: string
  type: 'local' | 'remote'
  shell: string           // 本地: shell名, 远程: username@host
  /** 会话启动目录（本地 PTY 才有值）。chip 上要显示「在哪个文件夹」，
   *  这是唯一读得到的事实源——输入栏不再靠猜或读进程 cwd。 */
  cwd?: string
  connectionId?: string   // 远程连接的 ID
  createdAt?: number      // 会话创建时间戳（用于判断本地终端是否“启动即退出”）
  /** 后台会话有新输出（切走时置位，激活时清零） */
  hasActivity?: boolean
  /** 重连进行中（主进程 ssh:reconnect 事件驱动）；null = 无重连 */
  reconnect?: { attempt: number; maxAttempts: number } | null
  /** 自动重连已放弃（等待用户手动重连） */
  reconnectGaveUp?: boolean
  /** 主进程侧已没有这条会话（探测回 SESSION_NOT_FOUND 时置位）。与 reconnectGaveUp
   *  的区别是来源：那个由主进程推送，这个由本端实际执行失败反证。重连成功后清除。 */
  unavailable?: boolean
}

interface CommandHistoryEntry {
  sessionId: string
  command: string
  count: number
  lastUsedAt: number
}

/** 命令链条目：记录连续命令对 (prevCmd -> nextCmd) */
interface CommandChainEntry {
  sessionId: string
  prevCmd: string
  nextCmd: string
  count: number
  lastUsedAt: number
}

const LEGACY_KEY = 'ckun-ssh-connections'

export const useSSHStore = defineStore('ssh', () => {
  // Pinia 只在 setup 的同步段 scope.run()，所以任何 await 之后（含 action 里）都拿不到
  // 活跃 scope。这里先把本 store 的 scope 存下来，供 init() 尾部登记监听清理时用 ——
  // 见 _setupHostKeyNotifications。
  const _setupScope = getCurrentScope()

  // ─── State ────────────────────────────────────
  const connections = ref<SSHConnection[]>([])
  const sessions = ref<TerminalSession[]>([])
  const activeSessionId = ref('')
  const loaded = ref(false)

  /** MRU 栈：栈顶是最近激活的会话（Ctrl+Tab 切换器与「回到上次会话」的数据源） */
  const mruIds = ref<string[]>([])

  /** 后台活动标记：终端数据到达且会话非活跃时由终端组件置位 */
  function markActivity(id: string): void {
    if (id === activeSessionId.value) return
    const s = sessions.value.find(x => x.id === id)
    if (s) s.hasActivity = true
  }

  /** 按终端会话维度的命令历史（内存版） */
  const commandHistory = ref<CommandHistoryEntry[]>([])

  /** 命令链记录：连续命令对的共现统计 */
  const commandChains = ref<CommandChainEntry[]>([])

  /** 命令历史最大条目数（超出后淘汰最久未使用的条目） */
  const MAX_COMMAND_HISTORY = 500

  /** 命令链最大条目数 */
  const MAX_COMMAND_CHAINS = 1000

  /** 终端会话最大数量（防止无限制创建导致内存溢出） */
  const MAX_SESSIONS = 20

  /** 上一次执行的命令（按会话记录，用于构建命令链） */
  const lastCommandBySession = ref<Record<string, string>>({})

  /** 终端选中片段注册表（「问 AI」上下文采集：终端组件选中变化时写入） */
  const selectionBySession = ref<Record<string, string>>({})

  /** 终端工作目录缓存（远程 OSC 7 经 sshGetCwd 定时同步；本地经 ptyGetCwd） */
  const cwdBySession = ref<Record<string, string>>({})

  // ─── Computed ─────────────────────────────────
  const activeSession = computed(() => sessions.value.find(s => s.id === activeSessionId.value))

  /** 当前有活动会话的连接 ID 集合(用于在线状态指示) */
  const onlineConnectionIds = computed(() => {
    const set = new Set<string>()
    for (const s of sessions.value) {
      if (s.type === 'remote' && s.connectionId) set.add(s.connectionId)
    }
    return set
  })

  function isOnline(connId: string): boolean {
    return onlineConnectionIds.value.has(connId)
  }

  // ─── 初始化与迁移 ─────────────────────────────
  async function init(): Promise<void> {
    if (loaded.value) return
    await migrateLegacyIfNeeded()
    await loadConnections()
    loaded.value = true
    // s1 修复：主机密钥验证事件监听（TOFU 通知 + 中间人攻击警告）
    _setupHostKeyNotifications()
    // s-标签状态：重连事件的全局跟踪（状态条与标签状态点共用这份状态）
    _setupReconnectTracking()
  }

  /** s1 修复：监听并提示主机密钥验证事件 */
  function _setupHostKeyNotifications(): void {
    const api = window.electronAPI
    if (!api) return
    const toast = useToastStore()
    const offNew = api.onHostKeyNew(({ hostKey, fingerprint }) => {
      toast.warning(
        `首次连接到 ${hostKey}，已记录服务器指纹：${fingerprint.slice(0, 24)}...`,
        6000,
      )
    })
    const offMismatch = api.onHostKeyMismatch(({ hostKey }) => {
      toast.error(
        `⚠️ ${hostKey} 服务器密钥变更！可能存在中间人攻击，连接已被拒绝。`,
        0, // 不自动关闭，强制用户确认
      )
    })
    // 钉扎表读不出来时的拒绝：不是"密钥变了"，是本机 known_hosts 不可用，
    // 处置动作是修磁盘而不是查服务器，所以不并入上面那条文案。
    const offUnpinned = api.onHostKeyUnpinned(({ hostKey }) => {
      toast.error(
        `${hostKey} 连接已被拒绝：本机 known_hosts 没能完整读出，无从判断这台主机是否见过。`
        + '请修复或删除该文件后重连（首连会重新钉扎指纹）。',
        0,
      )
    })
    // Pinia scope 销毁时清理监听器（HMR 场景）。
    // 注意：这里离 init() 的两个 await 已经远了，activeEffectScope 是 null —— 直接
    // onScopeDispose 只会冒 "no active effect scope" 告警并且**不登记**，三条监听
    // 从此摘不掉。改用 setup 同步段抓到的 scope run() 回去登记。
    const removeHostKeyListener = () => {
      if (typeof offNew === 'function') offNew()
      if (typeof offMismatch === 'function') offMismatch()
      if (typeof offUnpinned === 'function') offUnpinned()
    }
    if (_setupScope) _setupScope.run(() => onScopeDispose(removeHostKeyListener))
  }

  /** s-标签状态：重连事件的全局跟踪（RemoteTerminal 的状态条与标签状态点共用这份状态） */
  function _setupReconnectTracking(): void {
    const api = window.electronAPI
    if (!api?.onSshReconnect) return
    const off = api.onSshReconnect((data) => {
      const s = sessions.value.find(x => x.id === data.id)
      if (!s) return // 重连完成前用户已关标签：静默丢弃
      if (data.phase === 'reconnecting' || data.phase === 'retry') {
        s.reconnect = { attempt: data.attempt, maxAttempts: data.maxAttempts }
        s.reconnectGaveUp = false
      } else if (data.phase === 'success') {
        s.reconnect = null
        s.reconnectGaveUp = false
        // 重连成功 = 这条会话又在主进程里了，探测反证的失效标记必须一起清掉，
        // 否则目录 chip 会一直停在「未连接」
        s.unavailable = false
      } else if (data.phase === 'gave-up') {
        s.reconnect = null
        // cancelled/config-missing 是「收摊」不是「失败」：清状态，不亮红点
        s.reconnectGaveUp = data.reason === 'exhausted' || data.reason === 'not-retryable'
      }
    })
    if (_setupScope) _setupScope.run(() => onScopeDispose(() => off()))
  }

  async function migrateLegacyIfNeeded(): Promise<void> {
    try {
      const raw = localStorage.getItem(LEGACY_KEY)
      if (!raw) return
      const legacy = JSON.parse(raw)
      if (!Array.isArray(legacy) || legacy.length === 0) {
        localStorage.removeItem(LEGACY_KEY)
        return
      }
      const api = window.electronAPI
      if (!api) return
      await api.sshMigrateLegacy(legacy)
      localStorage.removeItem(LEGACY_KEY)
    } catch (e) {
      // m6 修复：记录迁移失败日志，便于排查兼容性问题
      console.error('[ssh] 旧版连接迁移失败:', e)
    }
  }

  async function loadConnections(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    try {
      connections.value = await api.sshListConnections()
    } catch (err) {
      console.error('[ssh] loadConnections failed:', err)
    }
  }

  // ─── 本地终端 ─────────────────────────────────

  /** 解析 shell 到实际可执行文件路径（Windows 下检测常见安装位置） */
  async function resolveShellPath(shell: string): Promise<string> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')

    // 非 Windows 环境直接使用 shell 名
    const isWindows = navigator.userAgent.includes('Windows')
    if (!isWindows) return shell

    const systemRoot = 'C:\\Windows'
    const pf = 'C:\\Program Files'
    const pf86 = 'C:\\Program Files (x86)'

    // 优先通过主进程解析 PATH / 注册表
    const resolvedFromPath = shell === 'Git Bash'
      ? await api.fsResolveProgram('bash')
      : shell === 'PowerShell'
        ? (await api.fsResolveProgram('pwsh')) || (await api.fsResolveProgram('powershell'))
        : null

    const candidates: Record<string, string[]> = {
      CMD: [`${systemRoot}\\System32\\cmd.exe`, 'cmd.exe'],
      PowerShell: [
        ...(resolvedFromPath ? [resolvedFromPath] : []),
        `${pf}\\PowerShell\\7\\pwsh.exe`,
        `${pf}\\PowerShell\\7-preview\\pwsh.exe`,
        `${systemRoot}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`,
        'pwsh.exe',
        'powershell.exe',
      ],
      'Git Bash': [
        ...(resolvedFromPath ? [resolvedFromPath] : []),
        `${pf}\\Git\\bin\\bash.exe`,
        `${pf}\\Git\\usr\\bin\\bash.exe`,
        `${pf86}\\Git\\bin\\bash.exe`,
        `${pf86}\\Git\\usr\\bin\\bash.exe`,
        'bash.exe',
        'bash',
      ],
      WSL: [`${systemRoot}\\System32\\wsl.exe`, 'wsl.exe'],
    }

    const list = candidates[shell] || [shell]
    for (const path of list) {
      if (await api.fsExists(path)) return path
    }

    throw new Error(`未找到 ${shell}，请确认已安装并添加到系统 PATH`)
  }

  /** 获取 shell 启动参数（解决某些 shell 被 node-pty 启动后立即退出的问题） */
  function getShellArgs(shell: string): string[] {
    switch (shell) {
      case 'PowerShell':
        return ['-NoLogo', '-NoExit']
      case 'Git Bash':
        return ['--login', '-i']
      case 'CMD':
        return []
      case 'WSL':
        return []
      default:
        return []
    }
  }

  async function createLocalTerminal(shell: string, cwd?: string): Promise<string> {
    // s4 修复：会话数量上限保护
    if (sessions.value.length >= MAX_SESSIONS) {
      throw new Error(`终端会话数量已达上限（${MAX_SESSIONS}），请先关闭部分会话`)
    }
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')

    const shellPath = await resolveShellPath(shell)
    const args = getShellArgs(shell)
    // cwd：Git 仓库管理「打开终端」要直落在仓库目录（pty:create 原生支持）
    const result = await api.ptyCreate(cwd ? { shell: shellPath, args, cwd } : { shell: shellPath, args })

    sessions.value.push({
      id: result.id,
      name: shell,
      type: 'local',
      shell,
      cwd: cwd || '',
      createdAt: Date.now(),
    })

    activeSessionId.value = result.id
    return result.id
  }

  // ─── 远程连接 ─────────────────────────────────
  async function connectRemote(connectionId: string, cols: number, rows: number): Promise<string> {
    // s4 修复：会话数量上限保护
    if (sessions.value.length >= MAX_SESSIONS) {
      throw new Error(`终端会话数量已达上限（${MAX_SESSIONS}），请先关闭部分会话`)
    }
    const conn = connections.value.find(c => c.id === connectionId)
    if (!conn) throw new Error(`Connection ${connectionId} not found`)

    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')

    // v2 改进：connecting UI 标志统一由 useSshNavigation 管理（避免 store 层和 UI 层
    // 双重重置导致竞态）；store.connectRemote 只负责数据状态（sessions / activeSessionId）
    // 剥离 Vue 响应式 Proxy,避免 IPC structuredClone 失败
    const plain = JSON.parse(JSON.stringify(conn))
    const { id, name } = await api.sshConnect({
      ...plain,
      cols,
      rows,
    })

    sessions.value.push({
      id,
      name: name || `${conn.username}@${conn.host}`,
      type: 'remote',
      shell: `${conn.username}@${conn.host}`,
      connectionId: conn.id,
      createdAt: Date.now(),
    })

    activeSessionId.value = id

    // 主进程已 touchLastConnected,前端同步更新本地副本
    const idx = connections.value.findIndex(c => c.id === connectionId)
    if (idx !== -1) {
      connections.value[idx] = { ...connections.value[idx], lastConnectedAt: Date.now() }
    }

    return id
  }

  /** 把一条「主进程侧已经不在了」的远程会话重新拨起来，**占同一个标签位**。
   *
   *  为什么必须有它：会话池是主进程内存，主进程重启或自动重连放弃之后，池里就没有
   *  这条会话了；而渲染层的标签是活的（HMR / 长时间不关窗口时状态一直在）。于是界面
   *  呈现「一个看着还连着的标签 + 一枚没有目录的 chip」，真实状态却是背后一条连接都
   *  没有（2026-11-11 用户反馈：连接成功到 SSH 却不显示文件夹，实际那条会话在 10 小时
   *  前就被主进程移除了，4642 次 SESSION_NOT_FOUND 全来自它）。
   *
   *  走的是 ssh:connect 而不是 ssh:reconnect：后者按 id 找主进程的重连条目，而条目在
   *  「放弃」那一刻就被删了，对幽灵会话必然抛 SESSION_NOT_FOUND —— 只能整条重建。
   *  凭据仍由主进程从连接配置里取（渲染层不持明文）。
   */
  async function redial(sessionId: string, cols = 80, rows = 24): Promise<string> {
    const idx = sessions.value.findIndex(x => x.id === sessionId)
    if (idx === -1) throw new Error('会话已关闭')
    const s = sessions.value[idx]
    if (s.type !== 'remote' || !s.connectionId) throw new Error('只有远程会话可以重新连接')
    // 首页不一定已经拉过连接列表（会话从终端页带过来时连接配置可能还没加载）：
    // 空表下直接报「配置已被删除」是假诊断，先补一次再判。
    let conn = connections.value.find(c => c.id === s.connectionId)
    if (!conn) {
      await loadConnections().catch(() => { /* 下面统一报错 */ })
      conn = connections.value.find(c => c.id === s.connectionId)
    }
    if (!conn) throw new Error('连接配置已被删除，请重新添加服务器')
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')

    // 主进程侧本就没有它时这里是空操作；万一标记是误判、会话其实还活着，先拆掉再建，
    // 不留两条并存连接（同 id 迟到握手那套守卫解决不了「两个 id 两条连接」）。
    try { await api.sshDisconnect(sessionId) } catch { /* 主进程侧已无此会话 */ }

    // 剥离 Vue 响应式 Proxy，避免 structuredClone 失败（同 connectRemote）
    const plain = JSON.parse(JSON.stringify(conn))
    const { id, name } = await api.sshConnect({ ...plain, cols, rows })

    sessions.value[idx] = {
      ...s,
      id,
      name: name || s.name,
      unavailable: false,
      reconnect: null,
      reconnectGaveUp: false,
      createdAt: Date.now(),
    }

    // 按会话 id 键控的现场状态整体搬到新 id：换的是连接，不是用户的那条会话
    delete selectionBySession.value[sessionId]
    delete cwdBySession.value[sessionId]
    commandHistory.value = commandHistory.value.map(h => (h.sessionId === sessionId ? { ...h, sessionId: id } : h))
    commandChains.value = commandChains.value.map(c => (c.sessionId === sessionId ? { ...c, sessionId: id } : c))
    if (lastCommandBySession.value[sessionId] !== undefined) {
      lastCommandBySession.value[id] = lastCommandBySession.value[sessionId]
      delete lastCommandBySession.value[sessionId]
    }
    mruIds.value = mruIds.value.map(x => (x === sessionId ? id : x))
    if (activeSessionId.value === sessionId) activeSessionId.value = id

    // 立刻补一次目录：重拨完 chip 就该显示远端真实目录，而不是等着终端页 OSC 7
    void ensureTerminalCwd(id)
    return id
  }

  // ─── 关闭会话 ─────────────────────────────────
  async function closeSession(id: string): Promise<void> {
    const session = sessions.value.find(s => s.id === id)
    if (!session) return

    const api = window.electronAPI
    if (api) {
      try {
        if (session.type === 'local') {
          await api.ptyDestroy(id)
        } else {
          await api.sshDisconnect(id)
        }
      } catch (e) {
        // C1 修复：IPC 调用失败时仍继续清理本地 session，防止僵尸会话

      }
    }

    removeSession(id)
  }

  /** 从列表中移除（用于退出事件回调） */
  function removeSession(id: string) {
    const idx = sessions.value.findIndex(s => s.id === id)
    if (idx !== -1) {
      sessions.value.splice(idx, 1)
      if (activeSessionId.value === id) {
        activeSessionId.value = sessions.value.length > 0
          ? sessions.value[Math.min(idx, sessions.value.length - 1)].id
          : ''
      }
    }
    // 同步清理该会话的命令历史,防止长期使用后内存增长
    commandHistory.value = commandHistory.value.filter(h => h.sessionId !== id)
    // C37 修复：补充清理 commandChains 中该 sessionId 的条目（原实现只清了
    // commandHistory，命令链记录随会话删除残留，占满 MAX_COMMAND_CHAINS 前一直泄漏）
    commandChains.value = commandChains.value.filter(c => c.sessionId !== id)
    // 清理 lastCommandBySession 对应该会话的条目
    delete lastCommandBySession.value[id]
    // 清理终端现场上下文缓存（选中片段 / cwd）
    delete selectionBySession.value[id]
    delete cwdBySession.value[id]
    mruIds.value = mruIds.value.filter(x => x !== id)
  }

  function switchSession(id: string) {
    activeSessionId.value = id
    // MRU touch：任何激活路径（标签/快捷键/命令面板）都汇到这一个出口
    mruIds.value = [id, ...mruIds.value.filter(x => x !== id)]
    // 看见即清：激活的会话不再有「未读输出」
    const s = sessions.value.find(x => x.id === id)
    if (s) s.hasActivity = false
  }

  /** 标签拖拽排序：把 dragId 移到 targetId 当前位置（其余相对顺序不变） */
  function reorderSessions(dragId: string, targetId: string): void {
    if (dragId === targetId) return
    const from = sessions.value.findIndex(s => s.id === dragId)
    if (from === -1) return
    const [moved] = sessions.value.splice(from, 1)
    const to = sessions.value.findIndex(s => s.id === targetId)
    sessions.value.splice(to === -1 ? sessions.value.length : to, 0, moved)
  }

  // ─── 标签右键批量关闭 ──────────────────────
  async function handleTabContextAction(action: string, targetId: string): Promise<void> {
    const idx = sessions.value.findIndex(s => s.id === targetId)
    if (idx === -1 && action !== 'closeAll') return

    // 复制终端：基于目标会话创建一个相同类型的新会话并切到前台
    if (action === 'duplicate') {
      await duplicateSession(targetId)
      return
    }

    let toClose: string[] = []

    switch (action) {
      case 'closeCurrent':
        toClose = [targetId]
        break
      case 'closeLeft':
        toClose = sessions.value.slice(0, idx).map(s => s.id)
        break
      case 'closeRight':
        toClose = sessions.value.slice(idx + 1).map(s => s.id)
        break
      case 'closeAll':
        toClose = sessions.value.map(s => s.id)
        break
    }

    for (const id of toClose) {
      await closeSession(id)
    }

    if (sessions.value.length > 0 && !sessions.value.find(s => s.id === activeSessionId.value)) {
      activeSessionId.value = sessions.value[sessions.value.length - 1].id
    }
  }

  /** 复制一个终端会话：本地终端复用相同 shell，远程终端复用同一连接配置重连 */
  async function duplicateSession(targetId: string): Promise<void> {
    const target = sessions.value.find(s => s.id === targetId)
    if (!target) return
    try {
      if (target.type === 'local') {
        await createLocalTerminal(target.shell)
      } else if (target.type === 'remote' && target.connectionId) {
        // 复用原会话的列/行不可得（xterm 实例在组件内），用默认 80x24
        await connectRemote(target.connectionId, 80, 24)
      }
    } catch (e) {
      console.error('[sshStore] duplicateSession failed:', e)
      useToastStore().error(toFriendlyMessage(e, '复制终端失败'))
    }
  }

  // ─── 命令历史记录 ─────────────────────────────

  /** 记录一次用户提交的完整命令行 */
  function recordCommand(sessionId: string, command: string): void {
    if (!sessionId || !command) return
    const trimmed = command.trim()
    if (!trimmed) return

    const now = Date.now()
    const existing = commandHistory.value.find(
      h => h.sessionId === sessionId && h.command === trimmed,
    )
    if (existing) {
      existing.count++
      existing.lastUsedAt = now
    } else {
      commandHistory.value.push({
        sessionId,
        command: trimmed,
        count: 1,
        lastUsedAt: now,
      })
      // s5 修复：超出上限时淘汰最久未使用的条目，防止内存无界增长
      if (commandHistory.value.length > MAX_COMMAND_HISTORY) {
        commandHistory.value.sort((a, b) => a.lastUsedAt - b.lastUsedAt)
        commandHistory.value.splice(0, commandHistory.value.length - MAX_COMMAND_HISTORY)
      }
    }

    // 记录命令链：将上一个命令与当前命令关联
    const prevCmd = lastCommandBySession.value[sessionId]
    if (prevCmd && prevCmd !== trimmed) {
      recordCommandChain(sessionId, prevCmd, trimmed, now)
    }
    lastCommandBySession.value[sessionId] = trimmed
  }

  /**
   * 记录命令链（连续命令对）
   */
  function recordCommandChain(sessionId: string, prevCmd: string, nextCmd: string, now: number): void {
    const existing = commandChains.value.find(
      c => c.sessionId === sessionId && c.prevCmd === prevCmd && c.nextCmd === nextCmd,
    )
    if (existing) {
      existing.count++
      existing.lastUsedAt = now
    } else {
      commandChains.value.push({
        sessionId,
        prevCmd,
        nextCmd,
        count: 1,
        lastUsedAt: now,
      })
      // 超出上限时淘汰最久未使用的条目
      if (commandChains.value.length > MAX_COMMAND_CHAINS) {
        commandChains.value.sort((a, b) => a.lastUsedAt - b.lastUsedAt)
        commandChains.value.splice(0, commandChains.value.length - MAX_COMMAND_CHAINS)
      }
    }
  }

  /**
   * 查询命令链推荐：给定前一个命令，返回最可能的下一个命令
   */
  function queryCommandChainCandidates(sessionId: string, prevCmd: string, limit = 5): CommandChainEntry[] {
    if (!sessionId || !prevCmd) return []
    return commandChains.value
      .filter(c => c.sessionId === sessionId && c.prevCmd === prevCmd)
      .sort((a, b) => {
        if (b.count !== a.count) return b.count - a.count
        return b.lastUsedAt - a.lastUsedAt
      })
      .slice(0, limit)
  }

  /**
   * 获取某会话全部历史命令（去重后的完整命令列表，E 阶段 fuzzy 联想用）
   */
  function getSessionCommands(sessionId: string): string[] {
    return commandHistory.value
      .filter(h => h.sessionId === sessionId)
      .map(h => h.command)
  }

  /**
   * 获取某会话中一条命令的执行次数（E 阶段补全弹窗显示统计）
   */
  function getCommandCount(command: string, sessionId: string): number {
    const entry = commandHistory.value.find(
      h => h.sessionId === sessionId && h.command === command,
    )
    return entry?.count ?? 0
  }

  /**
   * 获取命令的时间衰减加权评分
   * 评分 = count * 时间衰减因子
   * 时间衰减因子：最近使用的命令得分更高，半衰期 7 天
   */
  function getCommandScore(command: string, sessionId: string): number {
    const entry = commandHistory.value.find(
      h => h.sessionId === sessionId && h.command === command,
    )
    if (!entry) return 0
    const now = Date.now()
    const ageDays = (now - entry.lastUsedAt) / (1000 * 60 * 60 * 24)
    const halfLifeDays = 7
    const decayFactor = Math.pow(0.5, ageDays / halfLifeDays)
    return entry.count * decayFactor
  }

  /**
   * 获取某会话上一次执行的命令
   */
  function getLastCommand(sessionId: string): string | undefined {
    return lastCommandBySession.value[sessionId]
  }

  /**
   * 获取某会话最近执行的命令列表（按时间正序）
   */
  function getRecentCommands(sessionId: string, limit = 10): string[] {
    return commandHistory.value
      .filter(h => h.sessionId === sessionId)
      .sort((a, b) => b.lastUsedAt - a.lastUsedAt)
      .slice(0, limit)
      .map(h => h.command)
      .reverse()
  }

  // ─── 终端现场上下文（「问 AI」采集） ─────────────────────────

  /** 终端组件在选中变化时登记当前选中片段（空选中时清除） */
  function setTerminalSelection(sessionId: string, text: string): void {
    if (!sessionId) return
    if (text) selectionBySession.value[sessionId] = text
    else delete selectionBySession.value[sessionId]
  }

  function getTerminalSelection(sessionId: string): string {
    return selectionBySession.value[sessionId] || ''
  }

  /** 终端组件定时同步 cwd（远程 OSC 7 / 本地 pty），无变化时跳过写入 */
  function setTerminalCwd(sessionId: string, cwd: string): void {
    if (!sessionId || !cwd) return
    if (cwdBySession.value[sessionId] === cwd) return
    cwdBySession.value[sessionId] = cwd
  }

  function getTerminalCwd(sessionId: string): string {
    return cwdBySession.value[sessionId] || ''
  }

  /** 某个会话「现在在哪个目录」：优先终端同步上来的实时 cwd（用户 cd 过之后它才是
   *  真相，OSC 7 / pty 轮询都写这张表），退到建会话时的初始 cwd。
   *  落点 chip 与会话列表的目录读数都走这里 —— 直接读 session.cwd 会把
   *  「建会话时没给目录」的会话一律读成空，进而被显示成「主目录」或一个编出来的名字。 */
  function sessionCwd(sessionId: string): string {
    const live = getTerminalCwd(sessionId)
    if (live) return live
    return sessions.value.find(s => s.id === sessionId)?.cwd || ''
  }

  /** 在途的远程 pwd 探测（同一会话并发只发一次） */
  const cwdProbing = new Set<string>()

  /** 探测到的「主进程侧没有这条会话」：把标签标记成不可用，界面据此说真话 */
  function _markSessionUnavailable(sessionId: string): void {
    const s = sessions.value.find(x => x.id === sessionId)
    if (s) s.unavailable = true
  }

  /** IPC 抛出的会话失效错误（主进程 SSHManager 抛 SESSION_NOT_FOUND） */
  function _isSessionGoneError(e: unknown): boolean {
    const msg = e instanceof Error ? e.message : String(e ?? '')
    return msg.includes('SESSION_NOT_FOUND') || msg.includes('会话不存在')
  }

  /** 远程会话「现在在哪个目录」的一次性兜底：OSC 7 只在终端页可见时每 5s 上报
   *  （useTerminalContext 的轮询），用户刚连上就切回首页、或远端 shell 不是
   *  bash/zsh（PROMPT_COMMAND / precmd 注入不生效）时，cwdBySession 永远是空的，
   *  首页那枚目录 chip 就只能写死一个 `~`。这里按代价从低到高分两级补：
   *
   *  1. `sshGetCwd`：主进程本来就在 stream 里持续解析 OSC 7（ssh-manager 的
   *     osc7Match），它与渲染层是否挂载无关 —— 这是最准也最便宜的一级。
   *  2. `sshExec pwd`：走 client.exec 独立通道，不往交互式 pty 写字节（不污染
   *     回显、不打断用户正在敲的命令）。代价是拿到的是**登录目录**而非交互式
   *     shell 里 cd 之后的目录，因此只在第 1 级为空时才用；一旦终端页的 OSC 7
   *     上报了真实值，会直接覆盖这里的兜底值。 */
  async function ensureTerminalCwd(sessionId: string): Promise<string> {
    if (!sessionId) return ''
    const known = sessionCwd(sessionId)
    if (known) return known
    const api = typeof window === 'undefined' ? undefined : window.electronAPI
    if (!api) return ''
    if (cwdProbing.has(sessionId)) return ''
    cwdProbing.add(sessionId)
    try {
      if (api.sshGetCwd) {
        const tracked = await api.sshGetCwd(sessionId).catch(() => '')
        if (tracked) {
          setTerminalCwd(sessionId, tracked)
          const s = sessions.value.find(x => x.id === sessionId)
          if (s) s.unavailable = false
          return tracked
        }
      }
      if (!api.sshExec) return ''
      const r = await api.sshExec(sessionId, 'pwd', 5)
      const cwd = (r?.stdout || '').trim()
      if (cwd) {
        setTerminalCwd(sessionId, cwd)
        const s = sessions.value.find(x => x.id === sessionId)
        if (s) s.unavailable = false
      }
      return cwd
    } catch (e) {
      // 会话已断开 / 通道被拒：静默降级，chip 保留占位读数。
      // SESSION_NOT_FOUND 是硬证据 —— 主进程侧已经没有这条会话了（标签还留着），
      // 必须置 unavailable，否则首页那枚 chip 会继续把死会话画成一个 `~` 的活目标。
      if (_isSessionGoneError(e)) _markSessionUnavailable(sessionId)
      return ''
    } finally {
      cwdProbing.delete(sessionId)
    }
  }

  /** 让远端交互式 shell 切到指定目录。
   *
   *  SSH 落点的「目录」不是一条配置，而是那条 shell 的 cwd —— 后端只认
   *  ssh_session_id，命令最终经 visible_terminal 写回同一个 pty（见 exec-target
   *  头部注释）。所以「选目录」在远端只能落成一句 cd：写进 pty 用户看得见，
   *  之后在终端里接着敲也在同一个目录，chip 与终端不会各说一套。
   *
   *  路径整段用单引号包住（内部单引号转义成 '\''），空格 / $ / 反引号都进不了 shell 语法。 */
  function cdRemote(sessionId: string, path: string): void {
    const api = typeof window === 'undefined' ? undefined : window.electronAPI
    const target = (path || '').trim()
    if (!sessionId || !target || !api?.sshWrite) return
    const quoted = `'${target.replace(/'/g, "'\\''")}'`
    api.sshWrite(sessionId, `cd -- ${quoted}\r`)
    // 乐观更新：OSC 7 要等终端页那 5s 轮询才回来，chip 不该为等它空一轮
    setTerminalCwd(sessionId, target)
  }

  // ─── 连接管理(走 IPC 持久化) ──────────────────
  /**
   * S4-03：主进程保存时若系统密钥环不可用，密码/passphrase 不会落盘（不落明文），
   * 而它把这件事随记录回传（credentialNotice）。这里负责让它对用户可见 —— 否则
   * 「保存成功」是假信号，用户要到下次连接认证失败才发现。
   */
  function warnIfCredentialDropped(rec: SSHConnection): void {
    if (rec.credentialNotice !== 'encryption-unavailable') return
    useToastStore().warning(
      '系统密钥环不可用，本次密码/passphrase 未保存 —— 下次连接需重新输入，或改用私钥认证',
    )
  }

  async function addConnection(conn: Omit<SSHConnection, 'id' | 'createdAt' | 'updatedAt' | 'tags' | 'note'> & { tags?: string[]; note?: string }): Promise<string> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')
    const now = Date.now()
    const record = {
      ...conn,
      id: '',
      tags: conn.tags || [],
      createdAt: now,
      updatedAt: now,
    } as SSHConnection
    const saved = await api.sshSaveConnection(record)
    connections.value.push(saved)
    warnIfCredentialDropped(saved)
    return saved.id
  }

  async function updateConnection(id: string, updates: Partial<Omit<SSHConnection, 'id' | 'createdAt'>>): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')
    const idx = connections.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = connections.value[idx]
    const merged = { ...existing, ...updates, id, updatedAt: Date.now() }
    // 审查R2修复：编辑连接切换认证方式后，buildPayload 里非活跃凭据字段是
    // undefined，经 JSON 序列化丢键 + 本处展开合并，旧记录中的凭据字段
    // （password/privateKeyPath/passphrase 及 has* 标志）会永久残留在保存记录里。
    // authType 变化时显式清除不属于新方式的凭据字段，让主进程 toDisk 不再落旧值。
    if (updates.authType && updates.authType !== existing.authType) {
      if (updates.authType !== 'password') delete merged.password
      if (updates.authType !== 'privateKey') {
        delete merged.privateKeyPath
        delete merged.passphrase
      }
      delete merged.hasPassword
      delete merged.hasPassphrase
    }
    const saved = await api.sshSaveConnection(merged)
    connections.value[idx] = saved
    warnIfCredentialDropped(saved)
  }

  async function removeConnection(id: string): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')
    await api.sshDeleteConnection(id)
    const idx = connections.value.findIndex(c => c.id === id)
    if (idx !== -1) connections.value.splice(idx, 1)
  }

  async function duplicateConnection(id: string): Promise<SSHConnection | undefined> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')
    const cloned = await api.sshDuplicateConnection(id)
    connections.value.push(cloned)
    return cloned
  }

  return {
    // state
    connections,
    sessions,
    activeSessionId,
    activeSession,
    mruIds,
    // methods
    isOnline,
    init,
    loadConnections,
    createLocalTerminal,
    connectRemote,
    redial,
    closeSession,
    removeSession,
    switchSession,
    markActivity,
    reorderSessions,
    handleTabContextAction,
    addConnection,
    updateConnection,
    removeConnection,
    duplicateConnection,
    // history
    recordCommand,
    getCommandScore,
    getCommandCount,
    getLastCommand,
    getRecentCommands,
    getSessionCommands,
    queryCommandChainCandidates,
    // 终端现场上下文（「问 AI」）
    setTerminalSelection,
    getTerminalSelection,
    setTerminalCwd,
    getTerminalCwd,
    sessionCwd,
    ensureTerminalCwd,
    cdRemote,
  }
})

// 同 exec-target：开发期热更本文件时把新增 action 补进已存在的实例，
// 否则运行中的窗口会拿旧 setup 定义的 store，新方法一律 undefined。
if (import.meta.hot) {
  import.meta.hot.accept(acceptHMRUpdate(useSSHStore, import.meta.hot))
}
