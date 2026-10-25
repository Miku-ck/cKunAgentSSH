/**
 * GitHub 加速 —— 内置下载器（主进程）
 *
 * 经镜像 URL 流式落盘：<最终名>.<URL指纹>.ckpart 部分文件（同 URL 重复下载时按已下字节
 * Range 续传，服务端返回 200/416 则全量重下；不同 URL 的同名文件互不串档），完成后
 * rename 为最终文件名（冲突自动加序号，不静默覆盖）；取消/出错保留 .ckpart 供下次续传。
 * 进度节流推送（~4/s）；字节计数始终用原始响应（gunzip 解压流不计入，口径与
 * content-length 一致，进度条才不会错乱）。
 * 落盘目录固定为用户下载目录下的 github-accel 子目录，渲染层只能发起/取消，
 * 不能指定落盘路径。
 */
import { app } from 'electron'
import { join, extname } from 'path'
import { createWriteStream } from 'fs'
import { createHash } from 'crypto'
import { stat, mkdir, access, rename, readdir, readFile, unlink, writeFile } from 'fs/promises'
import https from 'https'
import { createGunzip } from 'zlib'
import { decideResume, parseContentRangeTotal, PARTIAL_MAX_AGE_DAYS, type PartialMeta } from './utils/resume-policy'

export type DownloadState = 'downloading' | 'done' | 'error' | 'cancelled'

export interface DownloadStatus {
  id: string
  state: DownloadState
  /** 落盘绝对路径（done 时有效） */
  path?: string
  fileName?: string
  transferred: number
  /** 未知（chunked / 续传前未探测）时为 0 */
  total: number
  error?: string
}

export interface DownloadHandle {
  status: DownloadStatus
  /** 发起时的 URL（同 URL 去重判据） */
  url: string
  cancel: () => void
}

/** 进度节流间隔：下载可能几百 MB，每 chunk 推一条会把 IPC 打爆 */
const PROGRESS_PUSH_INTERVAL_MS = 250

/**
 * 并发下载任务上限（Q-E4）。渲染层可一次勾选多个 release 资产，无上限会把
 * 带宽、socket 与内存（每个任务一路 https 响应流）一起打满。上限纪律对齐仓内
 * PTY 32（MAX_PTY_SESSIONS）的做法：命名常量 + 超限拒绝并回执（渲染层收到报错
 * 文案），不做隐式排队 —— 排队会把"哪些在下载"变成另一个需要回执的状态面。
 */
const MAX_CONCURRENT_DOWNLOADS = 16

/**
 * GitHub 下载链路域名白名单（Q-E4）：命中即放行。列白名单不是为了独裁 ——
 * 本下载器的入口 URL 带用户自配镜像前缀（ghaccel 镜像清单），白名单之外的自定义
 * 镜像域只要不是内网/保留地址（isBlockedOutboundHost）同样放行；列出来是让
 * "哪些域算官方下载链路"有单一事实来源，且官方域不必逐项过黑名单判定。
 */
const GITHUB_DOWNLOAD_HOSTS = new Set([
  'github.com',
  'api.github.com',
  'codeload.github.com',
  'objects.githubusercontent.com',
  'release-assets.githubusercontent.com',
  'github-releases.githubusercontent.com',
  'raw.githubusercontent.com',
  'gist.githubusercontent.com',
  'github-cloud.s3.amazonaws.com',
])

function isGithubDownloadHost(host: string): boolean {
  if (GITHUB_DOWNLOAD_HOSTS.has(host)) return true
  for (const d of GITHUB_DOWNLOAD_HOSTS) {
    if (host.endsWith(`.${d}`)) return true
  }
  return false
}

/** IPv4 四元组是否落在私有/保留段（0/8、10/8、127/8、100.64/10、169.254/16、
 *  172.16/12、192.0.0/24、192.168/16、198.18/15、198.51.100/24、203.0.113/24、
 *  224/4 组播、240/4 保留与 255.255.255.255 广播）。判据全部是前缀段，
 *  第 4 字节不参与（255.255.255.255 由 a>=224 覆盖）。 */
