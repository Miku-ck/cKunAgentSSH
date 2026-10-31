/**
 * K8s 页的远程执行底座。
 *
 * 与 docker/context.ts 同构，但只共用真正同构的那一半：目标主机选择与动作锁已经
 * 收在 ../remote-target.ts（useRemoteTarget），这里的取数、审计、陈旧回包守卫是 K8s
 * 自己的一套 —— 边界是刻意画的：把 exec/审计也抽成公共层的话，K8s 的读数差异就得
 * 靠参数塞进公共层，公共层的形状会被一个调用方牵着走。
 *
 * 与 Docker 页的一处不同：这里没有 namespace 这根轴。列表一律 -A，命名空间是表里的
 * 一列、收窄靠搜索框，所以底座里既没有 ns 状态、也不用为「切 ns 作废在途回包」做事。
 */
import { computed, inject, provide, ref, type ComputedRef, type InjectionKey, type Ref } from 'vue'
import { useToastStore } from '@/stores/toast'
import {
  AUDIT_ACTION_K8S,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  writeRemoteAudit,
  type RemoteAuditConclusion,
} from '@/utils/remote-audit'
import {
  CONTEXT_CMD,
  ENV_PROBE_CMD,
  describeThrownK8sError,
  k8sErrorFromResult,
  parseEnvProbe,
  toK8sError,
  withExecDeadline,
  type K8sEnvProbe,
  type K8sError,
} from '@/stores/k8s-core'
import { isOutputTruncated } from '../../../../shared/exec-outcome-core'
import { useRemoteTarget, type RemoteTarget } from '../remote-target'

export const TABS = [
  { key: 'nodes', label: '节点' },
  { key: 'workloads', label: '负载' },
  { key: 'pods', label: 'Pod' },
  { key: 'services', label: '服务' },
  { key: 'events', label: '事件' },
] as const
export type TabKey = (typeof TABS)[number]['key']

/** 一类需要 pin 与同步时间记账的取数：就是五个分段 */
export type ListKind = TabKey
export const LIST_KINDS: ListKind[] = TABS.map(t => t.key)

// 与 Docker 页同一套预算：取数 15s、变更 60s。kubectl 侧的 --request-timeout=12s
// 比 15s 短，为的是让 kubectl 自己先超时（留下可读的 stderr），而不是让 ssh exec
// 超时（exitCode 为 null，什么都不知道）。
export const READ_TIMEOUT_SEC = 15
export const ACTION_TIMEOUT_SEC = 60

/** 一次取数的上下文凭证：宿主 id + 该类资源的请求序号 */
export interface ListPin { id: string; seq: number }

export interface K8sList<T> {
  rows: Ref<T[]>
  /**
   * 尽力而为取数失败时如实上浮，不伪装成「这个集群没有这类资源」。
   * 存整个 K8sError 而不是文本：页面要靠 code 判断这是「整机供不了 kubectl」
   * （页顶说一次）还是「只有这一类读不到」（列表自己说）。
   */
  error: Ref<K8sError | undefined>
  reload: () => Promise<void>
}

export type K8sAuditTarget = { targetId: string; environment: 'test' | 'prod' }

interface ListSpec<T> {
  kind: ListKind
  /** 命令随界面状态变（事件页的「含 Normal」）：在发起时取，不在构造时取 */
  cmd: () => string
  parse: (raw: string) => T[]
  /** 状态栏「已同步」后缀里点明实际取数的命令 */
  label: string
  /** 成功后的追加富集（宿主与 pin 一并传下去，由富集腿自行校验新鲜度） */
  after?: (id: string, pin: ListPin) => void
}

export interface K8sHost extends RemoteTarget {
  /** 任一取数命中 1MB 截断（列表可能缺行，状态栏必须明示） */
  truncated: Ref<boolean>
  tab: Ref<TabKey>
  /** 事件页是否连同 Normal 一起取（默认只看 Warning） */
  showNormalEvents: Ref<boolean>
  /** 只展示不切换：多 context 管理是另一套凭据模型，不在这一页 */
  contextName: Ref<string>
  /** 未检测到时抓取的执行现场；正常时为空 */
  envProbe: Ref<K8sEnvProbe | null>
  lastSync: ComputedRef<string>
  k8sExec: (cmd: string, id?: string, timeoutSec?: number, auditAction?: string) => Promise<string>
  actionExec: (cmd: string) => Promise<string>
  toastErr: (e: unknown) => void
  listFresh: (kind: ListKind, p: ListPin) => boolean
  createList: <T>(spec: ListSpec<T>) => K8sList<T>
}

