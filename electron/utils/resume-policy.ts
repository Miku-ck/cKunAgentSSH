/**
 * 断点续传取舍判据 —— 纯函数，无 fs / 无网络，两端都能钉死（实现侧见 electron/gh-download.ts）。
 *
 * 为什么需要它：`.ckpart` 文件名里已带 URL 指纹，"不同 URL 的同名文件互相续传"这一洞
 * 早已封住；剩下的洞是**同一个 URL 背后的内容被换掉**（release 资产重新上传、镜像回源到
 * 另一个构建产物）。已下字节被当成可信前缀继续拼，产出一个尺寸看着对、内容实则损坏的文件，
 * 而下载器报的是"完成" —— 这类损坏不会有任何报错，只会在使用时炸。
 */

/** 上次写入分片时记下的可比对信息（落盘为 `<分片名>.meta.json`） */
export interface PartialMeta {
  etag?: string
  lastModified?: string
  /** 上次看到的资源总字节数；缺它就没有独立于校验子的第二重证据 */
  total?: number
}

export interface ResumeProbeInput {
  /** 磁盘上已有的分片字节数 */
  resumeFrom: number
  /** 分片元数据；null = 没有（首次下载，或旧版本/手工留下的分片） */
  meta: PartialMeta | null
  /** 响应状态码 */
  status: number
  /** 响应里的 ETag */
  etag?: string
  /** 响应里的 Last-Modified */
  lastModified?: string
  /** 响应里的 Content-Range，如 `bytes 400-999/1000` */
  contentRange?: string
  /** 响应里的 Content-Length（本次实体长度） */
  contentLength?: number
}

export type ResumeDecision = 'resume' | 'restart'

/** 分片与元数据保留期：超龄即清理，避免 Downloads/github-accel 无界增长 */
export const PARTIAL_MAX_AGE_DAYS = 7

const RANGE_WITH_TOTAL_RE = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/
const RANGE_OPEN_TOTAL_RE = /^bytes\s+\*\/(\d+)$/

function numberOrNull(raw: string): number | null {
  if (raw === '*') return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** `bytes 400-999/1000` → 1000；总长为星号（未知）与畸形形态一律 null */
export function parseContentRangeTotal(header?: string): number | null {
  if (!header) return null
  const m = RANGE_WITH_TOTAL_RE.exec(header.trim())
  if (m) return numberOrNull(m[3])
  const open = RANGE_OPEN_TOTAL_RE.exec(header.trim())
  return open ? numberOrNull(open[1]) : null
}

/** `bytes 400-999/1000` → 400；星号形态（无起始字节信息）与畸形一律 null */
export function parseContentRangeStart(header?: string): number | null {
  if (!header) return null
  const m = RANGE_WITH_TOTAL_RE.exec(header.trim())
  if (!m) return null
  const n = Number(m[1])
  return Number.isFinite(n) ? n : null
}

/**
 * 已下的分片能不能当可信前缀继续拼。
 *
 * 证据链按强度排：服务端对 `Range + If-Range: <校验子>` 回 206，本身就意味着它认可
 * 校验子仍匹配（RFC 7233 要求不匹配时回 200 全量）；镜像站经常不守这条，所以还要
 * `Content-Range` 的总长与起点都能对上。任何一环给不出可比对的数 —— 宁可重下。
 *
 * restart 不丢数据：调用方以 `flags: 'w'` 重开文件，等于从头下同一个分片名。
 */
export function decideResume(i: ResumeProbeInput): ResumeDecision {
  const restart: ResumeDecision = 'restart'
  if (i.resumeFrom <= 0) return restart
  // 416 = 分片比远端资源还大，远端内容已更新
  if (i.status === 416) return restart
  // 要求了 Range 却拿到 200：服务端不支持 Range，或 If-Range 校验子已不匹配
  if (i.status !== 206) return restart
  if (!i.meta) return restart

  const { meta } = i
  // 没有可比对的总长，就只剩"信不信服务端"这一个主张，不足以担保字节连续
  if (typeof meta.total !== 'number' || !Number.isFinite(meta.total) || meta.total <= 0) return restart

  // 校验子至少得有一条真的可比。两边都拿不出 ETag / Last-Modified 时，If-Range 根本
  // 无从发出，206 就不再代表"服务端认可这批旧字节仍是同一份内容"，剩下的只有总长与
  // 起点对得上 —— 而重新上传一份**同尺寸**的资产照样对得上，正是本文件开头要封的那个洞。
  // 按上面的口径"任何一环给不出可比对的数就重下"，这里不能网开一面。
  const etagComparable = Boolean(meta.etag && i.etag)
  const mtimeComparable = Boolean(meta.lastModified && i.lastModified)
  if (!etagComparable && !mtimeComparable) return restart

  // 校验子任一边有值而两边不等 → 内容已换
  if (meta.etag && i.etag && meta.etag !== i.etag) return restart
  if (meta.lastModified && i.lastModified && meta.lastModified !== i.lastModified) return restart

  const start = parseContentRangeStart(i.contentRange)
  if (start === null || start !== i.resumeFrom) return restart

  const total = parseContentRangeTotal(i.contentRange)
  if (total === null || total !== meta.total) return restart

  return 'resume'
}