function ipv4Blocked(a: number, b: number, c: number, _d: number): boolean {
  if (a === 0 || a === 10 || a === 127) return true
  if (a === 100 && b >= 64 && b <= 127) return true
  if (a === 169 && b === 254) return true
  if (a === 172 && b >= 16 && b <= 31) return true
  if (a === 192 && (b === 0 || b === 168)) return true
  if (a === 198 && (b === 18 || b === 19)) return true
  if (a === 198 && b === 51 && c === 100) return true
  if (a === 203 && b === 0 && c === 113) return true
  return a >= 224
}

/**
 * host 是否为 localhost / 环回 / 私有 / 保留地址（Q-E4）。
 *
 * 入参用 new URL 解析后的 hostname：WHATWG URL 解析器会把非常规编码写法归一化
 * （实测 0x7f000001 / 2130706433 / 0177.0.0.1 / 127.1 均归一成点分十进制，
 * ::ffff:127.0.0.1 归一成 ::ffff:7f00:1，末尾根点与大小写也归一），这里再按
 * 归一化后的形态统一判定，两种字面量族都拦。
 */
function isBlockedOutboundHost(hostname: string): boolean {
  let host = hostname.toLowerCase()
  while (host.endsWith('.')) host = host.slice(0, -1)
  if (!host) return true
  if (host === 'localhost' || host.endsWith('.localhost') || host === 'localhost.localdomain'
      || host.endsWith('.local') || host.endsWith('.localdomain')) return true

  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/)
  if (v4) return ipv4Blocked(Number(v4[1]), Number(v4[2]), Number(v4[3]), Number(v4[4]))

  // IPv6 字面量（hostname 保留方括号）
  if (host.startsWith('[') && host.endsWith(']')) {
    let v6 = host.slice(1, -1)
    const pct = v6.indexOf('%')
    if (pct >= 0) v6 = v6.slice(0, pct)  // 去 zone index（%25eth0）
    // IPv4-mapped（::ffff:a.b.c.d / 归一化的 ::ffff:hex:hex）：取出内嵌 IPv4 按同一规则判
    const mapped = v6.match(/^::ffff:(.+)$/)
    if (mapped) {
      const dotted = mapped[1].match(/^(\d{1,3}(?:\.\d{1,3}){3})$/)
      if (dotted) return isBlockedOutboundHost(dotted[1])
      const hexes = mapped[1].split(':')
      if (hexes.length === 2 && hexes.every(h => /^[0-9a-f]{1,4}$/.test(h))) {
        const hi = parseInt(hexes[0], 16)
        const lo = parseInt(hexes[1], 16)
        return ipv4Blocked(hi >> 8, hi & 0xff, lo >> 8, lo & 0xff)
      }
      return true  // ::ffff: 后跟不认识的形态：一律按内网/保留处理
    }
    if (v6 === '::' || v6 === '::1') return true  // 未指定 / 环回
    const parts = v6.split(':')
    const h = parseInt(parts[0] || '0', 16)
    if ((h >= 0xfc00 && h <= 0xfdff)   // fc00::/7 ULA
      || (h >= 0xfe80 && h <= 0xfebf)  // fe80::/10 链路本地
      || h >= 0xff00                   // ff00::/8 组播
      || h === 0x0100) return true     // 100::/64 discard-only
    if (h === 0x2001 && parseInt(parts[1] || '0', 16) === 0x0db8) return true  // 2001:db8::/32 文档段
    if (h === 0x0064 && parseInt(parts[1] || '0', 16) === 0xff9b) return true  // 64:ff9b::/96 本地翻译
    if (h === 0x2002) {
      // 6to4（2002::/16）内嵌 IPv4 在第 2、3 个 hextet：2002:7f00:1:: == 127.0.0.1
      const h2 = parseInt(parts[1] || '0', 16)
      const h3 = parseInt(parts[2] || '0', 16)
      return ipv4Blocked(h2 >> 8, h2 & 0xff, h3 >> 8, h3 & 0xff)
    }
    return false
  }
  return false
}

