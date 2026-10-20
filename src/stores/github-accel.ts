// stores/github-accel.ts — GitHub 加速（镜像套件）
// 纯逻辑（URL 解析/链接生成）在 shared/github-accel-core.ts，与主进程共用；
// 配置/测速/git/ssh 状态全部经 ghaccel:* IPC 落主进程，本 store 只做缓存与动作编排。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  buildAccelLinks,
  parseGithubUrl,
  GITHUB_BASE,
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
type SelfCheckRow = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelSelfCheck']>>[number]
type HealthStatus = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelHealthStatus']>>

/** 一键加速单项结果：UI 总控卡逐项显示成败。
 *  总控对称管辖全部三项（git / ssh443 / hosts）：启动 = 逐项开启（hosts 自动
 *  优选 + 写入，一次 UAC），关闭 = 逐项还原。此前 enable 不含 hosts 而 disable
 *  又删 hosts 的不对称语义已废弃 —— 用户预期是「启动加速后完全被动，不再手动」。 */
export interface MasterStep { key: 'git' | 'ssh443' | 'hosts'; ok: boolean; message?: string }

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

  const health = ref<HealthStatus | null>(null)
  const selfCheckRows = ref<SelfCheckRow[]>([])
  const selfChecking = ref(false)
  let healthUnsub: (() => void) | null = null

  const enabledProxies = computed(() => proxies.value.filter(p => p.enabled))

  /** 当前 git 生效前缀（从 insteadOf 子节反推）；未生效为空串 */
  const activeGitPrefix = computed(() => {
    const sub = git.value?.activeSubsection
    if (!sub) return ''
    return sub.endsWith(GITHUB_BASE) ? sub.slice(0, -GITHUB_BASE.length) : sub
  })

  /**
   * 巡检订阅（轻量：只拉状态 + 订阅，不加载整份配置）。
   * 标题栏用它挂红点，页面 init 也会调用 —— 两者共用同一份订阅，不重复注册。
   */
  async function initHealth(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    if (!healthUnsub) {
      healthUnsub = api.onGhaccelHealth(status => { health.value = status })
    }
    try {
      health.value = await api.ghaccelHealthStatus()
    } catch { /* 主进程不支持时忽略（如纯 web 预览） */ }
  }

  /** 端到端自检（只读）：结果按行展示，失败不阻断页面 */
  async function runSelfCheck(): Promise<void> {
    const api = window.electronAPI
    if (!api || selfChecking.value) return
    selfChecking.value = true
    try {
      selfCheckRows.value = await api.ghaccelSelfCheck()
    } finally {
      selfChecking.value = false
    }
  }

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
    void initHealth()
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

  /**
   * 切换生效镜像：手动下拉与巡检红灯按钮共用同一入口。
   * 只改 git insteadOf 与上次选择（不碰用户仓库 remote）；主进程 2s 后会自动复检并推送新状态。
   */
  async function switchActiveMirror(prefix: string): Promise<void> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    if (gitBusy.value) return
    gitBusy.value = true
    try {
      git.value = await api.ghaccelGitEnable(prefix)
      applyConfig(await api.ghaccelSaveOptions({ lastProxyPrefix: prefix }))
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
   * 启动加速：编排全部三项 —— git insteadOf（全局 git 配置，可一键还原）、
   * SSH 443（~/.ssh/config 托管块，可一键移除）、hosts（自动优选 + 写入，
   * 弹一次 UAC 提权）。单项失败不中断其余项。
   *
   * hosts 纳入总控是产品决策（2026-10-19）：用户预期「启动加速后完全被动」，
   * 手动两步优选/写入的心智负担高于一次 UAC 确认。优选 0 个可用 IP 时该项记失败，
   * 其余项照常生效。
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

    try {
      const picks = await api.ghaccelHostsPick()
      if (!picks.some(r => r.ip)) {
        steps.push({ key: 'hosts', ok: false, message: '优选未得到可用 IP' })
      } else {
        await api.ghaccelHostsApply()
        await refreshHosts()
        steps.push({ key: 'hosts', ok: true })
      }
    } catch (e: any) {
      await refreshHosts()
      steps.push({ key: 'hosts', ok: false, message: e?.message })
    }

    return steps
  }

  /** 关闭加速：逆序撤销全部三项（与 masterEnable 对称；hosts 移除同样按需 UAC）。 */
  async function masterDisable(): Promise<MasterStep[]> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const steps: MasterStep[] = []

    try {
      await api.ghaccelHostsRemove()
      await refreshHosts()
      steps.push({ key: 'hosts', ok: true })
    } catch (e: any) {
      await refreshHosts()
      steps.push({ key: 'hosts', ok: false, message: e?.message })
    }

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

  /** 总控管辖的三项生效数（hosts 已纳入总控，n/3 语义成立） */
  const activeMeasureCount = computed(() => {
    let n = 0
    if (git.value?.active) n++
    if (ssh443.value?.applied) n++
    if (hosts.value?.applied) n++
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
    health, selfCheckRows, selfChecking,
    enabledProxies, bestProxyId, activeMeasureCount, activeGitPrefix,
    init, initHealth, runSelfCheck,
    convert,
    toggleProxy, toggleWebMirror, addProxy, addWebMirror, removeProxy, removeWebMirror,
    probeAll, probeOf,
    refreshGitStatus, gitEnable, gitDisable, switchActiveMirror,
    refreshSsh443, ssh443Apply, ssh443Remove,
    downloadStart, downloadCancel, showInFolder,
    refreshHosts, hostsPick, hostsApply, hostsRemove,
    masterEnable, masterDisable, saveOptions,
  }
})
