/**
 * GitHub 加速 —— 内置下载器（主进程）
 *
 * 经镜像 URL 流式落盘：<最终名>.ckpart 部分文件（同 URL 重复下载时按已下字节
 * Range 续传，服务端返回 200 则全量重下），完成后 rename 为最终文件名（冲突自动
 * 加序号，不静默覆盖）；取消/出错保留 .ckpart 供下次续传。
 * 进度节流推送（~4/s）；字节计数始终用原始响应（gunzip 解压流不计入，口径与
 * content-length 一致，进度条才不会错乱）。
 * 落盘目录固定为用户下载目录下的 github-accel 子目录，渲染层只能发起/取消，
 * 不能指定落盘路径。
 */
import { app } from 'electron'
import { join, extname } from 'path'
import { createWriteStream } from 'fs'
import { stat, mkdir, access, rename } from 'fs/promises'
import https from 'https'
import { createGunzip } from 'zlib'

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

    // 部分文件用固定名（<名>.ckpart）：同 URL 重复下载时按已下字节续传；
    // 完成时才 rename 成最终名（uniquePath 防覆盖）。
    const partialPath = join(dir, `${h.status.fileName}.ckpart`)
    let resumeFrom = 0
    try {
      resumeFrom = (await stat(partialPath)).size
    } catch { /* 无部分文件，从 0 开始 */ }
    if (resumeFrom > 0) {
      // 续传：已下字节计入进度
      h.status.transferred = resumeFrom
    }

    const finalState = await this.fetchLoop(id, url, partialPath, resumeFrom, gunzip)
    if (finalState === 'done') {
      const h2 = this.active.get(id)
      if (!h2 || h2.status.state === 'cancelled') {
        this.finish(id, 'cancelled')
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
      this.finish(id, 'done')
    }
  }

  /** 下载循环：'retry-whole'（服务端不支持 Range）时清零重下，其余终态即返回 */
  private async fetchLoop(
    id: string, url: string, partialPath: string, resumeFrom: number, gunzip: boolean,
  ): Promise<'done' | 'cancelled' | 'error'> {
    for (;;) {
      const h = this.active.get(id)
      if (!h) return 'cancelled'
      if (h.status.state === 'cancelled') {
        this.finish(id, 'cancelled')
        return 'cancelled'
      }
      const result = await this.fetchOnce(id, url, partialPath, resumeFrom, gunzip)
      if (result !== 'retry-whole') return result
      // 服务端不支持 Range（返回 200）：丢弃已下部分，从头重下
      resumeFrom = 0
    }
  }

  private fetchOnce(
    id: string, url: string, partialPath: string, resumeFrom: number, gunzip: boolean,
  ): Promise<'done' | 'retry-whole' | 'cancelled' | 'error'> {
    return new Promise((resolvePromise) => {
      const h = this.active.get(id)
      if (!h) return resolvePromise('cancelled')

      const headers: Record<string, string> = {
        'User-Agent': 'cKunAgentSSH-ghaccel-downloader',
        'Accept-Encoding': 'identity', // 镜像常把压缩编码搞坏；这里不要压缩传输
      }
      if (resumeFrom > 0) headers.Range = `bytes=${resumeFrom}-`

      const req = https.get(url, { timeout: 15000, headers }, (res) => {
        const status = res.statusCode ?? 0
        // 重定向跟一次；内层终态透传（cancelled/error 不能谎报成 done）
        if (status >= 300 && status < 400 && res.headers.location) {
          res.resume()
          const next = new URL(res.headers.location, url).toString()
          try {
            this.assertUrl(next)
          } catch (e: any) {
            this.finish(id, 'error', e?.message ?? '重定向地址非法')
            return resolvePromise('error')
          }
          this.fetchLoop(id, next, partialPath, resumeFrom, gunzip).then(resolvePromise)
          return
        }
        if (status >= 400) {
          res.resume()
          this.finish(id, 'error', `HTTP ${status}`)
          return resolvePromise('error')
        }
        // 续传请求若得到 200，说明服务端不支持 Range：丢弃已下部分，全量重来
        if (resumeFrom > 0 && status !== 206) {
          res.resume()
          return resolvePromise('retry-whole')
        }

        const h2 = this.active.get(id)
        if (!h2) { res.resume(); return resolvePromise('cancelled') }

        const contentLength = Number(res.headers['content-length'] ?? 0)
        if (status === 206) {
          // 续传：total = 已下 + 剩余；transferred 已在 run() 里初始化为 resumeFrom
          h2.status.total = resumeFrom + (Number.isFinite(contentLength) ? contentLength : 0)
        } else {
          h2.status.total = Number.isFinite(contentLength) ? contentLength : 0
        }

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
