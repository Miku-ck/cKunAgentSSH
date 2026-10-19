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
  cancel: () => void
}

/** 进度节流间隔：下载可能几百 MB，每 chunk 推一条会把 IPC 打爆 */
const PROGRESS_PUSH_INTERVAL_MS = 250

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

  /** 仅允许 http(s) URL（file:// 等协议不能落盘） */
  private assertUrl(url: string): void {
    if (typeof url !== 'string' || !/^https:\/\//i.test(url)) throw new Error('仅支持 https 下载地址')
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
