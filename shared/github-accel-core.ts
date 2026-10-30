/**
 * GitHub 加速 —— 纯逻辑核心（无 electron / node 依赖，渲染层与主进程共用）
 *
 * 方案定调（2026-10-13）：用户无服务器，排除 SSH 隧道（-D）与本地反代 MITM，
 * 主线 = A+ 镜像加速套件：
 *   - clone 慢/失败 → ghproxy 类代理前缀 + git url.insteadOf 全局改写
 *   - Release 下载  → 代理前缀直连下载
 * 另附 GitHub 官方支持的零成本技巧：git@github.com 走 ssh.github.com:443（绕开被
 * QoS 的 22 端口），以 ~/.ssh/config 托管块形式写入/移除（BEGIN/END 标记圈定）。
 *
 * 风险提示（UI 必须展示）：公共镜像 = 不可信中继，经其 clone/下载的代码理论上可被
 * 篡改，敏感场景应核对 commit hash。
 */

/** 代理镜像（ghproxy 类）：prefix + 完整 GitHub URL 即为加速地址 */
export interface AccelProxy {
  id: string
  name: string
  /** 必须以 https:// 开头、/ 结尾，如 https://ghproxy.net/ */
  prefix: string
  /** 内置条目不可删除（可停用）；用户自增条目可删 */
  builtin: boolean
  enabled: boolean
}

/** 网页镜像站已下线（2026-10-30）：只留 hosts 优选覆盖网页访问，不再维护 host 替换清单。
 *  GithubAccelConfig 也不再带 webMirrors 字段（旧配置文件里的该字段读盘时直接忽略）。
 */
export interface GithubAccelConfig {
  proxies: AccelProxy[]
  /**
   * insteadOf 接管备份：开启时若用户已有其它 github.com 改写规则，先记下原值，
   * 关闭时还原（previousValue 为 null 表示原本没有规则，关闭即删除）。
   * 「当前是否激活」以 git config 实查为准，这里只存接管凭证。
   */
  gitBackup: {
    prefix: string
    previousValue: string | null
    /** 旧版字段（仅 direct 模式的布尔值），读盘时兼容 */
    pushInsteadOfAdded?: boolean
    /** 本应用写入的 push 规则类型；'none' = 用户自带规则，我们没写 */
    pushRewrite?: 'none' | GitPushMode
  } | null
  /** 一键加速使用的镜像前缀（记住上次选择） */
  lastProxyPrefix?: string
  /** push 目的地策略：direct = 直连 GitHub，ssh = 改写成 git@github.com:（走 SSH） */
  pushMode?: GitPushMode
  /** hosts 失效时自动重选并写入（默认关闭；需能直写 hosts，否则只亮红灯） */
  hostsAutoRefresh?: boolean
  /**
   * 连续探测失败计数（proxy id → 次数，成功即清零，落盘跨会话累计）。
   * 一键启动据此淘汰「连续失效」的镜像：单次失败多半是本地网络抖动，
   * 一次就删会把好镜像误杀；只在达到阈值后才动手（见 nextFailStreak）。
   */
  probeStats?: Record<string, number>
  /**
   * 私人令牌配置（按平台/自定义域名）。
   * 落地到 userData/github-accel.json，不在 git 仓库里（PAT 是凭据，不进源码）。
   * UI 层用 maskToken 脱敏展示，持久化与落盘都是原文。
   */
  privateTokens?: GitPrivateToken[]
}

/**
 * 各平台私人令牌（PAT）配置。
 *
 * 典型用途：自托管或国内平台（GitHub / GitCode / Gitea / GitLab）clone/push 走 HTTPS 时，
 * 把令牌注入 https://<user>:<token>@<host>/owner/repo.git 完成鉴权（glibc/openssh-less 环境、
 * CI、仅 HTTPS 可用的网络）。注意：公共代理（ghproxy）只代理读取、不带凭据；PAT 在「直连」
 * 或「自己可控的 https 反代」才有效，切勿把 PAT 发给公共镜像（等价于送人仓库）。
 */
export type PrivateTokenProvider = 'github' | 'gitcode' | 'gitee' | 'gitea' | 'gitlab' | 'custom'

/** 预设平台的 host 映射（custom host 留空由用户填） */
export const PROVIDER_HOSTS: Record<Exclude<PrivateTokenProvider, 'custom'>, string> = {
  github: 'github.com',
  gitcode: 'gitcode.com',
  gitee: 'gitee.com',
  gitea: 'gitea.com',
  gitlab: 'gitlab.com',
}

export const PROVIDER_LABELS: Record<PrivateTokenProvider, string> = {
  github: 'GitHub',
  gitcode: 'GitCode',
  gitee: 'Gitee',
  gitea: 'Gitea',
  gitlab: 'GitLab',
  custom: '自定义',
}

export const BUILTIN_PROVIDERS = Object.keys(PROVIDER_HOSTS) as Array<keyof typeof PROVIDER_HOSTS>

export function defaultPrivateTokens(): GitPrivateToken[] {
  const now = Date.now()
  // 出厂模板：PAT 为空（占位），用户到 UI 里填真实值 —— 源码里不落真实凭据。
  // 顺序即 UI 默认排列：gitcode 优先（国内主仓），次之 github / gitee / gitlab。
  return [
    { id: 'tok-gitcode', provider: 'gitcode', label: PROVIDER_LABELS.gitcode, host: PROVIDER_HOSTS.gitcode, token: '', updatedAt: now },
    { id: 'tok-github', provider: 'github', label: PROVIDER_LABELS.github, host: PROVIDER_HOSTS.github, token: '', updatedAt: now },
    { id: 'tok-gitee', provider: 'gitee', label: PROVIDER_LABELS.gitee, host: PROVIDER_HOSTS.gitee, token: '', updatedAt: now },
  ]
}

