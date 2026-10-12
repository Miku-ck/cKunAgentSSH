/**
 * SFTP 下载在 total===0 时的「大小一致即完成」捷径回归。
 *
 * stat.size 对 /proc/*、/sys/*、正在增长的日志恒报 0，而这类文件确实能读出字节；
 * 同时一次立刻停滞的尝试会留下 0 字节的 .part（retryable 分支刻意保留它）。
 * 原实现写的是 `partStat.size === total`，于是在 0 === 0 上命中：把刚截空的 .part
 * 直接 rename 到操作员已有的本地文件上，再回一个 100%「完成」—— 静默毁数据。
 * 同文件下方早就为 total===0 备了「按块顺序读到短读为止」的未知长度路径，封的是
 * 分块表那条腿，捷径这条腿漏了（本仓反复出现的「同族防护漏一处」）。
 *
 * 钉的是代码形状而不是行为：跑真行为要同时伪造 ssh2 的 SFTPWrapper、fs/promises 的
 * FileHandle 与 renameOverwrite 的 EXDEV 分支，任何一处与生产线形不同源都会假绿
 * （见 docs/reviews 里 _wire_s 那条教训）。所以最后一条是变异自检。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./ssh-manager.ts', import.meta.url), 'utf8')

/** 取 sftpDownload 的方法体（类成员缩进 2 空格，内层块更深，首个独占一行的 `  }` 即收尾）。
 *  按行切而不是 indexOf('\n  }\n')：本仓源文件是 CRLF，字符串字面量里的 \n 匹配不上。 */
function downloadBody(): string {
  const lines = src.split(/\r?\n/)
  const start = lines.findIndex(l => l.startsWith('  async sftpDownload('))
  expect(start, '找不到 sftpDownload').toBeGreaterThanOrEqual(0)
  const end = lines.findIndex((l, i) => i > start && l === '  }')
  expect(end).toBeGreaterThan(start)
  return lines.slice(start, end + 1).join('\n')
}

const GUARD = /total\s*>\s*0\s*&&\s*partStat\.size\s*===\s*total/

describe('sftpDownload 的未知长度捷径（2026-10-12 审查轮）', () => {
  const dl = downloadBody()

  it('「.part 与远端等长即 rename」只在已知长度时成立', () => {
    expect(dl, '早退捷径必须带 total > 0 前置').toMatch(GUARD)
  })

  it('早退捷径里除 rename 外不写任何字节，且发生在未知长度读取路径之前', () => {
    const shortcut = dl.slice(dl.indexOf('partStat.size === total'))
    const earlyRename = shortcut.indexOf('await renameOverwrite(partPath, localPath)')
    expect(earlyRename).toBeGreaterThanOrEqual(0)
    // total===0 的读取循环必须在这条捷径之后：捷径失效时要由它接管
    expect(dl.indexOf('if (total === 0) {')).toBeGreaterThan(dl.indexOf('partStat.size === total'))
  })

  it('未知长度路径仍先 truncate 到实际读出的字节，再交付', () => {
    const unknown = dl.slice(dl.indexOf('if (total === 0) {'), dl.indexOf('// 4. 构造分块列表'))
    const truncate = unknown.indexOf('downloadLocalFd.truncate(off)')
    const rename = unknown.indexOf('renameOverwrite(partPath, localPath)')
    expect(truncate).toBeGreaterThanOrEqual(0)
    expect(rename).toBeGreaterThan(truncate)
    // 无界灌盘的上限还在（远端 /dev/zero 就是这种流）
    expect(unknown).toMatch(/UNKNOWN_SIZE_LIMIT/)
  })

  it('无 pid 续传捷径的其他门径未回退：大于 total 的残留一律删掉重来（D12）', () => {
    expect(dl).toContain('if (resumeSize > total) {')
    // 只查可执行语句：D12 那句注释里原样写着旧形状 `partStat.size >= total`，
    // 拿它当命中会把守卫自己判红（本仓踩过：注释复述旧文案 ≠ 违规）
    expect(dl, '`>=` 会把陈旧过大的 .part 当完整结果').not.toMatch(/if\s*\(partStat\.size\s*>=\s*total/)
  })
})

describe('守卫自身的变异自检', () => {
  const hit = (line: string) => GUARD.test(line)

  it('旧形状必须判不出来（否则这条守卫恒绿）', () => {
    expect(hit('if (partStat.size === total) {')).toBe(false)
    expect(hit('if (partStat.size >= total) {')).toBe(false)
  })
  it('现形状必须判得出来', () => {
    expect(hit('if (total > 0 && partStat.size === total) {')).toBe(true)
  })
})
