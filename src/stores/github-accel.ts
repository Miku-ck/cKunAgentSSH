// stores/github-accel.ts — GitHub 加速（镜像套件）
// 纯逻辑（URL 解析/链接生成）在 shared/github-accel-core.ts，与主进程共用；
// 配置/测速/git/ssh 状态全部经 ghaccel:* IPC 落主进程，本 store 只做缓存与动作编排。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  buildAccelLinks,
  parseGithubUrl,
  type AccelLinkSet,
  type AccelProxy,
  type AccelWebMirror,
  type ParsedGhUrl,
} from '../../shared/github-accel-core'

type GhConfig = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelGetConfig']>>
type ProbeRow = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelProbeAll']>>[number]
type GitStatus = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelGitStatus']>>
type Ssh443 = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelSsh443Status']>>
type DownloadStatus = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelDownloadList']>>[number]
type HostsStatus = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelHostsStatus']>>
type HostsPickRow = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelHostsPick']>>[number]

/** 一键加速单项结果：UI 总控卡逐项显示成败。
 *  hosts 不在其中 —— 总控对 hosts 是「既不代写也不移除」（enable/disable 都不碰），
 *  hosts 始终手动。此前 disable 会顺手删 hosts，与视图文案「独立于总控」矛盾，已修。 */
export interface MasterStep { key: 'git' | 'ssh443'; ok: boolean; message?: string }

