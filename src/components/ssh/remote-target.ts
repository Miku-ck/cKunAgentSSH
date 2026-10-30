/**
 * 远程目标底座 —— 独立功能页里与「跑什么命令」完全无关的那一半。
 *
 * 只收三件事：
 *   1. 目标主机的选择（默认落在活动远程会话，并允许从已保存连接手动连一台）；
 *   2. 断开当前目标；
 *   3. 动作锁 —— 任一动作在途就锁住全部动作按钮，防连点重复下发。
 *
 * 取数、命令拼接、解析、错误分类、审计都不在这里：那些每页各有一套语义，
 * 共用只会把差异藏进参数里。这层的存在只为了别把上面三件事手抄第二遍 ——
 * 「用 connectRemote 返回的会话 id」这类坑抄一次漏一处，表现是目标指回旧会话。
 *
 * 必须在页面的 setup 同步段调用：内部的 watch 靠组件作用域回收，
 * 在组件外调用会留下一个永不释放的远程会话监听器。
 */
import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
import { toFriendlyMessage } from '@/api/client'
import { useSSHStore, type SSHConnection, type TerminalSession } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'

export interface RemoteTarget {
  sshStore: ReturnType<typeof useSSHStore>
  /** 已连接的远程会话（这类页面依附远程主机，本地终端不参与） */
  remoteSessions: ComputedRef<TerminalSession[]>
  targetId: Ref<string>
  targetName: ComputedRef<string>
  targetSession: ComputedRef<TerminalSession | null>
  /** 目标主机串：优先用 connectionId 回查连接记录，回落到会话 shell（username@host） */
  targetHost: ComputedRef<string>
  showConnPicker: Ref<boolean>
  connecting: Ref<boolean>
  pendingConnId: Ref<string>
  handleConnect: (conn: SSHConnection) => Promise<void>
  disconnectTarget: () => void
  busy: Ref<string>
  withBusyKey: (key: string, fn: () => Promise<void>) => Promise<void>
  withBusy: (target: { id: string }, fn: () => Promise<void>) => Promise<void>
}

export function useRemoteTarget(): RemoteTarget {
  const sshStore = useSSHStore()
  const toast = useToastStore()

  const remoteSessions = computed(() => sshStore.sessions.filter(s => s.type === 'remote'))
  const targetId = ref('')
  // 默认落在活动远程会话；活动会话切换或远程列表变化时校正目标
  watch(remoteSessions, (list) => {
    if (!list.some(s => s.id === targetId.value)) {
      const active = list.find(s => s.id === sshStore.activeSessionId)
      targetId.value = (active || list[0])?.id || ''
    }
  }, { immediate: true })
  const targetName = computed(() => remoteSessions.value.find(s => s.id === targetId.value)?.name || '')
  const targetSession = computed(() => remoteSessions.value.find(s => s.id === targetId.value) || null)
  const targetHost = computed(() => {
    const s = targetSession.value
    if (!s) return ''
    const conn = sshStore.connections.find(c => c.id === s.connectionId)
    return conn ? `${conn.username}@${conn.host}` : s.shell
  })

  // ── 手动连接（仿监控仪表盘：不预设必须有会话，可从已保存连接直接连） ──
  const showConnPicker = ref(false)
  const connecting = ref(false)
  const pendingConnId = ref('')

  async function handleConnect(conn: SSHConnection): Promise<void> {
    if (connecting.value) return
    connecting.value = true
    pendingConnId.value = conn.id
    try {
      // 用 connectRemote 返回的那条会话 id，不按 connectionId 回查：同一主机已有会话时
      // find 命中的是**最早那条**，目标会指回旧会话，而新开这条没人认领（多占一个远端
      // PTY，还挂在终端标签列表里）。
      targetId.value = await sshStore.connectRemote(conn.id, 80, 24)
      showConnPicker.value = false
      toast.success(`已连接 ${conn.name}`)
    } catch (err) {
      // S0-24 口径：握手/认证失败抛出的是 SSHError transport（或 Electron 信封），
      // 原样 err.message 进 toast 等于把 getaddrinfo、Cannot parse privateKey 端给用户。
      toast.error(toFriendlyMessage(err, 'SSH 连接失败'))
    } finally {
      connecting.value = false
      pendingConnId.value = ''
    }
  }

  function disconnectTarget(): void {
    const s = targetSession.value
    if (!s) return
    void sshStore.closeSession(s.id)
    toast.info(`已断开 ${s.name}`)
  }

  const busy = ref('')
  // 动作统一执行器：in-flight 期间锁定按钮。调用方传什么 key 只要唯一即可
  // （Docker 页用容器 id，卷传 'v:'+name，prune 传 'prune:volumes'）。
  async function withBusyKey(key: string, fn: () => Promise<void>): Promise<void> {
    if (busy.value) return
    busy.value = key
    try { await fn() } finally { busy.value = '' }
  }
  async function withBusy(target: { id: string }, fn: () => Promise<void>): Promise<void> {
    return withBusyKey(target.id, fn)
  }

  return {
    sshStore,
    remoteSessions,
    targetId,
    targetName,
    targetSession,
    targetHost,
    showConnPicker,
    connecting,
    pendingConnId,
    handleConnect,
    disconnectTarget,
    busy,
    withBusyKey,
    withBusy,
  }
}