function genId(): string {
  return `dl-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

/** 从 URL 提取文件名；无有效末段时用降级名 */
export function fileNameFromUrl(url: string): string {
  try {
    const u = new URL(url)
    const last = u.pathname.split('/').filter(Boolean).pop() || ''
    const name = decodeURIComponent(last).replace(/[\\/:*?"<>|]/g, '_')
    return name || 'download.bin'
  } catch {
    return 'download.bin'
  }
}

/** 目标文件已存在时追加 .1/.2… 序号，避免静默覆盖 */
async function uniquePath(dir: string, name: string): Promise<string> {
  const ext = extname(name)
  const stem = name.slice(0, name.length - ext.length)
  for (let i = 0; ; i++) {
    const candidate = join(dir, i === 0 ? name : `${stem}.${i}${ext}`)
    try {
      await access(candidate)
    } catch {
      return candidate
    }
  }
}

/**
 * 分片元数据的旁路文件名。之所以要旁证而不是只信 `.ckpart` 的字节数：
 * 同一 URL 背后的资产可能被重新上传，字节前缀看着连续、拼起来却是坏的，
 * 而下载器会报"完成"。etag / last-modified / 当时的总长记在这里，续传前
 * 交给 decideResume 做判据（见 utils/resume-policy.ts）。
 */
export function partialMetaPath(partialPath: string): string {
  return `${partialPath}.meta.json`
}

/** 读分片元数据；文件缺失、JSON 损坏、结构不对一律 null（= 不认这条续传路径） */
export async function readPartialMeta(partialPath: string): Promise<PartialMeta | null> {
  let raw: string
  try {
    raw = await readFile(partialMetaPath(partialPath), 'utf-8')
  } catch {
    return null
  }
  try {
    const data = JSON.parse(raw) as Partial<PartialMeta>
    if (!data || typeof data !== 'object') return null
    const meta: PartialMeta = {}
    if (typeof data.etag === 'string') meta.etag = data.etag
    if (typeof data.lastModified === 'string') meta.lastModified = data.lastModified
    if (typeof data.total === 'number' && Number.isFinite(data.total)) meta.total = data.total
    return Object.keys(meta).length ? meta : null
  } catch {
    return null
  }
}

/** 写分片元数据（尽力而为：写失败只是丢掉续传资格，绝不影响正在进行的下载） */
export async function writePartialMeta(partialPath: string, meta: PartialMeta): Promise<void> {
  try {
    await writeFile(partialMetaPath(partialPath), JSON.stringify(meta), 'utf-8')
  } catch (e) {
    console.warn('[ghaccel] 分片元数据写入失败（不影响下载，仅失去下次续传资格）:', (e as Error).message)
  }
}

/** 删除分片元数据；不存在视为成功 */
export async function dropPartialMeta(partialPath: string): Promise<void> {
  try {
    await unlink(partialMetaPath(partialPath))
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.warn('[ghaccel] 分片元数据删除失败:', (e as Error).message)
    }
  }
}

export class GhDownloadManager {
  private active = new Map<string, DownloadHandle>()
  /** 进度推送（throttle 后）与状态迁移共用一个通道：ghaccel:download */
  private send: (data: DownloadStatus) => void = () => undefined

  setSender(fn: (data: DownloadStatus) => void): void {
    this.send = fn
  }

  /**
   * 出站 URL 校验（Q-E4）：仅 https + 请求前 new URL 解析并校验 host。
   * host 命中 GitHub 下载链路白名单直接放行；其余 host 拒绝 localhost、环回、
   * 私有与保留地址（IPv4/IPv6 字面量与非常规编码写法，见 isBlockedOutboundHost），
   * 自定义镜像的公网域放行。302 重定向的每一跳经 fetchLoop → fetchOnce 里的
   * assertUrl 复检同一校验。
   */
  private assertUrl(url: string): void {
    if (typeof url !== 'string' || !/^https:\/\//i.test(url)) throw new Error('仅支持 https 下载地址')
    // 解析失败（畸形 URL / 畸形 Location）一律拒绝，不做宽松放行
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      throw new Error('下载地址无法解析')
    }
    if (parsed.protocol !== 'https:') throw new Error('仅支持 https 下载地址')
    const host = parsed.hostname.toLowerCase()
    if (isGithubDownloadHost(host)) return
    if (isBlockedOutboundHost(host)) {
      throw new Error(`不允许的下载地址（内网/保留地址）: ${host}`)
    }
  }

  list(): DownloadStatus[] {
    return [...this.active.values()].map(h => h.status)
  }

  statusOf(id: string): DownloadStatus | null {
    return this.active.get(id)?.status ?? null
  }

  /** 发起下载：返回初始状态（含 id） */
  async start(url: string, opts?: { gunzip?: boolean }): Promise<DownloadStatus> {
    this.assertUrl(url)
    // 同一 URL 已有在途任务时拒绝：两路并发按 'a' 追加写同一个 .ckpart，字节交错
    // 必然损坏，进度统计也互相踩。文件名相同但 URL 不同不受影响（partialPath 带指纹）。
    if ([...this.active.values()].some(h => h.url === url && h.status.state === 'downloading')) {
      throw new Error('该文件已在下载中，请在「下载任务」里查看进度')
    }
    // 并发任务数上限（Q-E4）：超限拒绝并回执。检查与 set 之间没有 await，
    // 并发 start 不会同时穿过这道门（与 pty 上限的检查-占用竞态同型，这里天然安全）。
    const activeCount = [...this.active.values()].filter(h => h.status.state === 'downloading').length
    if (activeCount >= MAX_CONCURRENT_DOWNLOADS) {
      throw new Error(`下载任务数已达上限（${MAX_CONCURRENT_DOWNLOADS}），请等待部分任务完成后再试`)
    }
    const id = genId()
    const status: DownloadStatus = {
      id,
      state: 'downloading',
      transferred: 0,
      total: 0,
      fileName: fileNameFromUrl(url),
    }
    const handle: DownloadHandle = {
      status,
      url,
      cancel: () => this.cancel(id),
    }
    this.active.set(id, handle)
    // 异步执行，不阻塞 IPC 返回；首条状态立即推送
    this.send({ ...status })
    void this.run(id, url, opts?.gunzip === true).catch((e: unknown) => {
      this.finish(id, 'error', e instanceof Error ? e.message : String(e))
    })
    return { ...status }
  }

  cancel(id: string): void {
    const h = this.active.get(id)
    if (!h) return
    // 置标记后由数据流/循环检查点中断；终态条目直接忽略
    if (h.status.state === 'downloading') h.status.state = 'cancelled'
  }

  /**
   * 终态落定。规则：'cancelled' 是用户意志，不被随后流销毁产生的 'error'
   * （premature close 之类）覆盖——状态仍为 cancelled，错误信息仅记录。
   */
  private finish(id: string, state: DownloadState, error?: string): void {
    const h = this.active.get(id)
    if (!h) return
    if (h.status.state === 'cancelled' && state === 'error') {
      h.status.error = error
    } else {
      h.status.state = state
      if (error) h.status.error = error
    }
    this.send({ ...h.status })
    // 终态条目 5 分钟后从内存清掉（UI 回看窗口），避免无限增长
    setTimeout(() => this.active.delete(id), 5 * 60 * 1000).unref?.()
  }

  private async run(id: string, url: string, gunzip: boolean): Promise<void> {
    const h = this.active.get(id)
    if (!h) return
    const dir = join(app.getPath('downloads'), 'github-accel')
    await mkdir(dir, { recursive: true })
    // 失败/取消的分片长期保留供续传，所以必须配一条到期规则，否则
    // Downloads/github-accel 只会单向变大。每进程扫一次，不阻塞本次下载。
    this.pruneStaleOnce(dir)

    // 部分文件名含 URL 指纹（<名>.<sha1(url)前8位>.ckpart）：防止不同 URL 的同名文件
    // （如两个仓库的 main.zip、两个 release 的 source.zip）互相按字节续传出损坏内容；
    // 同 URL 重复下载时命中同一部分文件，按已下字节 Range 续传。
    const urlKey = createHash('sha1').update(url).digest('hex').slice(0, 8)
    const partialPath = join(dir, `${h.status.fileName}.${urlKey}.ckpart`)
    let resumeFrom = 0
    try {
      resumeFrom = (await stat(partialPath)).size
    } catch { /* 无部分文件，从 0 开始 */ }
    // gunzip 落盘的是解压后内容，与 Range 的原始字节偏移不同口径，续传会错位——不支持
    if (gunzip) resumeFrom = 0
    // 续传资格看旁证而不是"磁盘上有分片"：分片在但元数据缺失/损坏（旧版本写的、
    // 被人清掉的）时无从证明这批字节来自同一份内容，按没有分片处理、全量重下。
    const meta = resumeFrom > 0 ? await readPartialMeta(partialPath) : null
    if (resumeFrom > 0 && !meta) resumeFrom = 0
    if (resumeFrom > 0) {
      // 续传：已下字节计入进度
      h.status.transferred = resumeFrom
    }

    const finalState = await this.fetchLoop(id, url, partialPath, resumeFrom, gunzip, meta)
    if (finalState === 'done') {
      const h2 = this.active.get(id)
      if (!h2 || h2.status.state === 'cancelled') {
        this.finish(id, 'cancelled')
        return
      }
      // `stream.on('finish')` 只说明"这条流读到头了、没再抛错"，不说明读全了：
      // 中途被掐断而没有触发 error 的响应（反向代理收尾、上游提前 end）照样走到这里，
      // 下面就要 rename 成最终名并 report "done" —— 用户拿到一个尺寸看着正常、内容缺一截
      // 的包，损坏发生在任何一次报错之前。远端给过总长就必须对得上才允许落最终名。
      const { total, transferred } = h2.status
      if (Number.isFinite(total) && total > 0 && transferred !== total) {
        // 不 rename、也不清 .ckpart：这次没读全不代表远端换了内容，
        // 下次仍交给续传判定（decideResume）去决定是补齐还是重来。
        this.finish(id, 'error', `下载不完整（收到 ${transferred}/${total} 字节），分片已保留`)
        return
      }
      // 完成才落最终名；失败/取消保留 .ckpart 供下次续传
      const finalPath = await uniquePath(dir, h2.status.fileName!)
      try {
        await rename(partialPath, finalPath)
        h2.status.path = finalPath
      } catch (e: any) {
        this.finish(id, 'error', `重命名下载文件失败: ${e?.message ?? e}`)
        return
      }
      // 分片已被 rename 带走，元数据留在目录里就是孤儿；下次同名下载会因
      // "有分片名而无元数据"走全量重下，正是要的行为
      await dropPartialMeta(partialPath)
      this.finish(id, 'done')
    }
  }

  /** 每进程只扫一次的目录集合（见 run 里的 pruneStaleOnce 调用点） */
  private prunedDirs = new Set<string>()

  private pruneStaleOnce(dir: string): void {
    if (this.prunedDirs.has(dir)) return
    this.prunedDirs.add(dir)
    void this.pruneStalePartials(dir).catch((e: unknown) => {
      // 清理失败不该影响下载：超龄分片最多是占地方，不会写错内容
      console.warn('[ghaccel] 超龄分片清理失败（不影响下载）:', e instanceof Error ? e.message : String(e))
    })
  }

  /**
   * 删除超过 PARTIAL_MAX_AGE_DAYS 的未完成分片及其元数据，返回清掉的分片数。
   *
   * 只认我们自己写的 `.ckpart` / `.ckpart.meta.json` 后缀 —— 这个目录在用户的
   * 下载目录下，目录里任何别的东西都不该被"清理"名义删掉。
   * （测试里按运行时取用本方法，不为测试把它公开成对外 API）
   */
  private async pruneStalePartials(dir: string, now = Date.now()): Promise<number> {
    const names = await readdir(dir)
    const cutoff = now - PARTIAL_MAX_AGE_DAYS * 86_400_000
    let removed = 0
    for (const name of names) {
      const isPartial = name.endsWith('.ckpart')
      if (!isPartial && !name.endsWith('.ckpart.meta.json')) continue
      const fp = join(dir, name)
      let mtimeMs: number
      try {
        mtimeMs = (await stat(fp)).mtimeMs
      } catch {
        continue // 读不到时间就不删，留给下次
      }
      if (mtimeMs >= cutoff) continue
      try {
        await unlink(fp)
        if (isPartial) {
          removed++
          await dropPartialMeta(fp)
        }
      } catch (e) {
        console.warn('[ghaccel] 删除超龄分片失败:', (e as Error).message)
      }
    }
    if (removed) console.log(`[ghaccel] 清理 ${removed} 个超过 ${PARTIAL_MAX_AGE_DAYS} 天的未完成分片`)
    return removed
  }

  /** 下载循环：'retry-whole'（服务端不支持 Range / 416 / 内容已变更）时清零重下，其余终态即返回 */
  private async fetchLoop(
    id: string, url: string, partialPath: string, resumeFrom: number, gunzip: boolean,
    meta: PartialMeta | null,
    redirectsLeft = 5,
  ): Promise<'done' | 'cancelled' | 'error'> {
    for (;;) {
      const h = this.active.get(id)
      if (!h) return 'cancelled'
      if (h.status.state === 'cancelled') {
        this.finish(id, 'cancelled')
        return 'cancelled'
      }
      const result = await this.fetchOnce(id, url, partialPath, resumeFrom, gunzip, meta, redirectsLeft)
      if (result !== 'retry-whole') return result
      // 丢弃已下部分，从头重下：要么服务端不认 Range，要么 decideResume 判定
      // 这批字节已不可信（内容被换过）。重下会带着新响应重写元数据，旧 meta 无需保留
      resumeFrom = 0
      h.status.transferred = 0
    }
  }

  private fetchOnce(
    id: string, url: string, partialPath: string, resumeFrom: number, gunzip: boolean,
    meta: PartialMeta | null,
    redirectsLeft: number,
  ): Promise<'done' | 'retry-whole' | 'cancelled' | 'error'> {
    return new Promise((resolvePromise) => {
      const h = this.active.get(id)
      if (!h) return resolvePromise('cancelled')

      const headers: Record<string, string> = {
        'User-Agent': 'cKunAgentSSH-ghaccel-downloader',
        'Accept-Encoding': 'identity', // 镜像常把压缩编码搞坏；这里不要压缩传输
      }
      if (resumeFrom > 0) {
        headers.Range = `bytes=${resumeFrom}-`
        // If-Range 让合规服务端自己表态：校验子不匹配就必须回 200 全量，
        // 于是"内容变了"这件事变成一次可观测的状态码，而不是靠猜。
        const validator = meta?.etag || meta?.lastModified
        if (validator) headers['If-Range'] = validator
      }

      const req = https.get(url, { timeout: 15000, headers }, (res) => {
        const status = res.statusCode ?? 0
        // 重定向跟一次；内层终态透传（cancelled/error 不能谎报成 done）
        if (status >= 300 && status < 400 && res.headers.location) {
          res.resume()
          if (redirectsLeft <= 0) {
            this.finish(id, 'error', '重定向次数过多（>5），已中止')
            return resolvePromise('error')
          }
          let next: string
          try {
            // URL 解析也要一起兜住：Location 畸形时 new URL 在 'response' 回调里
            // 抛出，fetchOnce 的 promise 永不 settle，任务就永久停在 downloading
            // ——finish() 不执行、取消读不到、active 表里的条目也无界增长。
            next = new URL(res.headers.location, url).toString()
            this.assertUrl(next)
          } catch (e: any) {
            this.finish(id, 'error', e?.message ?? '重定向地址非法')
            return resolvePromise('error')
          }
          this.fetchLoop(id, next, partialPath, resumeFrom, gunzip, meta, redirectsLeft - 1).then(resolvePromise)
          return
        }
        // 416 = Range 不满足（.ckpart 比远端文件还大，远端内容已更新）：丢弃已下部分从头重下
        if (status === 416 && resumeFrom > 0) {
          res.resume()
          return resolvePromise('retry-whole')
        }
        if (status >= 400) {
          res.resume()
          this.finish(id, 'error', `HTTP ${status}`)
          return resolvePromise('error')
        }
        const contentLength = Number(res.headers['content-length'] ?? 0)
        if (resumeFrom > 0) {
          // 续传前必须过这道判据：服务端不支持 Range（回 200 全量）、If-Range 校验子
          // 不匹配、Content-Range 的起点/总长与元数据对不上 —— 任一条不成立就当这批
          // 字节不可信，丢弃重下。判据本身是纯函数，见 utils/resume-policy.test.ts
          const decision = decideResume({
            resumeFrom,
            meta,
            status,
            etag: res.headers.etag,
            lastModified: res.headers['last-modified'],
            contentRange: res.headers['content-range'],
            contentLength,
          })
          if (decision === 'restart') {
            res.resume()
            return resolvePromise('retry-whole')
          }
        }

        const h2 = this.active.get(id)
        if (!h2) { res.resume(); return resolvePromise('cancelled') }

        if (status === 206) {
          // 续传：total = 已下 + 剩余；transferred 已在 run() 里初始化为 resumeFrom
          h2.status.total = resumeFrom + (Number.isFinite(contentLength) ? contentLength : 0)
        } else {
          h2.status.total = Number.isFinite(contentLength) ? contentLength : 0
        }
        // 记下这份响应的可比对信息（资源总长取服务端口径，与 decideResume 的校验同源），
        // 中断/取消后下一次续传就拿它当证据。写失败只是失去下次续传资格
        const resourceTotal = status === 206
          ? parseContentRangeTotal(res.headers['content-range']) ?? h2.status.total
          : h2.status.total
        void writePartialMeta(partialPath, {
          etag: res.headers.etag,
          lastModified: res.headers['last-modified'],
          total: resourceTotal,
        })

        const stream = createWriteStream(partialPath, { flags: resumeFrom > 0 ? 'a' : 'w' })
        // 字节计数挂原始响应（与 content-length 同口径）；gunzip 只影响落盘内容
        const src = gunzip ? res.pipe(createGunzip()) : res
        let lastPush = 0
        let settled = false
        const settle = (r: 'done' | 'cancelled' | 'error') => {
          if (settled) return
          settled = true
          resolvePromise(r)
        }

        res.on('data', (chunk: Buffer) => {
          const cur = this.active.get(id)
          if (!cur) return
          if (cur.status.state === 'cancelled') {
            res.destroy()
            this.finish(id, 'cancelled')
            settle('cancelled')
            return
          }
          cur.status.transferred += chunk.length
          const now = Date.now()
          if (now - lastPush >= PROGRESS_PUSH_INTERVAL_MS) {
            lastPush = now
            this.send({ ...cur.status })
          }
        })
        src.on('error', (e: Error) => {
          stream.destroy()
          this.finish(id, 'error', e.message)
          settle('error')
        })
        stream.on('error', (e: Error) => {
          res.destroy()
          this.finish(id, 'error', e.message)
          settle('error')
        })
        stream.on('finish', () => {
          const cur = this.active.get(id)
          if (!cur || cur.status.state === 'cancelled') {
            this.finish(id, 'cancelled')
            return settle('cancelled')
          }
          // 不在此处推送 done：终态在 run() 里 rename 落定最终名后再推（带 path）
          settle('done')
        })
        src.pipe(stream)
      })
      req.once('timeout', () => {
        req.destroy()
        this.finish(id, 'error', '连接超时（15s）')
        resolvePromise('error')
      })
      req.once('error', (e: Error) => {
        this.finish(id, 'error', e.message)
        resolvePromise('error')
      })
    })
  }
}
