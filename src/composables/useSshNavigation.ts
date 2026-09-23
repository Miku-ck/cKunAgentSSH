// useSshNavigation.ts — 页面导航 + 终端生命周期 + Quick Start + 模态框 + 远程连接
import { ref, computed, watch, onUnmounted } from 'vue'
import { useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useServerMonitorStore } from '@/stores/server-monitor'
import { useSftpStore } from '@/stores/sftp'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'
import { extractSSHError, isRetryable } from '@/ssh-error-types'

/** 本地终端列表常量 */
const LOCAL_SHELLS = ['PowerShell', 'CMD', 'Git Bash', 'WSL']

/** 远程连接防抖间隔 */
const CONNECT_DEBOUNCE_MS = 2000

export function useSshNavigation(
  focusActiveTerminal: () => void,
  terminalRefs: Record<string, any>,
) {
  const store = useSSHStore()
  const sshAiStore = useSshAiStore()
  const sftpStore = useSftpStore()
  const monitorStore = useServerMonitorStore()
  const toast = useToastStore()

  // ─── 布局状态 ───
  const showHome = ref(true)
  const activeNavPage = ref<'home' | 'remote-mgr' | 'sftp' | 'audit' | 'schedules' | 'dashboard'>('home')
  const showModal = ref(false)
  const editingConnection = ref<SSHConnection | undefined>(undefined)

  const showCommandBar = computed(() =>
    store.activeSessionId !== '' && !showHome.value && activeNavPage.value === 'home'
  )

  // 延迟聚焦定时器 ID 列表，组件卸载时清理
  const focusTimers: ReturnType<typeof setTimeout>[] = []

  // ─── 页面切换 ───
  function goHome() {
    showHome.value = true
    activeNavPage.value = 'home'
  }

  function activateTerminal(sessionId: string) {
    showHome.value = false
    activeNavPage.value = 'home'
    store.switchSession(sessionId)
    // 等待 Vue 切换渲染后，让终端获取焦点，用户可立即输入
    // C28 修复：定时器触发后自摘除，focusTimers 数组不再无界增长
    const tid = setTimeout(() => {
      const i = focusTimers.indexOf(tid)
      if (i !== -1) focusTimers.splice(i, 1)
      focusActiveTerminal()
    }, 50)
    focusTimers.push(tid)
  }

  function toggleRemoteManager() {
    if (activeNavPage.value === 'remote-mgr') {
      goHome()
    } else {
      showHome.value = true
      activeNavPage.value = 'remote-mgr'
      // 切到独立页面时自动收起 AI 助手面板（点 AI 图标可重新展开）
      sshAiStore.closeAiPanel()
    }
  }

  async function toggleSftp() {
    if (activeNavPage.value === 'sftp') {
      goHome()
      return
    }
    showHome.value = true
    activeNavPage.value = 'sftp'
    // 切到独立页面时自动收起 AI 助手面板（点 AI 图标可重新展开）
    sshAiStore.closeAiPanel()

    // 已有 SFTP 连接时无需重复连接
    if (sftpStore.online) return

    // 检测当前活动终端是否为远程会话，是则复用其 SSH 连接打开 SFTP
    const activeId = store.activeSessionId
    if (!activeId) return
    const activeSession = store.sessions.find(s => s.id === activeId)
    if (!activeSession || activeSession.type !== 'remote') return

    try {
      await sftpStore.connectFromTerminal(
        activeSession.id,
        activeSession.name,
        activeSession.shell,
      )
      toast.success(`SFTP 已连接: ${activeSession.name}`)
    } catch (err: any) {
      toast.error(toFriendlyMessage(err, 'SFTP 连接失败，请手动连接'))
    }
  }

  function toggleAiPanel() {
    sshAiStore.toggleAiPanel()
  }

  function toggleAudit() {
    if (activeNavPage.value === 'audit') {
      goHome()
    } else {
      showHome.value = true
      activeNavPage.value = 'audit'
      // 切到独立页面时自动收起 AI 助手面板（点 AI 图标可重新展开）
      sshAiStore.closeAiPanel()
    }
  }

  function toggleSchedules() {
    if (activeNavPage.value === 'schedules') {
      goHome()
    } else {
      showHome.value = true
      activeNavPage.value = 'schedules'
      // 切到独立页面时自动收起 AI 助手面板（点 AI 图标可重新展开）
      sshAiStore.closeAiPanel()
    }
  }

  function toggleDashboard() {
    if (activeNavPage.value === 'dashboard') {
      goHome()
    } else {
      showHome.value = true
      activeNavPage.value = 'dashboard'
      // 切到独立页面时自动收起 AI 助手面板（点 AI 图标可重新展开）
      sshAiStore.closeAiPanel()
    }
  }

  // ─── Quick Start 搜索过滤 ───
  const qsSearch = ref('')

  const filteredShells = computed(() => {
    const q = qsSearch.value.toLowerCase().trim()
    if (!q) return LOCAL_SHELLS
    return LOCAL_SHELLS.filter(s => s.toLowerCase().includes(q))
  })

  const filteredConnections = computed(() => {
    const q = qsSearch.value.toLowerCase().trim()
    if (!q) return store.connections
    return store.connections.filter(c =>
      c.name.toLowerCase().includes(q) ||
      c.host.toLowerCase().includes(q) ||
      c.username.toLowerCase().includes(q)
    )
  })

  // ─── 本地终端 ───
  async function openLocalShell(shellName: string) {
    try {
      await store.createLocalTerminal(shellName)
      showHome.value = false
    } catch (err: any) {
      console.error('[openLocalShell] Failed:', shellName, err)
      toast.error(toFriendlyMessage(err, `${shellName} 打开失败`))
    }
  }

  // 本地终端“启动即退出”判定阈值（毫秒）：进程刚 spawn 成功不久就退出，
  // 基本可断定是启动阶段失败（如 WSL 无发行版、pwsh/powershell 路径不可执行），
  // 此时不该静默移除会话，而应提示用户。
  const LOCAL_STARTUP_CRASH_MS = 2500

  // 各本地 shell 启动失败时的友好排查提示
  const LOCAL_SHELL_HINTS: Record<string, string> = {
    PowerShell: '请确认已安装 Windows PowerShell / PowerShell 7 (pwsh)，且路径可执行。',
    WSL: '请确认已通过 wsl --install 安装 WSL，并已安装至少一个发行版（wsl -l -v 可查看）。',
    'Git Bash': '请确认已安装 Git，且 Git Bash 可正常启动。',
    CMD: '',
  }

  function handleLocalExit(id: string, exitCode: number) {
    // 启动即退出（非零退出码 + 存活时间极短）：多半是终端打开即失败，给出友好提示。
    // 某些失败会带 0 退出码（如 WSL 未装发行版时 wsl.exe 可能以 0 退出），此时用更严的
    // 1s 阈值兜底（正常 `exit` 关闭不会在 1 秒内完成打开→输入→退出全流程）。
    const session = store.sessions.find(s => s.id === id)
    const age = (session && session.createdAt) ? Date.now() - session.createdAt : Number.POSITIVE_INFINITY
    const crashThresholdMs = exitCode !== 0 ? LOCAL_STARTUP_CRASH_MS : 1000
    if (age < crashThresholdMs) {
      const hint = LOCAL_SHELL_HINTS[session?.shell || ''] || ''
      const shellName = session?.shell || '终端'
      toast.error(
        exitCode !== 0
          ? `「${shellName}」打开失败（进程已退出，退出码 ${exitCode}）。${hint}`
          : `「${shellName}」打开异常（进程启动后立即退出）。${hint}`
      )
    }
    store.removeSession(id)
  }

  // ─── 远程连接 ───
  // 修复：按连接 ID 分别记录最后连接时间，避免不同服务器之间的防抖互相影响
  const lastConnectAtById: Record<string, number> = {}
  // 新增连接 loading 状态：显示转圈圈提示
  const connecting = ref(false)
  const pendingConnId = ref('')

  /**
   * 复位连接 loading：仅当状态仍归属本次连接时才清。
   * 另一次连接（pendingConnId 已指向它）在途时，本次的 finally 不得越权清除，
   * 否则「A 结束」会让仍在连接的 B 失去转圈，B 所在行重新可点导致重复会话。
   */
  function releaseConnectLock(connId: string): void {
    if (pendingConnId.value && pendingConnId.value !== connId) return
    connecting.value = false
    pendingConnId.value = ''
  }

  async function connectRemote(conn: SSHConnection) {
    const now = Date.now()
    const lastConnectAt = lastConnectAtById[conn.id] || 0
    if (now - lastConnectAt < CONNECT_DEBOUNCE_MS) {
      toast.info('连接请求过于频繁，请稍候')
      // debounce 路径也要重置 connecting 状态，避免 loading 永远不消失
      releaseConnectLock(conn.id)
      return
    }
    lastConnectAtById[conn.id] = now
    // loading 由本函数统一持有：Quick Start 卡片与失败「重试」按钮过去直接调用本
    // 函数而不经过 handleRmConnect，既没有转圈反馈，finally 又会清掉同期
    // 另一次连接的状态
    connecting.value = true
    pendingConnId.value = conn.id

    try {
      await store.connectRemote(conn.id, 80, 24)
      // 与 activateTerminal 同口径：只收 showHome 不够，导航页是盖在 term-content
      // 之上的绝对定位浮层（SshView.vue .rm-page z-index:10）。从 Remote Manager
      // 列表点连接时浮层不消失，新终端已经在跑却被自己来的那一页挡住。
      showHome.value = false
      activeNavPage.value = 'home'
      // 等待 Vue 渲染 RemoteTerminal 组件后，让终端获取焦点
      // C28 修复：定时器触发后自摘除
      const tid = setTimeout(() => {
        const i = focusTimers.indexOf(tid)
        if (i !== -1) focusTimers.splice(i, 1)
        focusActiveTerminal()
      }, 100)
      focusTimers.push(tid)
    } catch (err: any) {
      console.error('[useSshNavigation] connectRemote failed:', err)
      // 失败即解除防抖：时间戳在发起前就写入了，而下方 toast 的「重试」按钮直接
      // 回调本函数 —— 秒级失败（拒绝连接/认证错误）时重试会撞进防抖窗口，
      // 只弹「连接请求过于频繁」，重试按钮形同虚设
      delete lastConnectAtById[conn.id]
      // v2 改进：结构化错误优先提取 SSHErrorTransport，带详情与重试按钮
      const sshTransport = extractSSHError(err)
      const friendlyMsg = toFriendlyMessage(err, 'SSH 连接失败')
      const details = sshTransport?.detail
        || (err instanceof Error && err.message !== friendlyMsg ? err.message : undefined)
      const actions = (sshTransport && isRetryable(sshTransport.code))
        ? [{ label: '重试', onClick: () => connectRemote(conn) }]
        : undefined
      toast.error(friendlyMsg, 8000, { details, actions })
    } finally {
      releaseConnectLock(conn.id)
    }
  }

  // RemoteManager 触发的连接
  function handleRmConnect(connId: string) {
    const conn = store.connections.find(c => c.id === connId)
    if (!conn) {
      console.error('[useSshNavigation] connection not found:', connId)
      toast.error('连接配置不存在')
      return
    }
    // connecting/pendingConnId 由 connectRemote 在首个 await 前同步置位，
    // 同一 tick 内即可渲染转圈
    void connectRemote(conn)
  }

  function handleRemoteClose(id: string) {
    store.removeSession(id)
  }

  // ─── 连接管理模态框 ───
  function editConnection(conn: SSHConnection | null) {
    editingConnection.value = conn ? { ...conn } : undefined
    showModal.value = true
  }

  function closeModal() {
    showModal.value = false
    editingConnection.value = undefined
  }

  async function handleSaved({ savedId, connectNow }: { savedId: string; connectNow: boolean }) {
    closeModal()
    if (connectNow) {
      const conn = store.connections.find(c => c.id === savedId)
      if (conn) await connectRemote(conn)
    }
  }

  // ─── 终端引用清理 + 自动回首页 ───
  // 当所有会话关闭时自动回到 Home，同时清理 terminalRefs 防止内存泄漏
  // M6 修复：watch id 数组而非 length，避免同一 tick 内关旧开新（长度不变）导致 terminalRefs 不清理
  watch(() => store.sessions.map(s => s.id), (ids) => {
    const activeIds = new Set(ids)
    for (const id of Object.keys(terminalRefs)) {
      if (!activeIds.has(id)) {
        delete terminalRefs[id]
      }
    }
  })
  // 独立 watch length 用于自动回首页（length 降为 0 时触发）
  watch(() => store.sessions.length, (len) => {
    if (len === 0) {
      goHome()
    }
  })

  // ─── 实时监控轮询生命周期（B 阶段） ───
  // 单一事实来源：monitoredTarget 同时依赖 sessions 与 activeSessionId，
  // 覆盖「切到远程会话 → start」「切到本地/会话全关 → stop」「关闭会话后校正目标」
  // 三种场景。监控数据在 store 层存活，视图切换不中断。
  const monitoredTarget = computed(() => {
    const remotes = store.sessions.filter(s => s.type === 'remote')
    if (!remotes.length) return ''
    const active = remotes.find(s => s.id === store.activeSessionId)
    return (active || remotes[0]).id
  })
  watch(monitoredTarget, (target) => {
    if (!target) {
      monitorStore.stop()
      return
    }
    if (monitorStore.polling && monitorStore.current?.sshId === target) return
    monitorStore.start(target)
  }, { immediate: true })

  // 组件卸载时清理延迟聚焦定时器
  onUnmounted(() => {
    focusTimers.forEach(t => clearTimeout(t))
  })

  return {
    showHome,
    activeNavPage,
    showAiPanel: sshAiStore.showAiPanel,
    showModal,
    editingConnection,
    showCommandBar,
    goHome,
    activateTerminal,
    toggleRemoteManager,
    toggleSftp,
    toggleAiPanel,
    toggleAudit,
    toggleSchedules,
    toggleDashboard,
    qsSearch,
    filteredShells,
    filteredConnections,
    openLocalShell,
    handleLocalExit,
    connectRemote,
    connecting,
    pendingConnId,
    handleRmConnect,
    handleRemoteClose,
    editConnection,
    closeModal,
    handleSaved,
  }
}
