/**
 * 断点续传的取舍判据（纯函数，接缝）。
 *
 * .ckpart 分片按 URL 指纹命名，所以"不同 URL 的同名文件互相续传"早就防住了；
 * 剩下的洞是**同一个 URL 的内容被换掉**：release 资产重新上传、镜像回源到不同
 * 构建产物，字节口径不变的部分被当作可信前缀继续拼，产出一个看似完整实则损坏的
 * 文件，而下载器报的是"完成"。
 *
 * 判据来源是 HTTP 语义而非信任：Range 请求带 `If-Range: <上次的校验子>`，
 * 合规服务端校验子不匹配时必须回 200 全量（现有 retry-whole 分支正好接住）。
 * 但镜像站经常不守 If-Range，所以再要一条能独立证伪的证据 —— `Content-Range`
 * 的总长必须与分片元数据记录的总长一致；对不上、或根本给不出可比对的数，一律重下。
 */
import { describe, expect, it } from 'vitest'
import { decideResume, parseContentRangeTotal } from './resume-policy'

const meta = { etag: 'W/"abc"', lastModified: 'Wed, 01 Oct 2026 00:00:00 GMT', total: 1000 }

describe('decideResume：能否把 .ckpart 当可信前缀', () => {
  it('没有已下字节 → 就是从头下', () => {
    expect(decideResume({ resumeFrom: 0, meta, status: 200, contentLength: 1000 })).toBe('restart')
  })

  it('分片元数据缺失 → 无法证明分片来自同一份内容，重下', () => {
    expect(decideResume({ resumeFrom: 400, meta: null, status: 206, contentRange: 'bytes 400-999/1000' })).toBe('restart')
  })

  it('206 + 总长一致 + 起点一致 → 续传', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta, status: 206,
        etag: 'W/"abc"', contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('resume')
  })

  it('206 但服务端校验子变了 → 重下', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta, status: 206,
        etag: 'W/"zzz"', contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('restart')
  })

  it('206 但总长变了 → 镜像没守 If-Range 而资产已被替换，重下', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta, status: 206,
        contentRange: 'bytes 400-1199/1200',
      }),
    ).toBe('restart')
  })

  it('206 但 Content-Range 起点不等于已下字节 → 口径错位，重下', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta, status: 206,
        contentRange: 'bytes 0-999/1000',
      }),
    ).toBe('restart')
  })

  it('206 但服务端没给 Content-Range → 无从校验，重下', () => {
    expect(decideResume({ resumeFrom: 400, meta, status: 206, contentLength: 600 })).toBe('restart')
  })

  it('要求 Range 却收到 200 全量 → 服务端不支持 Range，重下（现有 retry-whole 语义）', () => {
    expect(decideResume({ resumeFrom: 400, meta, status: 200, contentLength: 1000 })).toBe('restart')
  })

  it('416 = 分片比远端还大（远端已更新）→ 重下', () => {
    expect(decideResume({ resumeFrom: 4000, meta, status: 416 })).toBe('restart')
  })

  it('元数据里没有可比对的总长 → 视同无元数据，重下', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta: { etag: 'W/"abc"' }, status: 206,
        contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('restart')
  })

  it('双方都给不出校验子 → 重下：If-Range 无从发出，同尺寸换内容封不住', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta: { total: 1000 }, status: 206,
        contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('restart')
  })

  it('只有 Last-Modified 可比也算校验子（镜像站常剥 ETag，不能因此一律重下）', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta: { total: 1000, lastModified: 'Wed, 01 Oct 2026 00:00:00 GMT' },
        status: 206, lastModified: 'Wed, 01 Oct 2026 00:00:00 GMT',
        contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('resume')
  })

  it('分片记了 ETag 而本次响应没回 → 无从确认，按不可比处理，重下', () => {
    expect(
      decideResume({
        resumeFrom: 400, meta: { total: 1000, etag: 'W/"abc"' }, status: 206,
        contentRange: 'bytes 400-999/1000',
      }),
    ).toBe('restart')
  })
})

describe('parseContentRangeTotal', () => {
  it('正常形态取斜杠后的总长', () => {
    expect(parseContentRangeTotal('bytes 400-999/1000')).toBe(1000)
  })

  it('总长用星号表示未知 → null（不能拿来校验）', () => {
    expect(parseContentRangeTotal('bytes 400-999/*')).toBeNull()
  })

  it('缺失或畸形 → null', () => {
    expect(parseContentRangeTotal(undefined)).toBeNull()
    expect(parseContentRangeTotal('bytes')).toBeNull()
    expect(parseContentRangeTotal('bytes 400-999/abc')).toBeNull()
  })
})

describe('parseContentRangeStart', () => {
  it('取回 Range 的起点字节', async () => {
    const { parseContentRangeStart } = await import('./resume-policy')
    expect(parseContentRangeStart('bytes 400-999/1000')).toBe(400)
    expect(parseContentRangeStart('bytes */1000')).toBeNull()
    expect(parseContentRangeStart(undefined)).toBeNull()
  })
})
