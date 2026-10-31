/**
 * sftpDownload 必须验远端报的 size 再建分块表（形状守卫，同 ssh-manager-download-total-zero
 * 那份的理由：跑真行为要伪造 ssh2 的 SFTPWrapper、FileHandle 与 renameOverwrite 分支，
 * 任何一处与生产线不同源都会假绿）。
 *
 * 威胁模型：连上的那台服务器是不可信输入源。stat().size 由它给，而分块表长度
 * = total / CHUNK_SIZE 直接由此推导 —— 报一个 PB 级数字就能在读出第一个字节之前
 * 让主进程分配几十亿个对象。同一函数下面早已为「报 0 的未知长度」准备了
 * UNKNOWN_SIZE_LIMIT，漏的正是「报一个荒谬的大数」这一路。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./ssh-manager.ts', import.meta.url), 'utf8')

function downloadBody(): string {
  const lines = src.split(/\r?\n/)
  const start = lines.findIndex((l) => l.startsWith('  async sftpDownload('))
  expect(start, '找不到 sftpDownload').toBeGreaterThanOrEqual(0)
  const end = lines.findIndex((l, i) => i > start && l === '  }')
  expect(end).toBeGreaterThan(start)
  return lines.slice(start, end + 1).join('\n')
}

describe('sftpDownload 对远端声明尺寸的校验', () => {
  const dl = downloadBody()

  it('total 取自 stat.size 之后立刻被验，且验在分块表之前', () => {
    const guard = dl.search(/Number\.isSafeInteger\(total\)/)
    expect(guard, '缺少对 total 的安全整数校验').toBeGreaterThanOrEqual(0)
    expect(dl).toMatch(/total\s*>\s*MAX_SFTP_DOWNLOAD_BYTES/)
    const chunksAt = dl.indexOf('// 4. 构造分块列表')
    expect(chunksAt, '找不到分块表构造那段').toBeGreaterThanOrEqual(0)
    // vitest 的比较断言只收一个参数，失败说明只能挂在 expect(value, message) 上
    expect(chunksAt > guard, '校验必须挡在建表之前，否则内存已经花完了').toBe(true)
  })

  it('校验失败先归还租约再抛，绝不泄漏 SFTP 连接', () => {
    const end = dl.indexOf('const partPath')
    expect(end, '找不到分块路径那行，取不出校验段').toBeGreaterThan(0)
    const guardBlock = dl.slice(dl.indexOf('Number.isSafeInteger(total)'), end)
    expect(guardBlock, '这条抛路没归还租约 = 每次拒下载漏一条连接').toMatch(/release\(\)/)
    expect(guardBlock).toMatch(/throw new Error\(/)
  })

  it('上限常量是模块级单一事实源，且未知长度那一路的软上限未回退', () => {
    expect(src).toMatch(/^const MAX_SFTP_DOWNLOAD_BYTES = /m)
    expect(dl).toMatch(/UNKNOWN_SIZE_LIMIT/)
  })

  it('变异自检：把校验摘掉后，上面的断言确实会红', () => {
    const mutant = dl.replace(/if \(!Number\.isSafeInteger\(total\)[\s\S]*?\n    }\n/, '')
    expect(mutant).not.toBe(dl)
    expect(/Number\.isSafeInteger\(total\)/.test(mutant)).toBe(false)
  })
})