export interface GitPrivateToken {
  id: string
  /** 平台预设；custom 时 host 由用户指定 */
  provider: PrivateTokenProvider
  /** 展示名（可自定义，便于区分多账号） */
  label: string
  /** 主机 / 域名（custom 必填，预设取 PROVIDER_HOSTS） */
  host: string
  /** 令牌原文（落地前 trim；不校验格式——各平台差异大，只看非空） */
  token: string
  /** 最后更新时间戳（ms），UI 用于按最近调整排序 */
  updatedAt: number
}

/**
 * 内置代理镜像（2026-10-14 实测校准：gh.llkk.cc 超时已死，剔除）。
 * 公共镜像死亡率高，清单可在 UI 停用/增删，这里只是出厂值。
 */
export function defaultProxies(): AccelProxy[] {
  return [
    { id: 'bp-ghproxy-net', name: 'ghproxy.net', prefix: 'https://ghproxy.net/', builtin: true, enabled: true },
    { id: 'bp-ghfast-top', name: 'ghfast.top', prefix: 'https://ghfast.top/', builtin: true, enabled: true },
    { id: 'bp-gh-proxy-com', name: 'gh-proxy.com', prefix: 'https://gh-proxy.com/', builtin: true, enabled: true },
  ]
}

// ─── 公共镜像清单聚合（动态发现） ───
//
// 公共 ghproxy 死亡率极高，写死在源码里的清单半年后必然全灭。聚合接口只提供
// 「候选」，真正能不能用、快不快仍由本地测速决定。手动发现要用户点一下；一键启动
// 时由 pickAutoAdopt 代劳，但只收实测可用的前两名，不整批采纳。

export const MIRROR_AGGREGATOR_URL = 'https://ghproxy.link/api/v1/sites'

/** 单次发现最多返回多少条候选（UI 一屏能看完的量） */
export const DISCOVER_LIMIT = 12

export interface MirrorCandidate {
  prefix: string
  name: string
}

/**
 * 解析镜像聚合接口的 JSON。常见形态：数组 / { sites: [] } / { data: [] }，
 * 条目可能是字符串或带 url/site/prefix 的对象。无法识别的形状返回空数组
 * （调用方降级到内置清单，不报错）。
 */
