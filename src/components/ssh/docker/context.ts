/**
 * Docker 页的远程执行底座。
 *
 * 收口四件在页面壳、四个页签与三个弹窗之间必须完全一致的事：
 *   1. 目标主机选择（含从已保存连接手动连一台）；
 *   2. 命令执行 —— 读/写分预算，变更动作写审计链；
 *   3. 动作锁 —— 任一动作在途就锁住全部动作按钮，防连点重复下发；
 *   4. 陈旧回包守卫 —— 切宿主递增 epoch、同类并发重拉递增序号，
 *      回包只在「目标与序号都未变」时允许写回。
 *
 * 组件树里只 provide 一次（DockerView），页签与弹窗用 useDockerHost() 取同一份实例。
 * 「切宿主后旧回包不许写回」因此只有一处实现，不必各页签各抄一遍。
 */
import { computed, inject, provide, ref, watch, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import { toFriendlyMessage } from '@/api/client'
import { useSSHStore, type SSHConnection, type TerminalSession } from '@/stores/ssh'
import { useToastStore } from '@/stores/toast'
import {
  AUDIT_ACTION_DOCKER,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  writeRemoteAudit,
  type RemoteAuditConclusion,
} from '@/composables/remoteAudit'
import {
  describeThrownDockerError,
  dockerErrorFromResult,
  isOutputTruncated,
  type DockerError,
} from '@/stores/docker-core'

export const TABS = [
  { key: 'containers', label: '容器' },
  { key: 'images', label: '镜像' },
  { key: 'volumes', label: '卷' },
  { key: 'networks', label: '网络' },
] as const
export type TabKey = (typeof TABS)[number]['key']

// 取数与变更分预算：docker stop/restart/rm -f 自带 10s 的 SIGTERM→SIGKILL 宽限期，
// 卡住的容器（D 状态、挂死的后端卷）能拖到几十秒，rmi/prune 也要真删层数据。
// 原先 12 处变更动作共用 15s 一条预算，超时会把「远端其实正在成功」的命令报成失败，
// 界面随即停在旧状态 —— 用户对着一个其实已经停掉的容器再点一次停止。
export const READ_TIMEOUT_SEC = 15
export const ACTION_TIMEOUT_SEC = 60

/** 一次取数的上下文凭证：宿主 id + 宿主代次 + 该类资源的请求序号 */
export interface ListPin { id: string; epoch: number; seq: number }

/** 一类资源的列表取数句柄（四个页签同构，只是命令与解析不同） */
export interface DockerList<T> {
  rows: Ref<T[]>
  /** 尽力而为取数失败时如实上浮，不伪装成「该主机没有该资源」 */
  error: Ref<string | undefined>
  reload: () => Promise<void>
}

export type DockerAuditTarget = { targetId: string; environment: 'test' | 'prod' }

export interface DockerHost {
  sshStore: ReturnType<typeof useSSHStore>
  /** 已连接的远程会话（Docker 依附远程主机，本地终端不参与） */
  remoteSessions: ComputedRef<TerminalSession[]>
  targetId: Ref<string>
  targetName: ComputedRef<string>
  /** 目标主机串：优先用 connectionId 回查连接记录，回落到会话 shell（username@host） */
  targetHost: ComputedRef<string>
  targetSession: ComputedRef<TerminalSession | null>
  showConnPicker: Ref<boolean>
  connecting: Ref<boolean>
  pendingConnId: Ref<string>
  handleConnect: (conn: SSHConnection) => Promise<void>
  disconnectTarget: () => void
  busy: Ref<string>
  withBusyKey: (key: string, fn: () => Promise<void>) => Promise<void>
  withBusy: (target: { id: string }, fn: () => Promise<void>) => Promise<void>
  /** 任一取数命中 1MB 截断（列表可能缺行，状态栏必须明示） */
  truncated: Ref<boolean>
  /** 其中的 docker ps 单独记账：镜像「引用」列只取决于容器列表是否完整 */
  psTruncated: Ref<boolean>
  tab: Ref<TabKey>
  lastSync: ComputedRef<string>
  markSynced: (kind: TabKey | TabKey[], label: string) => void
  dockerExec: (cmd: string, id?: string, timeoutSec?: number, auditAction?: string) => Promise<string>
  /** 变更类动作入口：只有这一类需要放宽预算，取数路径继续用默认值 */
  actionExec: (cmd: string) => Promise<string>
  dockerAuditTarget: (id: string) => DockerAuditTarget
  writeDockerAudit: (
    target: DockerAuditTarget & { action: string },
    cmd: string,
    conclusion: RemoteAuditConclusion,
    result: string,
  ) => void
  toastErr: (e: unknown) => void
  /** 宿主切换时作废所有在途回包 */
  bumpEpoch: () => void
  pinList: (kind: TabKey, id: string) => ListPin
  listFresh: (kind: TabKey, p: ListPin) => boolean
  createList: <T>(spec: ListSpec<T>) => DockerList<T>
}

interface ListSpec<T> {
  kind: TabKey
  cmd: string
  parse: (raw: string) => T[]
  /** 状态栏「已同步」后缀里点明实际取数的命令 */
  label: string
  /** 成功后的追加富集（宿主的 pin 一并传下去，由它自行校验新鲜度） */
  after?: (id: string, pin: ListPin) => void
}

export const errText = (e: unknown): string => (e as DockerError)?.message || String(e)

const DOCKER_HOST_KEY: InjectionKey<DockerHost> = Symbol('dockerHost')

/**
 * 在 DockerView 里调用一次：构造底座并向下 provide。
 * 必须在 DockerView 的 setup 同步段调用 —— 目标主机切换要立即触发首轮取数。
 */
export function provideDockerHost(): DockerHost {
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
  // 动作统一执行器：in-flight 期间锁定按钮（docker stop 有 10s 宽限期），防连点重复下发
  // 容器/镜像传 id；卷传 'v:'+name；网络传 'n:'+name；prune 传 'prune:volumes' 等。只要唯一即可。
  async function withBusyKey(key: string, fn: () => Promise<void>): Promise<void> {
    if (busy.value) return
    busy.value = key
    try { await fn() } finally { busy.value = '' }
  }
  async function withBusy(target: { id: string }, fn: () => Promise<void>): Promise<void> {
    return withBusyKey(target.id, fn)
  }

  // 单次 exec 输出上限 1MB：命中标记时列表可能缺行，状态栏必须明示，别让用户
  // 把「只看到前 N 个容器」当成主机真实状态。
  const truncated = ref(false)
  const psTruncated = ref(false)

  // 同步时间按资源分槽：共用一个字段时，刷完镜像再切到卷页仍显示「docker images · 12:00」，
  // 读起来像卷列表也是那一刻取的。状态栏只展示当前 tab 那一格。
  const tab = ref<TabKey>('containers')
  const syncedAt = ref<Record<TabKey, string>>({ containers: '', images: '', volumes: '', networks: '' })
  /** 一次可标多类：loadAll 四类同取时四格写同一个时刻，"已同步" 不再分先后 */
  function markSynced(kind: TabKey | TabKey[], label: string): void {
    const at = `${label} · ${new Date().toLocaleTimeString()}`
    const next = { ...syncedAt.value }
    for (const k of Array.isArray(kind) ? kind : [kind]) next[k] = at
    syncedAt.value = next
  }
  const lastSync = computed(() => syncedAt.value[tab.value])

  // 跨宿主竞态守卫：一轮 loadAll 内串行 + 并行共 4 个 await（单条硬超时 15s），用户中途切
  // 主机若照常写入，会把 A 的容器盖进已切到 B 的页面（混合宿主数据是运维场景的危险误导）。
  // 切宿主时递增 epoch；同一资源的并发重拉再各自递增序号，以最后发起者为准。
  let targetEpoch = 0
  const listSeq: Record<TabKey, number> = { containers: 0, images: 0, volumes: 0, networks: 0 }
  function bumpEpoch(): void { targetEpoch++ }
  function pinList(kind: TabKey, id: string): ListPin {
    return { id, epoch: targetEpoch, seq: ++listSeq[kind] }
  }
  function listFresh(kind: TabKey, p: ListPin): boolean {
    return p.epoch === targetEpoch && p.id === targetId.value && p.seq === listSeq[kind]
  }

  /** 审计目标在发起时定格：动作飞行途中切宿主，仍记实际执行的那台 */
  function dockerAuditTarget(id: string): DockerAuditTarget {
    const s = remoteSessions.value.find(x => x.id === id)
    const conn = s?.connectionId
      ? sshStore.connections.find(c => c.id === s.connectionId)
      : undefined
    return {
      targetId: conn ? `${conn.name} (${conn.username}@${conn.host})` : (s?.shell || s?.name || id),
      environment: auditEnvironmentFor(s, sshStore.connections),
    }
  }

  function writeDockerAudit(
    target: DockerAuditTarget & { action: string },
    cmd: string,
    conclusion: RemoteAuditConclusion,
    result: string,
  ): void {
    void writeRemoteAudit(buildRemoteAuditRecord({
      action: target.action,
      conclusion,
      targetId: target.targetId,
      environment: target.environment,
      command: cmd,
      result,
    })).then(ok => {
      if (!ok) toast.warning('审计记录写入失败：本次 Docker 操作未进入审计链')
    })
  }

  async function dockerExec(
    cmd: string, id = targetId.value, timeoutSec = READ_TIMEOUT_SEC, auditAction?: string,
  ): Promise<string> {
    const api = window.electronAPI
    if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as DockerError
    // 只有变更动作传 auditAction；读路径（列表/日志/inspect）不写审计，
    // 否则一次列表刷新就会往审计链里灌十几条噪声。
    const auditCtx = auditAction
      ? { action: auditAction, ...dockerAuditTarget(id) }
      : null
    const started = Date.now()
    let res
    try {
      res = await api.sshExec(id, cmd, timeoutSec)
    } catch (err) {
      const dErr = describeThrownDockerError(err)
      if (auditCtx) {
        const outcome = auditOutcomeForDispatchError(err, dErr.message, timeoutSec)
        writeDockerAudit(auditCtx, cmd, outcome.conclusion, outcome.result)
      }
      throw dErr
    }
    if (isOutputTruncated(res.stdout, res.stderr)) truncated.value = true
    const durationMs = Date.now() - started
    const dErr = dockerErrorFromResult(res.stderr, res.exitCode, res.stdout)
    if (auditCtx) {
      // sshExec 回包 = 命令已下发；docker 返回非零（容器已不存在等）仍记 executed，
      // 错误码附在 result 里供检索 —— 审计结论不是成败统计。
      const base = auditResultForExit(res.exitCode, durationMs)
      writeDockerAudit(
        auditCtx, cmd, 'executed',
        dErr ? `${base} docker_error=${dErr.code}` : base,
      )
    }
    if (dErr) throw dErr
    return res.stdout
  }

  function actionExec(cmd: string): Promise<string> {
    return dockerExec(cmd, targetId.value, ACTION_TIMEOUT_SEC, AUDIT_ACTION_DOCKER)
  }

  function toastErr(e: unknown): void {
    const d = e as DockerError
    toast.error(d?.message || String(e), 6000, d?.hint ? { details: d.hint } : undefined)
  }

  /**
   * 四类资源的取数同构：pin 住发起时的宿主与序号 → 取数 → 只在仍新鲜时写回，
   * 失败（同 pin 下）如实上浮到 error 并把列表清空，绝不伪装成「该主机没有该资源」。
   */
  function createList<T>(spec: ListSpec<T>): DockerList<T> {
    const rows = ref<T[]>([]) as Ref<T[]>
    const error = ref<string | undefined>(undefined)
    async function reload(): Promise<void> {
      const id = targetId.value
      if (!id) return
      const pin = pinList(spec.kind, id)
      try {
        const next = spec.parse(await dockerExec(spec.cmd, id))
        if (!listFresh(spec.kind, pin)) return
        rows.value = next
        error.value = undefined
        markSynced(spec.kind, spec.label)
        spec.after?.(id, pin)
      } catch (e) {
        if (!listFresh(spec.kind, pin)) return
        error.value = errText(e)
        rows.value = []
      }
    }
    return { rows, error, reload }
  }

  const host: DockerHost = {
    sshStore,
    remoteSessions,
    targetId,
    targetName,
    targetHost,
    targetSession,
    showConnPicker,
    connecting,
    pendingConnId,
    handleConnect,
    disconnectTarget,
    busy,
    withBusyKey,
    withBusy,
    truncated,
    psTruncated,
    tab,
    lastSync,
    markSynced,
    dockerExec,
    actionExec,
    dockerAuditTarget,
    writeDockerAudit,
    toastErr,
    bumpEpoch,
    pinList,
    listFresh,
    createList,
  }
  provide(DOCKER_HOST_KEY, host)
  return host
}

/** 页签与弹窗取同一份底座实例；脱离 DockerView 使用时是编码错误，直接抛。 */
export function useDockerHost(): DockerHost {
  const host = inject(DOCKER_HOST_KEY, null)
  if (!host) throw new Error('useDockerHost() 必须在 DockerView 的组件树内调用')
  return host
}