function genId(): string {
  return `ux-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export const useGithubAccelStore = defineStore('github-accel', () => {
  const proxies = ref<AccelProxy[]>([])
  const webMirrors = ref<AccelWebMirror[]>([])
  const gitBackup = ref<GhConfig['gitBackup']>(null)
  const lastProxyPrefix = ref<string | undefined>(undefined)
  const loaded = ref(false)

  const probeResults = ref<Record<string, ProbeRow>>({})
  const probing = ref(false)
  const probedAt = ref(0)

  const git = ref<GitStatus | null>(null)
  const gitBusy = ref(false)
  const ssh443 = ref<Ssh443 | null>(null)
  const sshBusy = ref(false)

  const downloads = ref<DownloadStatus[]>([])
  let downloadUnsub: (() => void) | null = null

  const hosts = ref<HostsStatus | null>(null)
  const hostsPicks = ref<HostsPickRow[]>([])
  const hostsPicking = ref(false)
  const hostsBusy = ref(false)

  const enabledProxies = computed(() => proxies.value.filter(p => p.enabled))

  async function init(): Promise<void> {
    const api = window.electronAPI
    if (!api || loaded.value) return
    const config = await api.ghaccelGetConfig()
    applyConfig(config)
    loaded.value = true
    // git / ssh 状态并行拉取（失败不阻塞页面，状态区显示提示文案）
    void refreshGitStatus().catch(() => undefined)
    void refreshSsh443().catch(() => undefined)
    void refreshHosts().catch(() => undefined)
    // 下载任务：恢复内存中仍在跑的任务 + 订阅推送（页面重开不丢下载进度）
    if (!downloadUnsub) {
      downloadUnsub = api.onGhaccelDownload(status => {
        const idx = downloads.value.findIndex(d => d.id === status.id)
        if (idx === -1) downloads.value = [...downloads.value, status]
        else downloads.value = downloads.value.map(d => (d.id === status.id ? status : d))
      })
      try {
        downloads.value = await api.ghaccelDownloadList()
      } catch { /* 主进程不支持时忽略（如纯 web 预览） */ }
    }
  }

  function applyConfig(config: GhConfig): void {
    proxies.value = config.proxies
    webMirrors.value = config.webMirrors
    gitBackup.value = config.gitBackup
    lastProxyPrefix.value = config.lastProxyPrefix
  }

  // ─── 链接转换（纯前端，同步） ───
  function convert(input: string): { parsed: ParsedGhUrl; result: AccelLinkSet } {
    const parsed = parseGithubUrl(input)
    return { parsed, result: buildAccelLinks(parsed, proxies.value, webMirrors.value) }
  }

  // ─── 镜像清单管理 ───
  async function persist(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    applyConfig(await api.ghaccelSaveLists(proxies.value, webMirrors.value))
  }

  async function toggleProxy(id: string, enabled: boolean): Promise<void> {
    const p = proxies.value.find(x => x.id === id)
    if (!p) return
    p.enabled = enabled
    await persist()
  }

  async function toggleWebMirror(id: string, enabled: boolean): Promise<void> {
    const w = webMirrors.value.find(x => x.id === id)
    if (!w) return
    w.enabled = enabled
    await persist()
  }

  async function addProxy(name: string, prefix: string): Promise<void> {
    proxies.value = [...proxies.value, { id: genId(), name: name.trim() || prefix.trim(), prefix: prefix.trim(), builtin: false, enabled: true }]
    await persist()
  }

  async function addWebMirror(name: string, host: string): Promise<void> {
    webMirrors.value = [...webMirrors.value, { id: genId(), name: name.trim() || host.trim(), host: host.trim().toLowerCase(), builtin: false, enabled: true }]
    await persist()
  }

  async function removeProxy(id: string): Promise<void> {
    proxies.value = proxies.value.filter(p => p.id !== id)
    await persist()
  }

  async function removeWebMirror(id: string): Promise<void> {
    webMirrors.value = webMirrors.value.filter(w => w.id !== id)
    await persist()
  }

  // ─── 测速 ───
  async function probeAll(): Promise<void> {
    const api = window.electronAPI
    if (!api || probing.value) return
    probing.value = true
    try {
      const rows = await api.ghaccelProbeAll()
      const map: Record<string, ProbeRow> = {}
      for (const r of rows) map[r.id] = r
      probeResults.value = map
      probedAt.value = Date.now()
    } finally {
      probing.value = false
    }
  }

  function probeOf(id: string): ProbeRow | null {
    return probeResults.value[id] ?? null
  }

  /** 当前最快可用代理（测速结果 ok 且延迟最低）；未测速时为 null */
  const bestProxyId = computed<string | null>(() => {
    let best: string | null = null
    let bestMs = Infinity
    for (const p of enabledProxies.value) {
      const r = probeResults.value[p.id]
      if (r?.ok && r.latencyMs !== null && r.latencyMs < bestMs) {
        best = p.id
        bestMs = r.latencyMs
      }
    }
    return best
  })

  // ─── git insteadOf ───
  async function refreshGitStatus(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    git.value = await api.ghaccelGitStatus()
  }

  async function gitEnable(prefix: string): Promise<void> {
    const api = window.electronAPI
    if (!api || gitBusy.value) return
    gitBusy.value = true
    try {
      git.value = await api.ghaccelGitEnable(prefix)
    } finally {
      gitBusy.value = false
    }
  }

  async function gitDisable(): Promise<void> {
    const api = window.electronAPI
    if (!api || gitBusy.value) return
    gitBusy.value = true
    try {
      git.value = await api.ghaccelGitDisable()
      // 关闭后同步一次接管凭证（应为 null）
      const config = await api.ghaccelGetConfig()
      gitBackup.value = config.gitBackup
    } finally {
      gitBusy.value = false
    }
  }

  // ─── ssh 443 ───
  async function refreshSsh443(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    ssh443.value = await api.ghaccelSsh443Status()
  }

  async function ssh443Apply(): Promise<void> {
    const api = window.electronAPI
    if (!api || sshBusy.value) return
    sshBusy.value = true
    try {
      ssh443.value = await api.ghaccelSsh443Apply()
    } finally {
      sshBusy.value = false
    }
  }

  async function ssh443Remove(): Promise<void> {
    const api = window.electronAPI
    if (!api || sshBusy.value) return
    sshBusy.value = true
    try {
      ssh443.value = await api.ghaccelSsh443Remove()
    } finally {
      sshBusy.value = false
    }
  }

  // ─── 下载器 ───
  async function downloadStart(url: string): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const st = await api.ghaccelDownloadStart(url)
    if (!downloads.value.some(d => d.id === st.id)) {
      downloads.value = [...downloads.value, st]
    }
  }

  async function downloadCancel(id: string): Promise<void> {
    await window.electronAPI?.ghaccelDownloadCancel(id)
  }

  async function showInFolder(id: string): Promise<void> {
    await window.electronAPI?.ghaccelShowItemInFolder(id)
  }

  // ─── hosts 优选 IP ───
  async function refreshHosts(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    hosts.value = await api.ghaccelHostsStatus()
  }

  async function hostsPick(): Promise<void> {
    const api = window.electronAPI
    if (!api || hostsPicking.value) return
    hostsPicking.value = true
    try {
      hostsPicks.value = await api.ghaccelHostsPick()
    } finally {
      hostsPicking.value = false
    }
  }

  async function hostsApply(): Promise<void> {
    const api = window.electronAPI
    if (!api || hostsBusy.value) return
    hostsBusy.value = true
    try {
      hosts.value = await api.ghaccelHostsApply()
    } finally {
      hostsBusy.value = false
    }
  }

  async function hostsRemove(): Promise<void> {
    const api = window.electronAPI
    if (!api || hostsBusy.value) return
    hostsBusy.value = true
    try {
      hosts.value = await api.ghaccelHostsRemove()
    } finally {
      hostsBusy.value = false
    }
  }

  /** 优选结果里全部拿到 IP 的条目数（UI 据此决定是否允许写入） */
  const hostsReadyCount = computed(() => hostsPicks.value.filter(r => r.ip).length)

  // ─── 一键加速 / 关闭加速（总控） ───

  /**
   * 启动加速：只编排零系统风险的两项 —— git insteadOf（全局 git 配置，可一键还原）、
   * SSH 443（~/.ssh/config 托管块，可一键移除）。单项失败不中断其余项。
   *
   * 刻意不包含 hosts 优选：写 hosts 需要管理员授权且影响系统域名解析，必须由用户在
   * 下方卡片里显式两步操作（「优选 IP」→「写入」）完成，总控不代劳。
   */
  async function masterEnable(): Promise<MasterStep[]> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const steps: MasterStep[] = []

    const prefix = lastProxyPrefix.value && proxies.value.some(p => p.enabled && p.prefix === lastProxyPrefix.value)
      ? lastProxyPrefix.value
      : enabledProxies.value[0]?.prefix
    if (prefix) {
      try {
        await api.ghaccelGitEnable(prefix)
        await api.ghaccelSaveOptions({ lastProxyPrefix: prefix })
        applyConfig(await api.ghaccelGetConfig())
        await refreshGitStatus()
        steps.push({ key: 'git', ok: true })
      } catch (e: any) {
        steps.push({ key: 'git', ok: false, message: e?.message })
      }
    } else {
      steps.push({ key: 'git', ok: false, message: '没有可用的代理镜像' })
    }

    try {
      await api.ghaccelSsh443Apply()
      await refreshSsh443()
      steps.push({ key: 'ssh443', ok: true })
    } catch (e: any) {
      steps.push({ key: 'ssh443', ok: false, message: e?.message })
    }

    return steps
  }

  /** 关闭加速：只撤销总控管辖的两项（git + ssh443）。hosts 是用户手动写入的
   *  （含 UAC 提权），总控无权代删 —— 此前这里会 ghaccelHostsRemove，把用户
   *  手动的 hosts 一并清掉，与视图文案「独立于总控」矛盾，已修。 */
  async function masterDisable(): Promise<MasterStep[]> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const steps: MasterStep[] = []

    try {
      await api.ghaccelSsh443Remove()
      await refreshSsh443()
      steps.push({ key: 'ssh443', ok: true })
    } catch (e: any) {
      steps.push({ key: 'ssh443', ok: false, message: e?.message })
    }

    try {
      await api.ghaccelGitDisable()
      await refreshGitStatus()
      steps.push({ key: 'git', ok: true })
    } catch (e: any) {
      steps.push({ key: 'git', ok: false, message: e?.message })
    }
    return steps
  }

  /** 总控管辖的两项（git + ssh443）生效数。hosts 始终手动，不计入 ——
   *  此前把 hosts 算进来，「已开启」判 n===3 永远到不了（总控不代写 hosts），
   *  且手动写了 hosts 会让总控按钮卡在「关闭加速」无法启动剩余项。 */
  const activeMeasureCount = computed(() => {
    let n = 0
    if (git.value?.active) n++
    if (ssh443.value?.applied) n++
    return n
  })

  async function saveOptions(patch: { lastProxyPrefix?: string }): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    applyConfig(await api.ghaccelSaveOptions(patch))
  }

  return {
    proxies, webMirrors, gitBackup, loaded,
    lastProxyPrefix,
    probeResults, probing, probedAt,
    git, gitBusy, ssh443, sshBusy, downloads,
    hosts, hostsPicks, hostsPicking, hostsBusy, hostsReadyCount,
    enabledProxies, bestProxyId, activeMeasureCount,
    init, convert,
    toggleProxy, toggleWebMirror, addProxy, addWebMirror, removeProxy, removeWebMirror,
    probeAll, probeOf,
    refreshGitStatus, gitEnable, gitDisable,
    refreshSsh443, ssh443Apply, ssh443Remove,
    downloadStart, downloadCancel, showInFolder,
    refreshHosts, hostsPick, hostsApply, hostsRemove,
    masterEnable, masterDisable, saveOptions,
  }
})