export function parseMirrorAggregator(json: unknown, limit: number = DISCOVER_LIMIT): MirrorCandidate[] {
  const out: MirrorCandidate[] = []
  const seen = new Set<string>()

  const add = (raw: unknown, name?: unknown): void => {
    if (typeof raw !== 'string') return
    let s = raw.trim()
    if (!s) return
    if (!/^https?:\/\//i.test(s)) s = `https://${s}`
    // 聚合站常混http录入：前缀只接受 https，否则整条丢弃（不是静默升级）
    if (!/^https:\/\//i.test(s)) return
    if (!s.endsWith('/')) s += '/'
    if (validateProxyPrefix(s)) return
    if (seen.has(s)) return
    seen.add(s)
    let label = typeof name === 'string' ? name.trim().slice(0, 40) : ''
    if (!label) {
      try { label = new URL(s).hostname } catch { label = s }
    }
    out.push({ prefix: s, name: label })
  }

  const addItem = (item: unknown): void => {
    if (typeof item === 'string') { add(item); return }
    if (!item || typeof item !== 'object') return
    const o = item as Record<string, unknown>
    const url = o.url ?? o.site ?? o.prefix ?? o.homepage
    add(url, o.name ?? o.title ?? o.label)
  }

  if (Array.isArray(json)) {
    for (const item of json) addItem(item)
  } else if (json && typeof json === 'object') {
    const o = json as Record<string, unknown>
    for (const key of ['sites', 'data', 'list', 'items', 'result']) {
      const arr = o[key]
      if (Array.isArray(arr)) for (const item of arr) addItem(item)
    }
  }
  return out.slice(0, Math.max(0, limit))
}

export function defaultGithubAccelConfig(): GithubAccelConfig {
  return {
    proxies: defaultProxies(),
    gitBackup: null,
    pushMode: 'direct',
    hostsAutoRefresh: false,
    privateTokens: defaultPrivateTokens(),
  }
}

// ─── 私人令牌（PAT）校验与脱敏（UI/主进程共享的单一事实来源） ───

/**
 * 令牌脱敏：掩码展示用。规则：保留前 3 后 4，中间全部替换为 *（至少 4 颗）。
 * 长度不足 8 时整段打码（`ghp_xxx` 这种短 token 也暴露结构，宁可全掩）。
 * 纯展示函数，不要拿结果当存储值——持久化落原文。
 */
export function maskToken(token: string): string {
  const t = (token || '').trim()
  if (!t) return ''
  if (t.length < 8) return '****'
  return `${t.slice(0, 3)}${'*'.repeat(Math.max(4, t.length - 7))}${t.slice(-4)}`
}

/** 令牌条目落地前清洗；合法返回清洗后对象，非法字段填进 errors（中文，IPC 直透给用户） */
export function sanitizeToken(input: Partial<GitPrivateToken> & { id?: string; provider?: string; host?: string; token?: string; label?: string; updatedAt?: number }, fallbackId: string): { token: GitPrivateToken | null; errors: string[] } {
  const errors: string[] = []
  const provider = (input.provider ?? 'custom') as PrivateTokenProvider
  const isCustom = provider === 'custom'
  let host = ''
  if (isCustom) {
    host = String(input.host ?? '').trim()
    if (!host) errors.push('自定义平台需要填写域名')
    else if (!HOST_RE.test(host)) errors.push('域名格式不正确')
  } else if (provider in PROVIDER_HOSTS) {
    host = PROVIDER_HOSTS[provider as keyof typeof PROVIDER_HOSTS]
  } else {
    errors.push('未知的平台类型 / 域名无效')
  }
  const token = String(input.token ?? '').trim()
  if (!token) errors.push('令牌不能为空')
  if (token.includes(' ') || token.includes('\n')) errors.push('令牌不应含空白字符')
  if (errors.length) return { token: null, errors }
  const labelNorm = (String(input.label ?? '').trim() || (isCustom ? host : PROVIDER_LABELS[provider] || provider)).slice(0, 40)
  return {
    token: {
      id: typeof input.id === 'string' && input.id ? input.id : fallbackId,
      provider,
      label: labelNorm,
      host,
      token,
      updatedAt: typeof input.updatedAt === 'number' ? input.updatedAt : Date.now(),
    },
    errors,
  }
}

/**
 * service 整表落盘前的预处理：过滤 token 为空的「未配置模板」条目（不校验、不入库）；
 * 只保留真正配置了令牌的条目进入 sanitize 校验。返回空条目 id 集合（便于 store 把它们补回 UI）。
 *
 * 为什么不能靠 UI 过滤：出厂模板（tok-gitcode / tok-github / tok-gitee 等）token 为空，但 UI
 * 整表提交时仍会把它们带进 IPC；若 service 直接 sanitize 会触发「令牌不能为空」报死。
 * 抽成纯函数也便于单元测试。
 */
export function filterEmptyTokens(tokens: Array<Partial<GitPrivateToken> | null | undefined>): { persist: Array<Exclude<typeof tokens[number], null | undefined>>; emptyIds: string[] } {
  const persist: Array<Exclude<typeof tokens[number], null | undefined>> = []
  const emptyIds: string[] = []
  for (const t of tokens) {
    if (!t || typeof t !== 'object') continue
    if (String(t.token ?? '').trim() === '') {
      if (typeof t.id === 'string') emptyIds.push(t.id)
      continue
    }
    persist.push(t)
  }
  return { persist, emptyIds }
}

// 下单前清洗 token（注入 remote URL 用）；校验失败抛错（消息直透 toast）
// user 保留签名位（便于未来支持 gitea 的 non-git 用户名鉴权），当前始终 'git'
export function buildTokenAuthUrl(host: string, ownerSlashRepo: string, token: string, _user = 'git'): { url: string; urlMasked: string } {
  if (!token || !token.trim()) throw new Error('令牌为空')
  if (!host || !HOST_RE.test(host.trim())) throw new Error('域名无效')
  const t = token.trim()
  const url = `https://${encodeURIComponent(t)}@${host.trim().toLowerCase()}/${ownerSlashRepo.replace(/^\/+/, '')}`
  return { url, urlMasked: `https://${maskToken(t)}@${host.trim().toLowerCase()}/${ownerSlashRepo.replace(/^\/+/, '')}` }
}

// ─── 配置校验（主进程 save 防线；UI 另有一层） ───

const HOST_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/

/**
 * 校验代理前缀；合法返回 null，非法返回中文原因（IPC 直接透传给用户）。
 *
 * 允许一级路径（`https://host/gh/`）：Cloudflare Worker 类代理（xget 等）把
 * 上游类型写进路径段，只允许 `https://host/` 会把这一整类镜像挡在门外。
 * 多段路径与点段仍拒 —— 前缀是直接拼在完整 URL 前面的字符串，多余层级会把
 * 拼出来的地址带偏（`/a/../b/` 这类归一化后也等于换目标）。
 */
export function validateProxyPrefix(prefix: string): string | null {
  if (typeof prefix !== 'string' || !prefix.trim()) return '镜像前缀不能为空'
  const p = prefix.trim()
  if (!p.startsWith('https://')) return '镜像前缀必须以 https:// 开头（明文 http 会把代码流量暴露给链路）'
  if (!p.endsWith('/')) return '镜像前缀必须以 / 结尾'
  let u: URL
  try {
    u = new URL(p)
  } catch {
    return '镜像前缀不是合法 URL'
  }
  if (u.search || u.hash) return '镜像前缀不能带查询串或锚点'
  if (u.username || u.password) return '镜像前缀不能包含账号凭证（凭据请走私人令牌配置）'
  if (!HOST_RE.test(u.hostname)) return '镜像前缀必须是域名（不能填 IP 或内网主机）'
  const seg = u.pathname.replace(/^\/+|\/+$/g, '')
  if (seg) {
    if (seg.includes('/')) return '镜像前缀只支持一级路径（形如 https://host/gh/）'
    if (seg === '.' || seg === '..' || !/^[A-Za-z0-9._~-]+$/.test(seg)) return '镜像前缀路径含非法字符'
  }
  return null
}

/**
 * 测速探测地址：前缀 + 一个真实可代理的 raw 文件（实测校准——ghproxy 类服务
 * 不代理 github 首页，打首页只会得到 403/404，把活镜像误判成失效）。
 * git/git 仓库的 README 稳定存在，直连与代理都可达。
 */
export const PROBE_TARGET = 'https://raw.githubusercontent.com/git/git/master/README.md'

/**
 * 整仓下载探测目标（第二维）：raw 首字节快不代表带宽好，而 clone/zipball 走的
 * codeload 与 raw 是不同的后端集群。git/git 是长期存在的仓库，拉前 256KB
 * 就断开，不会把整包拖下来。
 */
export const PROBE_TARGET_ARCHIVE = 'https://github.com/git/git/archive/refs/heads/master.zip'

/** 整仓探测最多读多少字节（量带宽用，不是下载） */
export const ARCHIVE_PROBE_BYTES = 256 * 1024

/**
 * 探测目标类别：
 * - raw：单文件首字节（README），兼作“镜像活着吗”的判据
 * - archive：整仓 zipball 小样本，量真实带宽（后端与 raw 不同）
 */
export type ProbeKind = 'raw' | 'archive'

/** 代理/整仓探测通用的取地址：前缀 + 绝对 URL（兼容 `https://host/gh/` 前缀） */
export function buildProbeUrl(prefix: string): string {
  return `${prefix}${PROBE_TARGET}`
}

export function buildProbeUrlForKind(prefix: string, kind: 'raw' | 'archive'): string {
  return `${prefix}${kind === 'archive' ? PROBE_TARGET_ARCHIVE : PROBE_TARGET}`
}

/**
 * 代理探测是否可用：只有 2xx 才算「拿到内容」。403/404/429 都说明镜像没替我们取到
 * 真实存在的 RAW 文件，不能参与「最快」排序，也不能显示为可用。
 */
export function isProxyProbeOk(status: number): boolean {
  return Number.isFinite(status) && status >= 200 && status < 300
}

/**
 * 代理探测的完整判据：2xx 之外还要求响应体不是 HTML。部分"死而不僵"的镜像对任意
 * URL 都回 200 广告/错误页（text/html）——只看状态码会把它们误判为可用并推上
 * 「最快」榜首，下载走过去拿到的却是垃圾页。PROBE_TARGET 是 raw 文本文件，
 * 合规镜像应透传 text/plain 类型。
 */
export function isProxyProbeOkWithContentType(status: number, contentType: string | undefined): boolean {
  if (!isProxyProbeOk(status)) return false
  const ct = (contentType || '').toLowerCase()
  return !ct.includes('text/html')
}

/**
 * 整仓探测：zipball 的 content-type 正常是 application/zip，但镜像可能改写。
 * 只要不是 HTML（错误页/广告页）且拿到过字节，就认为它在转真实文件。
 */
export function isArchiveProbeOk(status: number, contentType: string | undefined): boolean {
  return isProxyProbeOkWithContentType(status, contentType)
}

/** 一次探测的可比指标：首字节延迟 + （可选）实测带宽 */
export interface ProbeMetrics {
  ok: boolean
  /** 首字节延迟（ms）；失败为 null */
  latencyMs: number | null
  /** 实测下行速度（B/s）；未测/失败为 null */
  speedBps?: number | null
}

/** 速度差在这个比例内视为“同一档”，此时用首字节延迟做次选判据 */
export const PROBE_SPEED_TIE_RATIO = 0.15

/**
 * 镜像排序。旧实现只看 TTFB，会把“回得快但只能跑 20KB/s”的线推到“最快”榜首；
 * 现在速度优先（它才是 clone 大仓的短板），延迟只在速度同档时用来破平。
 * 返回 <0 表示 a 排在 b 前面。
 */
export function compareProbe(a: ProbeMetrics, b: ProbeMetrics): number {
  if (a.ok !== b.ok) return a.ok ? -1 : 1
  const sa = a.speedBps ?? null
  const sb = b.speedBps ?? null
  if (sa !== null && sb === null) return -1
  if (sa === null && sb !== null) return 1
  if (sa !== null && sb !== null) {
    const max = Math.max(sa, sb, 1)
    if (Math.abs(sa - sb) / max > PROBE_SPEED_TIE_RATIO) return sb - sa
  }
  const la = a.latencyMs ?? Number.POSITIVE_INFINITY
  const lb = b.latencyMs ?? Number.POSITIVE_INFINITY
  return la - lb
}

/** 取最优可用行（速度优先、延迟破平）；没有可用行返回 null */
export function pickBestProbe<T extends ProbeMetrics>(rows: readonly T[]): T | null {
  let best: T | null = null
  for (const r of rows) {
    if (!r.ok) continue
    if (!best || compareProbe(r, best) < 0) best = r
  }
  return best
}

// ─── 一键懒人模式：自动淘汰 / 自动采纳 ───
//
// 「一键启动」不再是「用上次选的那个」：每次实测重选、无可用时自动发现补充、
// 连续失效的顺手清掉。三道闸门都不能省 ——
// * 淘汰看「连续」失败次数而非单次，否则一次断网就把清单清空；
// * 淘汰内置镜像只停用不删除（用户还能一键恢复），自建/自动采纳的才真删；
// * 自动采纳只收「实测可用」的候选，且限量，避免第三方接口把清单灌爆。

/** 连续探测失败多少次判定失效（跨会话累计，见 GithubAccelConfig.probeStats） */
export const PROBE_RETIRE_STREAK = 3

/** 一键启动自动采纳的新镜像上限（一次最多补两个，够用且不失控） */
export const AUTO_ADOPT_LIMIT = 2

/**
 * 一键启动后希望保有的「可用镜像」数量。
 * 低于它才会实测候选并补入：公共镜像半年内必死一批，每次启动都刷新一次清单
 * 才能跟上；但已够用时不必实测十几个候选（那要多等好几秒）。
 */
export const AUTO_OK_TARGET = 3

/** 代理镜像清单总上限（自动采纳不能无限膨胀） */
export const PROXY_LIST_LIMIT = 20

/** 连续失败计数：成功清零，失败 +1 并封顶在淘汰阈值（不必记到 100） */
export function nextFailStreak(prev: number | undefined, ok: boolean): number {
  if (ok) return 0
  const n = typeof prev === 'number' && Number.isFinite(prev) && prev > 0 ? Math.floor(prev) : 0
  return Math.min(n + 1, PROBE_RETIRE_STREAK)
}

/** 是否已达淘汰阈值（未记录过的镜像不算） */
export function shouldRetire(streak: number | undefined): boolean {
  return typeof streak === 'number' && Number.isFinite(streak) && streak >= PROBE_RETIRE_STREAK
}

/**
 * 从发现结果里挑可自动采纳的镜像：实测可用、清单里没有的，按 compareProbe 排名取前
 * limit 个。返回的是原对象（调用方据此建条目）。
 */
export function pickAutoAdopt<T extends MirrorCandidate & ProbeMetrics>(
  candidates: readonly T[],
  knownPrefixes: readonly string[],
  limit: number = AUTO_ADOPT_LIMIT,
): T[] {
  const known = new Set(knownPrefixes)
  return candidates
    .filter(c => c.ok && !known.has(c.prefix))
    .slice()
    .sort(compareProbe)
    .slice(0, Math.max(0, limit))
}

// ─── git url.insteadOf 全局改写 ───

export const GITHUB_BASE = 'https://github.com/'

/** insteadOf 的子节（"用什么替换"），如 https://ghproxy.net/https://github.com/ */
export function insteadOfSubsection(prefix: string): string {
  return `${prefix}${GITHUB_BASE}`
}

/** 组装 git config --global 读取参数（列出全部 url.*.insteadof 与 pushinsteadof） */
export function buildGitReadArgs(): string[] {
  return ['config', '--global', '--get-regexp', '^url\\..*\\.(push)?insteadof$']
}

export function buildGitSetArgs(prefix: string): string[] {
  return ['config', '--global', `url.${insteadOfSubsection(prefix)}.insteadOf`, GITHUB_BASE]
}

/** 按子节卸载 insteadOf（生产卸载的是「任意生效子节」，不限于本应用前缀推导值） */
export function buildGitUnsetSubsectionArgs(subsection: string): string[] {
  return ['config', '--global', '--unset', `url.${subsection}.insteadOf`]
}

export function buildGitUnsetArgs(prefix: string): string[] {
  return buildGitUnsetSubsectionArgs(insteadOfSubsection(prefix))
}

/**
 * push 直连保护：insteadOf 对 fetch 与 push 都生效（git 实测确认），HTTPS remote 的
 * push 会被一并改写到只读镜像而必然失败。自指 pushInsteadOf（github.com → github.com）
 * 对 push 的优先级高于 insteadOf（git 文档语义），把 push 改写"抵消"为直连原址；
 * fetch 不受影响，继续走镜像。
 */
export function buildGitPushKeepArgs(): string[] {
  return ['config', '--global', `url.${GITHUB_BASE}.pushInsteadOf`, GITHUB_BASE]
}

export function buildGitPushKeepUnsetArgs(): string[] {
  return buildGitPushUnsetSubsectionArgs(GITHUB_BASE)
}

/**
 * push 目的地策略。
 * - direct：自指 pushInsteadOf 把 insteadOf 对 push 的改写“抵消”为直连（旧行为）
 * - ssh：把 HTTPS push 改写成 git@github.com:owner/repo.git（走 SSH；
 *   国内 443 可达性远好于 https 直连，与 ssh443 托管块天然配对）
 * 两种都只在用户自己没有 github.com push 规则时写入。
 */
export type GitPushMode = 'direct' | 'ssh'

/** ssh push 改写的子节（`url.git@github.com:.pushInsteadOf`） */
export const GIT_SSH_PUSH_SUBSECTION = 'git@github.com:'

export function buildGitPushSshArgs(): string[] {
  return ['config', '--global', `url.${GIT_SSH_PUSH_SUBSECTION}.pushInsteadOf`, GITHUB_BASE]
}

export function buildGitPushUnsetSubsectionArgs(subsection: string): string[] {
  return ['config', '--global', '--unset', `url.${subsection}.pushInsteadOf`]
}

export function buildGitPushSshUnsetArgs(): string[] {
  return buildGitPushUnsetSubsectionArgs(GIT_SSH_PUSH_SUBSECTION)
}

/** 是否为本应用写入的 push 规则（两种模式各一个子节） */
export function isManagedPushSubsection(subsection: string): boolean {
  return subsection === GITHUB_BASE || subsection === GIT_SSH_PUSH_SUBSECTION
}

/** 本应用策略对应的 push 子节 */
export function pushSubsectionOf(mode: GitPushMode): string {
  return mode === 'ssh' ? GIT_SSH_PUSH_SUBSECTION : GITHUB_BASE
}

/** 还原用户原有规则（backup.previousValue 非空时调用）：把子节改回旧前缀 */
export function buildGitRestoreArgs(previousSubsection: string): string[] {
  return ['config', '--global', `url.${previousSubsection}.insteadOf`, GITHUB_BASE]
}

export interface InsteadOfEntry {
  /** 完整 key，如 url.https://ghproxy.net/https://github.com/.insteadof（git 输出为小写名） */
  key: string
  /** 子节部分：url. 与 .insteadof 之间，即"加速后的地址基" */
  subsection: string
  /** 被替换的地址基（value） */
  value: string
  /** pushInsteadOf 条目（只改写 push 目的地）；未标记 = insteadOf（fetch 与 push 都改写） */
  push?: boolean
}

/** 解析 `git config --global --get-regexp ^url\..*\.(push)?insteadof$` 的输出（每行 "key value"） */
export function parseGitInsteadOf(stdout: string): InsteadOfEntry[] {
  const out: InsteadOfEntry[] = []
  for (const line of (stdout || '').split(/\r?\n/)) {
    const t = line.trim()
    if (!t) continue
    const sp = t.indexOf(' ')
    if (sp === -1) continue
    const key = t.slice(0, sp)
    const value = t.slice(sp + 1).trim()
    // pushinsteadof 先判（子节里可能含 "push" 字样，靠 .insteadof$ 后缀区分两者）
    const pm = key.match(/^url\.(.+)\.pushinsteadof$/i)
    if (pm) {
      out.push({ key, subsection: pm[1], value, push: true })
      continue
    }
    const m = key.match(/^url\.(.+)\.insteadof$/i)
    if (!m) continue
    out.push({ key, subsection: m[1], value })
  }
  return out
}

/**
 * 取一个代表性 github 地址，用来复现 git 的 insteadOf 选择规则。
 * git 改写 URL 时取“最长匹配前缀”的条目，而不是“第一条”。
 */
export const GITHUB_SAMPLE_URL = 'https://github.com/git/git'

/**
 * 在条目表里找对 url 生效的 insteadOf（跳过 push 条目，按 git 的最长前缀语义）。
 * 旧实现只认 value === GITHUB_BASE，遇到用户加过更长的改写（如按 owner
 * 细分的 `https://github.com/me/`）时判断与 git 实际行为不一致。
 */
export function resolveInsteadOf(entries: InsteadOfEntry[], url: string = GITHUB_SAMPLE_URL): InsteadOfEntry | null {
  let best: InsteadOfEntry | null = null
  for (const e of entries) {
    if (e.push) continue
    if (!e.value || !url.startsWith(e.value)) continue
    if (!best || e.value.length > best.value.length) best = e
  }
  return best
}

/** 从条目表里找“对 github.com 生效”的 insteadOf 条目，没有返回 null */
export function findGithubInsteadOf(entries: InsteadOfEntry[]): InsteadOfEntry | null {
  return resolveInsteadOf(entries)
}

/**
 * 已存在的 github.com push 直连规则（pushInsteadOf value === GITHUB_BASE）。
 * 用户的自建规则（如把 push 重定向到 ssh）也算 —— 它已经决定了 push 去向，
 * 我们的自指规则不应叠加（同 value 双匹配的胜者未定义）。
 */
export function findGithubPushInsteadOf(entries: InsteadOfEntry[]): InsteadOfEntry | null {
  return entries.find(e => e.push && e.value === GITHUB_BASE) ?? null
}

// ─── ~/.ssh/config 托管块（github.com → ssh.github.com:443） ───

export const SSH_BLOCK_BEGIN = '# >>> cKunAgentSSH github-accel >>>'
export const SSH_BLOCK_END = '# <<< cKunAgentSSH github-accel <<<'

export interface SshGResult {
  hostname: string | null
  port: number | null
}

/**
 * 解析 `ssh -G <host>` 的生效配置输出（小写 key + 空白分隔）。
 * 自检用它确认托管块真的把 github.com 解析成 ssh.github.com:443；
 * 缺字段返回 null，由调用方判失败而不是猜默认值。
 */
export function parseSshG(stdout: string): SshGResult {
  let hostname: string | null = null
  let port: number | null = null
  for (const rawLine of (stdout || '').split(/\r?\n/)) {
    const m = /^(\S+)\s+(.+)$/.exec(rawLine.trim())
    if (!m) continue
    const key = m[1].toLowerCase()
    const value = m[2].trim()
    if (key === 'hostname' && value) hostname = value
    else if (key === 'port' && /^\d+$/.test(value)) port = Number(value)
  }
  return { hostname, port }
}

/** GitHub 官方支持的 443 端口 SSH 接入（绕开被 QoS/封禁的 22 端口） */
export function renderSsh443Block(): string {
  return [
    SSH_BLOCK_BEGIN,
    'Host github.com',
    '  HostName ssh.github.com',
    '  Port 443',
    '  User git',
    SSH_BLOCK_END,
  ].join('\n')
}

export type SshBlockState = 'none' | 'ok' | 'broken'

export const SSH_BLOCK_BROKEN_MESSAGE =
  '~/.ssh/config 中的 cKunAgentSSH 托管标记不完整或顺序异常，已停止写入以免损坏配置；请手动清理多余的托管标记后重试'

/**
 * ssh443 托管块三态，判据与 hostsBlockState 相同。broken 包括：只有 BEGIN、只有 END、
 * END 在 BEGIN 前、重复标记。这些状态下按 indexOf 找「第一对标记」会把 broken 文件
 * 落进追加分支 —— 旧的手改块胜出、443 加速实际失效而 UI 仍显示已开启，标记还越积
 * 越多。所以 apply 前停手报错，修复/删除交用户手动确认（对齐 hosts 路径的既定做法）。
 */
export function sshBlockState(existing: string): SshBlockState {
  const begin = existing.indexOf(SSH_BLOCK_BEGIN)
  const end = existing.indexOf(SSH_BLOCK_END)
  if (begin === -1 && end === -1) return 'none'
  if (begin === -1 || end === -1 || end <= begin) return 'broken'
  const extraBegin = existing.indexOf(SSH_BLOCK_BEGIN, begin + SSH_BLOCK_BEGIN.length)
  const extraEnd = existing.indexOf(SSH_BLOCK_END, end + SSH_BLOCK_END.length)
  return extraBegin === -1 && extraEnd === -1 ? 'ok' : 'broken'
}

/**
 * 找到文件里第一个 `Host *`（通配 Host 段）的偏移。
 * OpenSSH 对同名关键字取「首个匹配值」，而 `Host *` 也匹配 github.com；
 * 我们的块追加在它后面就会失效（用户那段 `Host *` 里的 Port/ProxyJump 先落地）。
 */
export function findCatchAllHostOffset(text: string): number {
  const re = /^[ \t]*Host[ \t]+([^\r\n]+)/gim
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const patterns = m[1].trim().toLowerCase().split(/[ \t]+/)
    if (patterns.includes('*')) return m.index
  }
  return -1
}

/**
 * 写入/更新托管块：已有标记块则原位替换；否则插到首个 `Host *` 之前（没有则追加
 * 到末尾）。标记损坏时拒绝写入。
 */
export function applySshBlock(existing: string): string {
  if (sshBlockState(existing) === 'broken') throw new Error(SSH_BLOCK_BROKEN_MESSAGE)
  const block = renderSsh443Block()
  const begin = existing.indexOf(SSH_BLOCK_BEGIN)
  const end = existing.indexOf(SSH_BLOCK_END)
  if (begin !== -1 && end !== -1 && end > begin) {
    return existing.slice(0, begin) + block + existing.slice(end + SSH_BLOCK_END.length)
  }
  const at = findCatchAllHostOffset(existing)
  if (at > 0) {
    // 切在行首：head 必以换行结尾，补一个空行分隔（remove 会把它吃掉）
    const head = existing.slice(0, at)
    const tail = existing.slice(at)
    return head + (head.endsWith('\n') ? '\n' : '\n\n') + block + '\n' + tail
  }
  if (at === 0) {
    return block + '\n' + existing
  }
  const sep = existing.length === 0 || existing.endsWith('\n') ? '' : '\n'
  return existing + sep + (existing.length === 0 ? '' : '\n') + block + '\n'
}

/** 移除托管块（连同块前我们补的空行）；无块则原样返回。 */
export function removeSshBlock(existing: string): string {
  const begin = existing.indexOf(SSH_BLOCK_BEGIN)
  const end = existing.indexOf(SSH_BLOCK_END)
  if (begin === -1 || end === -1 || end <= begin) return existing
  let start = begin
  // 只吃 apply 补的那一行空行：BEGIN 前恰好是两个换行时，去掉后一个。
  // （旧实现最多吃两个换行符，块被插到文件中间时会吞掉上一行的行尾换行。）
  if (start >= 2 && existing.slice(start - 2, start) === '\n\n') start -= 1
  let stop = end + SSH_BLOCK_END.length
  if (existing[stop] === '\r') stop++
  if (existing[stop] === '\n') stop++
  return existing.slice(0, start) + existing.slice(stop)
}

export function hasSshBlock(existing: string): boolean {
  const begin = existing.indexOf(SSH_BLOCK_BEGIN)
  const end = existing.indexOf(SSH_BLOCK_END)
  return begin !== -1 && end !== -1 && end > begin
}

// ─── 系统 hosts 托管块（优选 IP 增强模式，M3） ───

/**
 * hosts 文件按字节解码：Windows 上常用 ANSI/GBK 注释，按 UTF-8 解码会把非 ASCII
 * 字节替换成 U+FFFD 再写回，块外内容被永久改写。latin1 是逐字节映射，配合只增删
 * ASCII 托管块的改写方式，可保证块外字节原样往返。
 */
export function decodeHostsBytes(bytes: Uint8Array): string {
  let out = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    out += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return out
}

export function encodeHostsBytes(text: string): Uint8Array {
  const out = new Uint8Array(text.length)
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 0xff
  return out
}

export const HOSTS_BLOCK_BEGIN = '# >>> cKunAgentSSH github-accel >>>'
export const HOSTS_BLOCK_END = '# <<< cKunAgentSSH github-accel <<<'

/** 块内生成时间戳行的前缀（只认紧跟 BEGIN 的那一行） */
const HOSTS_GENERATED_PREFIX = '# generated: '

/**
 * 优选 IP 的提醒阈值。GitHub 轮换接入 IP 没有公开节奏，取 7 天的理由是"出问题之前
 * 用户会被提醒一次"，而不是宣称 7 天后一定失效。
 */
export const HOSTS_STALE_AFTER_DAYS = 7

/** hosts 托管块覆盖的域名（GitHub 访问的关键面：网页/API/raw/资源/CDN） */
export function githubHostsDomains(): string[] {
  return [
    'github.com',
    'api.github.com',
    'raw.githubusercontent.com',
    'objects.githubusercontent.com',
    'codeload.github.com',
    'github.githubassets.com',
    'camo.githubusercontent.com',
    'user-images.githubusercontent.com',
  ]
}

/** 渲染一行 hosts 记录；IP 必须是合法 IPv4 数字点分（防注入注释/多行） */
export function isValidIPv4(ip: string): boolean {
  const parts = ip.split('.')
  return parts.length === 4 && parts.every(p => /^\d{1,3}$/.test(p) && Number(p) <= 255)
}

/**
 * 社区维护的优选 IP 数据源（GitHub520）。
 * 本地 DoH 只能拿到“当时当地解析到什么”，而 GitHub 在国内的可达 IP 是社区
 * 长期测出来的；两者取并集再本地测速，命中率明显高于单跑 DoH。
 */
export const COMMUNITY_HOSTS_SOURCE = 'https://raw.hellogithub.com/hosts'

/**
 * 解析社区 hosts 文本（`IP<TAB>域名`，可带 # 注释）。
 * 只保留我们托管的域名，非法 IP / 未知域名一律丢弃 —— 这份文本来自第三方，
 * 不能把任意域名写进用户系统 hosts。
 */
export function parseCommunityHosts(
  text: string,
  domains: string[] = githubHostsDomains(),
): { domain: string; ip: string }[] {
  const allowed = new Set(domains.map(d => d.toLowerCase()))
  const seen = new Set<string>()
  const out: { domain: string; ip: string }[] = []
  for (const rawLine of (text || '').split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, '').trim()
    if (!line) continue
    const parts = line.split(/\s+/)
    if (parts.length < 2) continue
    const ip = parts[0]
    if (!isValidIPv4(ip)) continue
    for (const name of parts.slice(1)) {
      const domain = name.toLowerCase()
      if (!allowed.has(domain)) continue
      const key = `${ip} ${domain}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({ domain, ip })
    }
  }
  return out
}

/**
 * 合并两批候选 IP（本地 DoH 优先，社区源补充），去重并限量。
 * 顺序即优先级：测速并发不分先后，但它决定候选被截断时先保留谁。
 */
export function mergeHostCandidates(primary: string[], secondary: string[], limit = 10): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const ip of [...primary, ...secondary]) {
    if (typeof ip !== 'string' || !isValidIPv4(ip) || seen.has(ip)) continue
    seen.add(ip)
    out.push(ip)
    if (out.length >= limit) break
  }
  return out
}

export function renderHostsBlock(entries: { domain: string; ip: string }[], generatedAt: Date = new Date()): string {
  // SEC-001：domain 与 IP 同为硬校验（纵深防御对齐）。原实现只校验 IP，
  // domain 仅小写化 —— IPC 契约（ghaccel:hostsApply）接受渲染层任意输入，
  // domain 带换行可向系统 hosts 注入任意行、带空格可当别名、带 # 可篡改
  // 注释边界。HOST_RE 白名单字符集天然拒绝空白/#//，长度按 DNS 域名上限收口。
  const lines = entries
    .filter(e => {
      if (typeof e.domain !== 'string' || typeof e.ip !== 'string' || !isValidIPv4(e.ip)) return false
      const domain = e.domain.trim().toLowerCase()
      return domain.length <= 253 && HOST_RE.test(domain)
    })
    .map(e => `${e.ip}\t${e.domain.trim().toLowerCase()}`)
  // 时间戳行紧跟 BEGIN：这是块内唯一被 hostsBlockAgeDays 认账的 generated 行，
  // 用户或旧版本在块体里写的同类注释都不会影响时效判定。
  // 拿到的是无效 Date 时宁可不写这行 —— 时效未知会被判成"该重选"，不会谎报新鲜。
  const stamp = generatedAt instanceof Date && Number.isFinite(generatedAt.getTime())
    ? [`${HOSTS_GENERATED_PREFIX}${generatedAt.toISOString()}`]
    : []
  return [HOSTS_BLOCK_BEGIN, ...stamp, ...lines, HOSTS_BLOCK_END].join('\n')
}

export type HostsBlockState = 'none' | 'ok' | 'broken'

export const HOSTS_BLOCK_BROKEN_MESSAGE =
  'hosts 中的 cKunAgentSSH 托管标记不完整或顺序异常，已停止操作以免损坏文件；请手动检查 hosts 后重试'

/**
 * 托管块三态。broken 包括：只有 BEGIN、只有 END、END 在 BEGIN 前、重复标记。
 * 这些状态下按 indexOf 找"第一对标记"会跨过用户内容，写入/移除都可能误删用户的
 * hosts 行，所以宁可停手并明确报错，也不猜。
 */
export function hostsBlockState(existing: string): HostsBlockState {
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  if (begin === -1 && end === -1) return 'none'
  if (begin === -1 || end === -1 || end <= begin) return 'broken'
  const extraBegin = existing.indexOf(HOSTS_BLOCK_BEGIN, begin + HOSTS_BLOCK_BEGIN.length)
  const extraEnd = existing.indexOf(HOSTS_BLOCK_END, end + HOSTS_BLOCK_END.length)
  return extraBegin === -1 && extraEnd === -1 ? 'ok' : 'broken'
}

/** 写入/更新 hosts 托管块（已有标记块原位替换，否则文件尾追加） */
export function applyHostsBlock(
  existing: string,
  entries: { domain: string; ip: string }[],
  generatedAt: Date = new Date(),
): string {
  if (hostsBlockState(existing) === 'broken') throw new Error(HOSTS_BLOCK_BROKEN_MESSAGE)
  const block = renderHostsBlock(entries, generatedAt)
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  if (begin !== -1 && end !== -1 && end > begin) {
    return existing.slice(0, begin) + block + existing.slice(end + HOSTS_BLOCK_END.length)
  }
  // 追加时恒在块前加一个 LF、块后留一个 LF：与 removeHostsBlock 的"各消费一个"
  // 对应，才能做到空文件 / LF / CRLF / 无尾换行四种文件的精确往返。
  if (existing.length === 0) return block + '\n'
  return existing + '\n' + block + '\n'
}

/** 移除 hosts 托管块；无块原样返回，标记损坏时拒绝（否则会跨过用户内容误删） */
export function removeHostsBlock(existing: string): string {
  const state = hostsBlockState(existing)
  if (state === 'broken') throw new Error(HOSTS_BLOCK_BROKEN_MESSAGE)
  if (state === 'none') return existing
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  // 与 applyHostsBlock 的"块前一个 LF、块后一个 LF"严格对应：BEGIN 前只消费一个
  // LF（不碰 CRLF 的 \r，保证 CRLF 文件移除后仍是 CRLF），END 后消费一个换行序列。
  // 旧版本写入的"无尾换行文件 + 两个 LF"会多留一个换行——这是唯一的非精确项，
  // 有意的：多一个空行无害，而猜错会吞用户行。
  let start = begin
  if (start > 0 && existing[start - 1] === '\n') start--
  let stop = end + HOSTS_BLOCK_END.length
  if (existing[stop] === '\r') stop++
  if (existing[stop] === '\n') stop++
  return existing.slice(0, start) + existing.slice(stop)
}

export function hasHostsBlock(existing: string): boolean {
  return hostsBlockState(existing) === 'ok'
}

/**
 * 托管块的生成时间原文（ISO 串）；null = 块里没有可比对的 `# generated:` 行。
 *
 * 只认紧跟 BEGIN 的那一行：块体里出现同类注释（用户手改、旧版本残留）不影响判定。
 *
 * 为什么要时效：优选 IP 是某一次 DoH 解析 + TCP 测速的快照，写进系统 hosts 后
 * 不会自己更新。GitHub 换了接入 IP，这块就从加速器变成阻断器，而用户看不出
 * 任何异常——只有 hosts 文件里多了一段三年前写的注释都算不上的记录。
 */
export function hostsBlockGeneratedAt(existing: string): string | null {
  if (hostsBlockState(existing) !== 'ok') return null
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  const body = existing.slice(begin + HOSTS_BLOCK_BEGIN.length, end)
  const firstLine = body.replace(/^\r?\n/, '').split(/\r?\n/)[0] ?? ''
  if (!firstLine.startsWith(HOSTS_GENERATED_PREFIX)) return null
  const raw = firstLine.slice(HOSTS_GENERATED_PREFIX.length).trim()
  return Number.isFinite(Date.parse(raw)) ? raw : null
}

/** 托管块写入至今的天数；null = 无可比对的生成时间戳（见 hostsBlockGeneratedAt） */
export function hostsBlockAgeDays(existing: string, now: Date = new Date()): number | null {
  const raw = hostsBlockGeneratedAt(existing)
  if (raw === null) return null
  if (!Number.isFinite(now.getTime())) return null
  // 未来时间戳（改过系统时钟）按 0 天处理，不给负数，也不谎报"还能再用几天"
  const diff = now.getTime() - Date.parse(raw)
  return diff <= 0 ? 0 : Math.floor(diff / 86_400_000)
}

/**
 * 是否该提醒用户重选。两种情况判 stale：超过阈值；或时效未知（旧版本写的块、
 * 手改过的块）。未知一律算 stale 是有意的 —— 拿"新鲜"去担保一个无从考证的 IP
 * 才是风险，多提示一次点选的代价只是几个点击。
 */
export function isHostsBlockStale(existing: string, now: Date = new Date()): boolean {
  if (!hasHostsBlock(existing)) return false
  const age = hostsBlockAgeDays(existing, now)
  return age === null || age >= HOSTS_STALE_AFTER_DAYS
}
