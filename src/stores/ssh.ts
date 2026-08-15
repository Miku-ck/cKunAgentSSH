/**
 * SSH Store — 管理 SSH 连接配置和终端会话
 * 连接配置通过 IPC 持久化到主进程(safeStorage 加密),不再走 localStorage
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
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

  /** 命令历史最大条目数（超出后淘汰最久未使用的条目） */
  const MAX_COMMAND_HISTORY = 500

  /** 终端会话最大数量（防止无限制创建导致内存溢出） */
  const MAX_SESSIONS = 20

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
    api.onHostKeyNew(({ hostKey, fingerprint }) => {
      toast.warning(
        `首次连接到 ${hostKey}，已记录服务器指纹：${fingerprint.slice(0, 24)}...`,
        6000,
      )
    })
    api.onHostKeyMismatch(({ hostKey }) => {
      toast.error(
        `⚠️ ${hostKey} 服务器密钥变更！可能存在中间人攻击，连接已被拒绝。`,
        0, // 不自动关闭，强制用户确认
      )
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

    connecting.value = true
    try {
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
    } finally {
      connecting.value = false
    }
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
  }

  /**
   * 查询某会话的历史命令候选
   * 排序：使用频率(count)越高越靠前，最近使用(lastUsedAt)越近越靠前
   */
  function queryHistoryCandidates(
    sessionId: string,
    prefix: string,
    limit = 10,
  ): CommandHistoryEntry[] {
    if (!sessionId || !prefix) return []
    const lowerPrefix = prefix.toLowerCase()
    return commandHistory.value
      .filter(
        h =>
          h.sessionId === sessionId &&
          h.command.toLowerCase().startsWith(lowerPrefix),
      )
      .sort((a, b) => {
        if (b.count !== a.count) return b.count - a.count
        return b.lastUsedAt - a.lastUsedAt
      })
      .slice(0, limit)
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
    queryHistoryCandidates,
  }
})
