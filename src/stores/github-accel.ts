// stores/github-accel.ts — GitHub 加速（镜像套件）
// 配置/测速/git/ssh 状态全部经 ghaccel:* IPC 落主进程，本 store 只做缓存与动作编排。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  GITHUB_BASE,
  compareProbe,
  maskToken,
  type AccelProxy,
  type GitPrivateToken,
  type GitPushMode,
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
type TuneResult = Awaited<ReturnType<NonNullable<typeof window.electronAPI>['ghaccelAutoTune']>>

/** 一键加速单项结果：UI 总控卡逐项显示成败。
 *  总控对称管辖全部三项（git / ssh443 / hosts）：启动 = 逐项开启（hosts 自动
 *  优选 + 写入，一次 UAC），关闭 = 逐项还原。此前 enable 不含 hosts 而 disable
 *  又删 hosts 的不对称语义已废弃 —— 用户预期是「启动加速后完全被动，不再手动」。 */
export interface MasterStep { key: 'git' | 'ssh443' | 'hosts'; ok: boolean; message?: string }

export const useGithubAccelStore = defineStore('github-accel', () => {
  const proxies = ref<AccelProxy[]>([])
  const gitBackup = ref<GhConfig['gitBackup']>(null)
  const lastProxyPrefix = ref<string | undefined>(undefined)
  /** push 目的地策略（direct = 直连 GitHub，ssh = 改写成 git@github.com:） */
  const pushMode = ref<GitPushMode>('direct')
  /** hosts 失效时自动重选（默认关闭：后台写 hosts 不弹 UAC） */
  const hostsAutoRefresh = ref(false)
  const loaded = ref(false)

  const privateTokens = ref<GitPrivateToken[]>([])
  /** 有真实令牌（非空）的条目——业务入口据此判断是否注入鉴权 */
  const activePrivateTokens = computed(() => privateTokens.value.filter(t => t.token.trim().length > 0))
  const tokensByHost = computed(() => {
    const map = new Map<string, GitPrivateToken>()
    for (const t of activePrivateTokens.value) map.set(t.host.toLowerCase(), t)
    return map
  })
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
    // 令牌独立读取（getConfig 合并后已有，但独立 getTokens 保证「仅配置过 token 未动镜像」也能拿到）
    try {
      privateTokens.value = await api.ghaccelGetTokens()
    } catch { /* 主进程不支持时忽略（如纯 web 预览） */ }
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
    gitBackup.value = config.gitBackup
    lastProxyPrefix.value = config.lastProxyPrefix
    pushMode.value = config.pushMode === 'ssh' ? 'ssh' : 'direct'
    hostsAutoRefresh.value = config.hostsAutoRefresh === true
    if (Array.isArray(config.privateTokens)) privateTokens.value = config.privateTokens
  }

  // ─── 镜像清单管理（仅停用/恢复；清单本身由一键启动自动维护） ───
  async function persist(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    applyConfig(await api.ghaccelSaveLists(proxies.value))
  }

  // ─── 私人令牌（PAT） ───
  /** 按 host 取已启用的令牌（用于后续 clone/push 注入 https://token@host）；未配置返回 null */
  function tokenForHost(host: string): GitPrivateToken | null {
    if (!host) return null
    return tokensByHost.value.get(host.toLowerCase()) ?? null
  }

  /**
   * 提交令牌全量清单（渲染层改完本地 ref 后整表提交）。
   * 主进程 saveTokens 失败时回滚本次提交前的完整列表（含空模板），避免 UI 与磁盘不一致。
   *
   * 注意：service.saveTokens 会过滤空 token 模板（只存有效条目）。为避免本地「未配置槽位」
   * 在 save 后被丢，这里用 payload 副本作 base，saved 有效条目按 id 覆盖，空模板原样保留。
   */
  async function saveTokens(next?: GitPrivateToken[]): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    // IPC 走 Structured Clone，Vue reactive Proxy / getter 会抛 "An object could not be cloned"。
    // 落闸前用 JSON 序列化剥一层，保证 payload 是纯数据快照。
    const raw = next ?? privateTokens.value
    const payload: GitPrivateToken[] = JSON.parse(JSON.stringify(raw)) as GitPrivateToken[]
    const snapshot = payload.slice()
    try {
      const saved: GitPrivateToken[] = await api.ghaccelSaveTokens(payload)
      const byId = new Map(saved.map(t => [t.id, t]))
      privateTokens.value = snapshot.map(t => byId.get(t.id) ?? t)
    } catch (e) {
      privateTokens.value = snapshot
      throw e
    }
  }

  async function toggleProxy(id: string, enabled: boolean): Promise<void> {
    const p = proxies.value.find(x => x.id === id)
    if (!p) return
    p.enabled = enabled
    await persist()
  }

  async function removeProxy(id: string): Promise<void> {
    proxies.value = proxies.value.filter(p => p.id !== id)
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

  /** 当前最快可用代理（按 compareProbe：速度优先、延迟破平）；未测速时为 null */
  const bestProxyId = computed<string | null>(() => {
    const rows = enabledProxies.value
      .map(p => ({ id: p.id, r: probeResults.value[p.id] }))
      .filter((x): x is { id: string; r: ProbeRow } => !!x.r?.ok)
    if (!rows.length) return null
    rows.sort((a, b) => compareProbe(a.r, b.r))
    return rows[0].id
  })

  // ─── git insteadOf ───
  async function refreshGitStatus(): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    git.value = await api.ghaccelGitStatus()
  }

  async function gitEnable(prefix: string, mode?: GitPushMode): Promise<void> {
    const api = window.electronAPI
    if (!api || gitBusy.value) return
    gitBusy.value = true
    try {
      git.value = await api.ghaccelGitEnable(prefix, mode ?? pushMode.value)
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
      git.value = await api.ghaccelGitEnable(prefix, pushMode.value)
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
   * 自动调优文案：只站用户视角说「这次替你做了什么」（换了谁、补了谁、删了谁），
   * 不暴露探测维度等实现细节。
   *
   * discovered 是「本次新拉到、不在清单里的候选数」——每次启动都拉，所以
   * 没补入也会报一句，让用户知道镜像池确实在跟新（而不是靠内置那几条吃老本）。
   */
  function tuneSummary(t: TuneResult): string | undefined {
    const bits: string[] = []
    if (t.best) bits.push(`已选 ${t.best.name}`)
    if (t.adopted.length) bits.push(`自动加入 ${t.adopted.map(a => a.name).join('、')}`)
    else if (t.discovered > 0) bits.push(`新候选 ${t.discovered} 个（可用镜像已够用，未补充）`)
    if (t.retired.length) bits.push(`淘汰 ${t.retired.length} 个连续失效镜像`)
    return bits.length ? `自动调优：${bits.join('，')}` : undefined
  }

  /**
   * 启动加速（总控）：先自动调优 —— git insteadOf（全局 git 配置，可一键还原）、
   * SSH 443（~/.ssh/config 托管块，可一键移除）、hosts（自动优选 + 写入，
   * 弹一次 UAC 提权）。单项失败不中断其余项。
   *
   * 「懒人版」的关键在第一步：以前沿用 lastProxyPrefix（上次选的那个可能早死了），
   * 现在每次实测重选最快；连续失效的自动淘汰；一个可用的都没有时自动从聚合接口
   * 发现 → 实测 → 采纳。用户只需要点这一个按钮，测速/发现/加入/删除都不用手动。
   *
   * hosts 纳入总控是产品决策（2026-10-19）：用户预期「启动加速后完全被动」，
   * 手动两步优选/写入的心智负担高于一次 UAC 确认。优选 0 个可用 IP 时该项记失败，
   * 其余项照常生效。
   */
  async function masterEnable(): Promise<MasterStep[]> {
    const api = window.electronAPI
    if (!api) throw new Error('electronAPI 不可用')
    const steps: MasterStep[] = []

    let tune: TuneResult | null = null
    let tuneError: string | undefined
    try {
      tune = await api.ghaccelAutoTune()
      applyConfig(await api.ghaccelGetConfig())
      // 调优结果就是一轮完整测速：把表格一起刷新，省得用户再点「全部测速」
      const map: Record<string, ProbeRow> = {}
      for (const r of tune.probed) map[r.id] = r
      probeResults.value = map
      probedAt.value = Date.now()
    } catch (e: any) {
      tuneError = e?.message
    }

    // 有实测结果就严格信它；只有调优本身失败（拿不到任何探测数据）才回退到旧口径 ——
    // 都实测失败了还硬选一个，等于把 clone 锁死在死镜像上（insteadOf 会把
    // github.com 改写到死前缀，比不加速更糟）。
    const remembered = lastProxyPrefix.value && proxies.value.some(p => p.enabled && p.prefix === lastProxyPrefix.value)
      ? lastProxyPrefix.value
      : undefined
    const prefix = tune ? tune.best?.prefix : (remembered ?? enabledProxies.value[0]?.prefix)
    if (prefix) {
      try {
        await api.ghaccelGitEnable(prefix, pushMode.value)
        await api.ghaccelSaveOptions({ lastProxyPrefix: prefix })
        applyConfig(await api.ghaccelGetConfig())
        await refreshGitStatus()
        steps.push({ key: 'git', ok: true, message: tune ? tuneSummary(tune) : undefined })
      } catch (e: any) {
        steps.push({ key: 'git', ok: false, message: e?.message })
      }
    } else {
      const message = tuneError
        ? `自动选镜像失败：${tuneError}`
        : tune && tune.adopted.length > 0
          ? '自动发现的新镜像实测均不可用'
          : tune && tune.discovered > 0
            ? `发现 ${tune.discovered} 个新候选但没有可用的镜像`
            : '镜像全部失效（含自动发现的候选）；如 git 已指向失效镜像，请先关闭加速恢复直连'
      steps.push({ key: 'git', ok: false, message })
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

  async function saveOptions(patch: { lastProxyPrefix?: string; pushMode?: GitPushMode; hostsAutoRefresh?: boolean }): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    applyConfig(await api.ghaccelSaveOptions(patch))
  }

  /**
   * 切 push 策略：已接管时立即重写 push 规则（fetch 不受影响，不用重开加速）。
   * 国内直连 https push 经常卡死，改走 SSH 443 与 ssh443 托管块天然配对。
   */
  async function setPushMode(mode: GitPushMode): Promise<void> {
    const api = window.electronAPI
    if (!api || gitBusy.value) return
    if (mode === pushMode.value && activeGitPrefix.value) return
    pushMode.value = mode
    const active = activeGitPrefix.value
    if (active) {
      gitBusy.value = true
      try {
        git.value = await api.ghaccelGitEnable(active, mode)
      } finally {
        gitBusy.value = false
      }
    }
    applyConfig(await api.ghaccelSaveOptions({ pushMode: mode }))
  }

  /** hosts 失效时自动重选开关（默认关；开启后只在无需提权时静默生效） */
  async function setHostsAutoRefresh(on: boolean): Promise<void> {
    const api = window.electronAPI
    if (!api) return
    hostsAutoRefresh.value = on
    applyConfig(await api.ghaccelSaveOptions({ hostsAutoRefresh: on }))
  }

  return {
    proxies, gitBackup, loaded,
    lastProxyPrefix, pushMode, hostsAutoRefresh,
    privateTokens, activePrivateTokens, tokensByHost,
    probeResults, probing, probedAt,
    git, gitBusy, ssh443, sshBusy, downloads,
    hosts, hostsPicks, hostsPicking, hostsBusy, hostsReadyCount,
    health, selfCheckRows, selfChecking,
    enabledProxies, bestProxyId, activeMeasureCount, activeGitPrefix,
    init, initHealth, runSelfCheck,
    toggleProxy, removeProxy,
    probeAll, probeOf,
    refreshGitStatus, gitEnable, gitDisable, switchActiveMirror,
    refreshSsh443, ssh443Apply, ssh443Remove,
    downloadStart, downloadCancel, showInFolder,
    refreshHosts, hostsPick, hostsApply, hostsRemove,
    masterEnable, masterDisable, saveOptions, setPushMode, setHostsAutoRefresh,
    tokenForHost, saveTokens,
    maskToken,
  }
})
