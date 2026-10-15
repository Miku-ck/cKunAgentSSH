/**
 * GitHub 加速 —— 纯逻辑核心（无 electron / node 依赖，渲染层与主进程共用）
 *
 * 方案定调（2026-10-13）：用户无服务器，排除 SSH 隧道（-D）与本地反代 MITM，
 * 主线 = A+ 镜像加速套件：
 *   - 网页浏览慢   → 网页镜像站（github.com 整站 host 替换，仅供浏览，不能登录）
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

/** 网页镜像站：github.com 的整站 host 替换，仅供浏览公开仓库（不能登录态操作） */
export interface AccelWebMirror {
  id: string
  name: string
  /** 纯 host，如 kkgithub.com */
  host: string
  builtin: boolean
  enabled: boolean
}

export interface GithubAccelConfig {
  proxies: AccelProxy[]
  webMirrors: AccelWebMirror[]
  /**
   * insteadOf 接管备份：开启时若用户已有其它 github.com 改写规则，先记下原值，
   * 关闭时还原（previousValue 为 null 表示原本没有规则，关闭即删除）。
   * 「当前是否激活」以 git config 实查为准，这里只存接管凭证。
   */
  gitBackup: { prefix: string; previousValue: string | null } | null
  /** 一键加速使用的镜像前缀（记住上次选择） */
  lastProxyPrefix?: string
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

/** 内置网页镜像站（2026-10-14 实测校准：kkgithub/bgithub 已死——前者证书易主、后者超时，换成 gitcode） */
export function defaultWebMirrors(): AccelWebMirror[] {
  return [
    { id: 'bw-gitcode', name: 'gitcode', host: 'gitcode.com', builtin: true, enabled: true },
  ]
}

export function defaultGithubAccelConfig(): GithubAccelConfig {
  return { proxies: defaultProxies(), webMirrors: defaultWebMirrors(), gitBackup: null }
}

// ─── 配置校验（主进程 save 防线；UI 另有一层） ───

const HOST_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/

/** 校验代理前缀；合法返回 null，非法返回中文原因（IPC 直接透传给用户） */
export function validateProxyPrefix(prefix: string): string | null {
  if (typeof prefix !== 'string' || !prefix.trim()) return '镜像前缀不能为空'
  const p = prefix.trim()
  if (!p.startsWith('https://')) return '镜像前缀必须以 https:// 开头（明文 http 会把代码流量暴露给链路）'
  if (!p.endsWith('/')) return '镜像前缀必须以 / 结尾'
  try {
    const u = new URL(p)
    if (u.pathname !== '/') return '镜像前缀不应带路径（形如 https://host/ 即可）'
  } catch {
    return '镜像前缀不是合法 URL'
  }
  return null
}

export function validateWebMirrorHost(host: string): string | null {
  if (typeof host !== 'string' || !host.trim()) return '镜像站域名不能为空'
  if (!HOST_RE.test(host.trim().toLowerCase())) return '镜像站域名格式不正确'
  return null
}

// ─── GitHub URL 解析 ───

export type GhUrlType =
  | 'repo'          // 仓库主页 / git@ 形式 / .git 结尾 → 可 clone
  | 'page'          // 仓库内页面（tree/blob/issues/pull/commit…）→ 可浏览、可推导 clone 地址
  | 'release-asset' // releases/download/<tag>/<file> → 可下载
  | 'archive'       // archive/<ref>.tar.gz 或 codeload 链接 → 可下载
  | 'raw'           // raw.githubusercontent.com/<o>/<r>/<ref>/<path> → 可下载
  | 'unknown'

export interface ParsedGhUrl {
  type: GhUrlType
  /** 规范化后的原始地址（https 形式；git@ 已折算） */
  normalized: string
  owner?: string
  repo?: string
  /** releases/download 之后的路径段（tag/file…），仅 release-asset */
  assetPath?: string
}

const PAGE_SEGMENTS = new Set(['tree', 'blob', 'issues', 'pull', 'pulls', 'commit', 'commits', 'actions', 'wiki', 'settings', 'releases', 'tags', 'branches', 'network', 'graphs', 'security'])

/**
 * 解析用户粘贴的 GitHub 地址。宽容输入：
 * https://github.com/o/r(.git)(/…)、git@github.com:o/r(.git)、
 * raw.githubusercontent.com、codeload、ssh://git@github.com/o/r。
 */
export function parseGithubUrl(input: string): ParsedGhUrl {
  const raw = (input || '').trim()
  if (!raw) return { type: 'unknown', normalized: '' }

  // git@github.com:owner/repo(.git) 或 ssh://git@github.com/owner/repo(.git)
  const sshScp = /^git@github\.com:([^/]+)\/([^/]+?)(\.git)?$/i.exec(raw)
  const sshUrl = /^ssh:\/\/git@github\.com\/([^/]+)\/([^/]+?)(\.git)?$/i.exec(raw)
  const sshM = sshScp || sshUrl
  if (sshM) {
    const owner = sshM[1]
    const repo = sshM[2]
    return { type: 'repo', normalized: `https://github.com/${owner}/${repo}`, owner, repo }
  }

  let u: URL
  try {
    u = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`)
  } catch {
    return { type: 'unknown', normalized: raw }
  }
  const host = u.hostname.toLowerCase()
  const segs = u.pathname.split('/').filter(Boolean)

  if (host === 'raw.githubusercontent.com') {
    if (segs.length < 4) return { type: 'unknown', normalized: raw }
    return { type: 'raw', normalized: `https://raw.githubusercontent.com/${segs.join('/')}`, owner: segs[0], repo: segs[1] }
  }

  if (host === 'codeload.github.com') {
    // codeload.github.com/o/r/tar.gz/<ref> → 归并到仓库 archive
    if (segs.length < 2) return { type: 'unknown', normalized: raw }
    return { type: 'archive', normalized: `https://codeload.github.com/${segs.join('/')}`, owner: segs[0], repo: segs[1] }
  }

  if (host !== 'github.com' && host !== 'www.github.com') {
    return { type: 'unknown', normalized: raw }
  }
  if (segs.length < 2) return { type: 'unknown', normalized: raw }

  const owner = segs[0]
  const repo = segs[1].replace(/\.git$/i, '')
  const rest = segs.slice(2)

  if (rest.length === 0) {
    return { type: 'repo', normalized: `https://github.com/${owner}/${repo}`, owner, repo }
  }
  if (rest[0] === 'releases' && rest[1] === 'download' && rest.length >= 4) {
    const assetPath = rest.slice(2).join('/')
    return {
      type: 'release-asset',
      normalized: `https://github.com/${owner}/${repo}/releases/download/${assetPath}`,
      owner, repo, assetPath,
    }
  }
  if (rest[0] === 'archive' && rest.length >= 2) {
    return { type: 'archive', normalized: `https://github.com/${owner}/${repo}/archive/${rest.slice(1).join('/')}`, owner, repo }
  }
  if (PAGE_SEGMENTS.has(rest[0])) {
    return { type: 'page', normalized: `https://github.com/${[owner, repo, ...rest].join('/')}`, owner, repo }
  }
  // 未识别的二级路径按页面处理（比 unknown 更实用：至少能给镜像站跳转）
  return { type: 'page', normalized: `https://github.com/${[owner, repo, ...rest].join('/')}`, owner, repo }
}

// ─── 加速链接生成 ───

export interface AccelLink {
  /** clone = git clone 用；download = 文件下载用；web = 浏览器打开 */
  kind: 'clone' | 'download' | 'web'
  label: string
  url: string
  mirrorId: string
  mirrorName: string
}

export interface AccelLinkSet {
  links: AccelLink[]
  /** 未加速的规范地址（UI 对照展示 / 复制原始命令用） */
  canonical: { cloneUrl?: string; webUrl?: string; downloadUrl?: string }
}

/**
 * 按 URL 类型产出加速链接：
 * - repo/page → 每个启用代理一条 clone 链接 + 每个启用镜像站一条 web 链接
 * - release-asset/archive/raw → 每个启用代理一条 download 链接
 * - unknown → 空（UI 提示无法识别）
 */
export function buildAccelLinks(parsed: ParsedGhUrl, proxies: AccelProxy[], webMirrors: AccelWebMirror[]): AccelLinkSet {
  const links: AccelLink[] = []
  const canonical: AccelLinkSet['canonical'] = {}
  if (parsed.type === 'unknown' || !parsed.owner || !parsed.repo) return { links, canonical }

  const onProxies = proxies.filter(p => p.enabled)
  const onWeb = webMirrors.filter(w => w.enabled)

  if (parsed.type === 'repo' || parsed.type === 'page') {
    const cloneUrl = `https://github.com/${parsed.owner}/${parsed.repo}.git`
    canonical.cloneUrl = cloneUrl
    canonical.webUrl = parsed.normalized
    for (const p of onProxies) {
      links.push({ kind: 'clone', label: `git clone（${p.name}）`, url: `${p.prefix}${cloneUrl}`, mirrorId: p.id, mirrorName: p.name })
    }
    for (const w of onWeb) {
      const path = parsed.type === 'page' ? new URL(parsed.normalized).pathname : `/${parsed.owner}/${parsed.repo}`
      links.push({ kind: 'web', label: `网页浏览（${w.name}）`, url: `https://${w.host}${path}`, mirrorId: w.id, mirrorName: w.name })
    }
    return { links, canonical }
  }

  // 下载类（release-asset / archive / raw）：代理前缀直连
  canonical.downloadUrl = parsed.normalized
  for (const p of onProxies) {
    links.push({ kind: 'download', label: `加速下载（${p.name}）`, url: `${p.prefix}${parsed.normalized}`, mirrorId: p.id, mirrorName: p.name })
  }
  return { links, canonical }
}

/**
 * 测速探测地址：前缀 + 一个真实可代理的 raw 文件（实测校准——ghproxy 类服务
 * 不代理 github 首页，打首页只会得到 403/404，把活镜像误判成失效）。
 * git/git 仓库的 README 稳定存在，直连与代理都可达。
 */
export const PROBE_TARGET = 'https://raw.githubusercontent.com/git/git/master/README.md'

export function buildProbeUrl(prefix: string): string {
  return `${prefix}${PROBE_TARGET}`
}

// ─── git url.insteadOf 全局改写 ───

export const GITHUB_BASE = 'https://github.com/'

/** insteadOf 的子节（"用什么替换"），如 https://ghproxy.net/https://github.com/ */
export function insteadOfSubsection(prefix: string): string {
  return `${prefix}${GITHUB_BASE}`
}

/** 组装 git config --global 读取参数（列出全部 url.*.insteadof） */
export function buildGitReadArgs(): string[] {
  return ['config', '--global', '--get-regexp', '^url\\..*\\.insteadof$']
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
}

/** 解析 `git config --global --get-regexp ^url\..*\.insteadof$` 的输出（每行 "key value"） */
export function parseGitInsteadOf(stdout: string): InsteadOfEntry[] {
  const out: InsteadOfEntry[] = []
  for (const line of (stdout || '').split(/\r?\n/)) {
    const t = line.trim()
    if (!t) continue
    const sp = t.indexOf(' ')
    if (sp === -1) continue
    const key = t.slice(0, sp)
    const value = t.slice(sp + 1).trim()
    const m = /^url\.(.+)\.insteadof$/i.exec(key)
    if (!m) continue
    out.push({ key, subsection: m[1], value })
  }
  return out
}

/** 从条目表里找"对 github.com 生效"的那条（value === GITHUB_BASE），没有返回 null */
export function findGithubInsteadOf(entries: InsteadOfEntry[]): InsteadOfEntry | null {
  return entries.find(e => e.value === GITHUB_BASE) ?? null
}

// ─── ~/.ssh/config 托管块（github.com → ssh.github.com:443） ───

export const SSH_BLOCK_BEGIN = '# >>> cKunAgentSSH github-accel >>>'
export const SSH_BLOCK_END = '# <<< cKunAgentSSH github-accel <<<'

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

/** 写入/更新托管块：已有标记块则原位替换，否则追加到文件末尾。 */
export function applySshBlock(existing: string): string {
  const block = renderSsh443Block()
  const begin = existing.indexOf(SSH_BLOCK_BEGIN)
  const end = existing.indexOf(SSH_BLOCK_END)
  if (begin !== -1 && end !== -1 && end > begin) {
    return existing.slice(0, begin) + block + existing.slice(end + SSH_BLOCK_END.length)
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
  // 吃掉块前相邻的空白行（apply 时补的那行）
  while (start > 0 && (existing[start - 1] === '\n' || existing[start - 1] === '\r')) {
    // 保留一个换行作为上一段的结束
    if (begin - start >= 2) break
    start--
  }
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

/** 写入/更新 hosts 托管块（已有标记块原位替换，否则文件尾追加） */
export function applyHostsBlock(
  existing: string,
  entries: { domain: string; ip: string }[],
  generatedAt: Date = new Date(),
): string {
  const block = renderHostsBlock(entries, generatedAt)
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  if (begin !== -1 && end !== -1 && end > begin) {
    return existing.slice(0, begin) + block + existing.slice(end + HOSTS_BLOCK_END.length)
  }
  const sep = existing.length === 0 || existing.endsWith('\n') ? '' : '\n'
  return existing + sep + (existing.length === 0 ? '' : '\n') + block + '\n'
}

/** 移除 hosts 托管块；无块原样返回 */
export function removeHostsBlock(existing: string): string {
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  if (begin === -1 || end === -1 || end <= begin) return existing
  let start = begin
  while (start > 0 && (existing[start - 1] === '\n' || existing[start - 1] === '\r') && begin - start < 2) start--
  let stop = end + HOSTS_BLOCK_END.length
  if (existing[stop] === '\r') stop++
  if (existing[stop] === '\n') stop++
  return existing.slice(0, start) + existing.slice(stop)
}

export function hasHostsBlock(existing: string): boolean {
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  return begin !== -1 && end !== -1 && end > begin
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
  const begin = existing.indexOf(HOSTS_BLOCK_BEGIN)
  const end = existing.indexOf(HOSTS_BLOCK_END)
  if (begin === -1 || end === -1 || end <= begin) return null
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
