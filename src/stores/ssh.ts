/**
 * SSH Store — 管理 SSH 连接配置和终端会话
 * 连接配置通过 IPC 持久化到主进程(safeStorage 加密),不再走 localStorage
 */
import { defineStore } from 'pinia'
import { ref, computed, onScopeDispose } from 'vue'
import { useToastStore } from './toast'

export type SSHConnection = SSHConnectionRecord

export interface TerminalSession {
  id: string
  name: string
  type: 'local' | 'remote'
  shell: string           // 本地: shell名, 远程: username@host
  connectionId?: string   // 远程连接的 ID
}

export interface CommandHistoryEntry {
  sessionId: string
  command: string
  count: number
  lastUsedAt: number
}

/** 命令链条目：记录连续命令对 (prevCmd -> nextCmd) */
export interface CommandChainEntry {
  sessionId: string
  prevCmd: string
  nextCmd: string
  count: number
  lastUsedAt: number
}

const LEGACY_KEY = 'ckun-ssh-connections'

export const useSSHStore = defineStore('ssh', () => {
  // ─── State ────────────────────────────────────
  const connections = ref<SSHConnection[]>([])
  const sessions = ref<TerminalSession[]>([])
  const activeSessionId = ref('')
  const connecting = ref(false)
  const loaded = ref(false)

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
  const localSessions = computed(() => sessions.value.filter(s => s.type === 'local'))
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
    // Pinia scope 销毁时清理监听器（HMR 场景）
    onScopeDispose(() => {
      if (typeof offNew === 'function') offNew()
      if (typeof offMismatch === 'function') offMismatch()
    })
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
      console.debug('[ssh] 迁移旧版连接配置失败:', e)
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

  async function createLocalTerminal(shell: string): Promise<string> {
    // s4 修复：会话数量上限保护
    if (sessions.value.length >= MAX_SESSIONS) {
      throw new Error(`终端会话数量已达上限（${MAX_SESSIONS}），请先关闭部分会话`)
    }
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')

    const shellPath = await resolveShellPath(shell)
    const args = getShellArgs(shell)
    const result = await api.ptyCreate({ shell: shellPath, args })

    sessions.value.push({
      id: result.id,
      name: shell,
      type: 'local',
      shell,
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
    })

    activeSessionId.value = id

    // 主进程已 touchLastConnected,前端同步更新本地副本
    const idx = connections.value.findIndex(c => c.id === connectionId)
    if (idx !== -1) {
      connections.value[idx] = { ...connections.value[idx], lastConnectedAt: Date.now() }
    }

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
        console.debug('[ssh] closeSession IPC 失败:', e)
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
  }

  function switchSession(id: string) {
    activeSessionId.value = id
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

  // ─── 连接管理(走 IPC 持久化) ──────────────────
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
    return saved.id
  }

  async function updateConnection(id: string, updates: Partial<Omit<SSHConnection, 'id' | 'createdAt'>>): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('Electron API not available')
    const idx = connections.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const merged = { ...connections.value[idx], ...updates, id, updatedAt: Date.now() }
    const saved = await api.sshSaveConnection(merged)
    connections.value[idx] = saved
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
    localSessions,
    connecting,
    loaded,
    // computed helpers
    onlineConnectionIds,
    // methods
    isOnline,
    init,
    loadConnections,
    createLocalTerminal,
    connectRemote,
    closeSession,
    removeSession,
    switchSession,
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
  }
})
