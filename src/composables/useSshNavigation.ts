// useSshNavigation.ts — 页面导航 + 终端生命周期 + Quick Start + 模态框 + 远程连接
import { ref, computed, watch } from 'vue'
import { useSSHStore, type SSHConnection } from '@/stores/ssh'
import { useSshAiStore } from '@/stores/ssh-ai'
import { useSftpStore } from '@/stores/sftp'
import { useToastStore } from '@/stores/toast'
import { toFriendlyMessage } from '@/api/client'

/** 本地终端列表常量 */
const LOCAL_SHELLS = ['PowerShell', 'Git Bash', 'CMD', 'WSL']

/** 远程连接防抖间隔 */
const CONNECT_DEBOUNCE_MS = 2000

export function useSshNavigation(
  focusActiveTerminal: () => void,
  terminalRefs: Record<string, any>,
) {
  const store = useSSHStore()
  const sshAiStore = useSshAiStore()
  const sftpStore = useSftpStore()
  const toast = useToastStore()

  // ─── 布局状态 ───
  const showHome = ref(true)
  const activeNavPage = ref<'home' | 'remote-mgr' | 'sftp'>('home')
  const showModal = ref(false)
  const editingConnection = ref<SSHConnection | undefined>(undefined)

  const showCommandBar = computed(() =>
    store.activeSessionId !== '' && !showHome.value && activeNavPage.value === 'home'
  )

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
    setTimeout(() => focusActiveTerminal(), 50)
  }

  function toggleRemoteManager() {
    if (activeNavPage.value === 'remote-mgr') {
      goHome()
    } else {
      showHome.value = true
      activeNavPage.value = 'remote-mgr'
    }
  }

  async function toggleSftp() {
    if (activeNavPage.value === 'sftp') {
      goHome()
      return
    }
    showHome.value = true
    activeNavPage.value = 'sftp'

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

  function handleLocalExit(id: string, _exitCode: number) {
    store.removeSession(id)
  }

  // ─── 远程连接 ───
  let lastConnectAt = 0
  // 新增连接 loading 状态：显示转圈圈提示
  const connecting = ref(false)
  const pendingConnId = ref('')

  async function connectRemote(conn: SSHConnection) {
    const now = Date.now()
    if (now - lastConnectAt < CONNECT_DEBOUNCE_MS) {
      console.log('[useSshNavigation] connectRemote: debounced, skipping')
      return
    }
    lastConnectAt = now
    console.log('[useSshNavigation] connectRemote: connecting to', conn.name, conn.id)

    try {
      await store.connectRemote(conn.id, 80, 24)
      console.log('[useSshNavigation] connectRemote: connected!', conn.id)
      showHome.value = false
      // 等待 Vue 渲染 RemoteTerminal 组件后，让终端获取焦点
      setTimeout(() => focusActiveTerminal(), 100)
    } catch (err: any) {
      console.error('[useSshNavigation] connectRemote failed:', err)
      toast.error(toFriendlyMessage(err, 'SSH 连接失败'))
    } finally {
      connecting.value = false
      pendingConnId.value = ''
      console.log('[useSshNavigation] connectRemote: cleanup', conn.id)
    }
  }

  // RemoteManager 触发的连接
  function handleRmConnect(connId: string) {
    console.log('[useSshNavigation] handleRmConnect called:', connId)
    // 立即显示 loading（点击后马上转圈圈，给用户即时反馈）
    connecting.value = true
    pendingConnId.value = connId

    const conn = store.connections.find(c => c.id === connId)
    if (conn) {
      connectRemote(conn)
    } else {
      console.error('[useSshNavigation] connection not found:', connId)
      toast.error('连接配置不存在')
      connecting.value = false
      pendingConnId.value = ''
    }
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

  return {
    showHome,
    activeNavPage,
    showModal,
    editingConnection,
    showCommandBar,
    goHome,
    activateTerminal,
    toggleRemoteManager,
    toggleSftp,
    toggleAiPanel,
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