const K8S_HOST_KEY: InjectionKey<K8sHost> = Symbol('k8sHost')

/**
 * 在 K8sView 里调用一次：构造底座并向下 provide。
 * 必须在 setup 同步段调用 —— 目标主机切换要立即触发首轮取数。
 */
export function provideK8sHost(): K8sHost {
  const remoteTarget = useRemoteTarget()
  const toast = useToastStore()
  const { sshStore, remoteSessions, targetId } = remoteTarget

  const truncated = ref(false)
  const tab = ref<TabKey>('workloads')
  const showNormalEvents = ref(false)
  const contextName = ref('')
  const envProbe = ref<K8sEnvProbe | null>(null)

  // 同步时间按资源分槽：共用一个字段时，刷完节点再切到事件页仍显示「kubectl get
  // nodes · 12:00」，读起来像事件也是那一刻取的。状态栏只展示当前 tab 那一格。
  const syncedAt = ref<Record<ListKind, string>>(
    Object.fromEntries(LIST_KINDS.map(k => [k, ''])) as Record<ListKind, string>,
  )
  function markSynced(kind: ListKind | ListKind[], label: string): void {
    const at = `${label} · ${new Date().toLocaleTimeString()}`
    const next = { ...syncedAt.value }
    for (const k of Array.isArray(kind) ? kind : [kind]) next[k] = at
    syncedAt.value = next
  }
  const lastSync = computed(() => syncedAt.value[tab.value])

  // 跨宿主竞态守卫：一轮刷新并发 5 个 await（单条硬超时 15s），用户中途切主机若照常
  // 写入，会把 A 集群的 Pod 盖进已切到 B 的页面 —— 运维场景里这种混合读数比报错危险。
  // 每条列表各自递增序号：id 挡住「回包来自别的宿主」，seq 挡住「同一宿主上这一类资源
  // 已被更晚的一轮取数取代」（A→B→A 来回切时 id 会重新相等，只有 seq 能分辨）。
  const listSeq = Object.fromEntries(LIST_KINDS.map(k => [k, 0])) as Record<ListKind, number>
  function pinList(kind: ListKind, id: string): ListPin {
    return { id, seq: ++listSeq[kind] }
  }
  function listFresh(kind: ListKind, p: ListPin): boolean {
    return p.id === targetId.value && p.seq === listSeq[kind]
  }

  /** 审计目标在发起时定格：动作飞行途中切宿主，仍记实际执行的那台 */
  function k8sAuditTarget(id: string): K8sAuditTarget {
    const s = remoteSessions.value.find(x => x.id === id)
    const conn = s?.connectionId
      ? sshStore.connections.find(c => c.id === s.connectionId)
      : undefined
    return {
      targetId: conn ? `${conn.name} (${conn.username}@${conn.host})` : (s?.shell || s?.name || id),
      environment: auditEnvironmentFor(s, sshStore.connections),
    }
  }

  function writeK8sAudit(
    audit: K8sAuditTarget & { action: string },
    cmd: string,
    conclusion: RemoteAuditConclusion,
    result: string,
  ): void {
    void writeRemoteAudit(buildRemoteAuditRecord({
      action: audit.action,
      conclusion,
      targetId: audit.targetId,
      environment: audit.environment,
      command: cmd,
      result,
    })).then(ok => {
      if (!ok) toast.warning('审计记录写入失败：本次 K8s 操作未进入审计链')
    })
  }

  async function k8sExec(
    cmd: string, id = targetId.value, timeoutSec = READ_TIMEOUT_SEC, auditAction?: string,
  ): Promise<string> {
    const api = window.electronAPI
    if (!id || !api) throw { code: 'NO_SESSION', message: '未选择目标主机', retryable: false } as K8sError
    // 只有变更动作传 auditAction；读路径（列表/详情/日志）不写审计，
    // 否则一次刷新就会往审计链里灌十几条噪声。
    const auditCtx = auditAction ? { action: auditAction, ...k8sAuditTarget(id) } : null
    const started = Date.now()
    let res
    try {
      // 渲染层闸门：主进程超时兜的是命令，兜不住「IPC 回包丢失」——那时 await 永不结算，
      // 页面就永远转圈。多给 3 秒，正常路径轮不到它。
      res = await withExecDeadline(api.sshExec(id, cmd, timeoutSec), timeoutSec)
    } catch (err) {
      const kErr = describeThrownK8sError(err)
      if (auditCtx) {
        const outcome = auditOutcomeForDispatchError(err, kErr.message, timeoutSec)
        writeK8sAudit(auditCtx, cmd, outcome.conclusion, outcome.result)
      }
      throw kErr
    }
    if (isOutputTruncated(res.stdout, res.stderr)) truncated.value = true
    const durationMs = Date.now() - started
    const kErr = k8sErrorFromResult(res.stderr, res.exitCode, res.stdout)
    if (auditCtx) {
      // sshExec 回包 = 命令已下发；kubectl 返回非零（对象已不存在等）仍记 executed，
      // 错误码附在 result 里供检索 —— 审计结论不是成败统计。
      const base = auditResultForExit(res.exitCode, durationMs)
      writeK8sAudit(auditCtx, cmd, 'executed', kErr ? `${base} k8s_error=${kErr.code}` : base)
    }
    if (kErr) throw kErr
    return res.stdout
  }

  function actionExec(cmd: string): Promise<string> {
    return k8sExec(cmd, targetId.value, ACTION_TIMEOUT_SEC, AUDIT_ACTION_K8S)
  }

  function toastErr(e: unknown): void {
    const k = e as K8sError
    toast.error(k?.message || String(e), 6000, k?.hint ? { details: k.hint } : undefined)
  }

  /**
   * 列表取数同构：pin 住发起时的宿主与序号 → 取数 → 只在仍新鲜时写回，
   * 失败（同 pin 下）如实上浮到 error 并把列表清空，绝不伪装成「这里没有资源」。
   */
  function createList<T>(spec: ListSpec<T>): K8sList<T> {
    const rows = ref<T[]>([]) as Ref<T[]>
    const error = ref<K8sError | undefined>(undefined)
    async function reload(): Promise<void> {
      const id = targetId.value
      if (!id) return
      const pin = pinList(spec.kind, id)
      try {
        const next = spec.parse(await k8sExec(spec.cmd(), id))
        if (!listFresh(spec.kind, pin)) return
        rows.value = next
        error.value = undefined
        markSynced(spec.kind, spec.label)
        spec.after?.(id, pin)
      } catch (e) {
        if (!listFresh(spec.kind, pin)) return
        error.value = toK8sError(e)
        rows.value = []
      }
    }
    return { rows, error, reload }
  }

  const host: K8sHost = {
    ...remoteTarget,
    truncated,
    tab,
    showNormalEvents,
    contextName,
    envProbe,
    lastSync,
    k8sExec,
    actionExec,
    toastErr,
    listFresh,
    createList,
  }
  provide(K8S_HOST_KEY, host)
  return host
}

/** 页签与抽屉取同一份底座实例；脱离 K8sView 使用时是编码错误，直接抛。 */
export function useK8sHost(): K8sHost {
  const host = inject(K8S_HOST_KEY, null)
  if (!host) throw new Error('useK8sHost() 必须在 K8sView 的组件树内调用')
  return host
}

/** 头部的 context 读数：失败不报错、不上浮，只把显示留空（它不影响任何列表） */
export async function readK8sContext(host: K8sHost): Promise<void> {
  const id = host.targetId.value
  if (!id) return
  try {
    const out = await host.k8sExec(CONTEXT_CMD, id)
    if (host.targetId.value !== id) return
    host.contextName.value = out.trim()
  } catch {
    host.contextName.value = ''
  }
}

/**
 * 未检测到 k8s 时的现场自检：以应用实际使用的账号、HOME、PATH 去读 kubeconfig。
 * 只在失败后跑一次，且不参与任何列表判定 —— 它回答「为什么读不到」，不改变结论。
 */
export async function probeK8sEnv(host: K8sHost): Promise<void> {
  const id = host.targetId.value
  if (!id) return
  try {
    const out = await host.k8sExec(ENV_PROBE_CMD, id)
    if (host.targetId.value !== id) return
    host.envProbe.value = parseEnvProbe(out)
  } catch {
    host.envProbe.value = null
  }
}
