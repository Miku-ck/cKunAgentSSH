/**
 * SFTP 上传/下载收尾阶段的两组形状守卫（审查 P2 / D12 / S5-01 上传侧补齐）。
 *
 * 钉的是代码形状而不是行为：跑真行为要同时伪造 ssh2 的 SFTPWrapper、
 * fs/promises 的 FileHandle 与 renameOverwrite 的 EXDEV 分支，任何一处与生产线
 * 形不同源都会假绿（同仓 ssh-manager-download-total-zero.test.ts 的既定手法）。
 * 行为可确定性驱动的「心跳粘滞复位」另有独立测试文件，不在此处。
 *
 * 守卫一（rename 阶段错误分类）：数据已完整落进 .part 之后、rename 收尾之前
 * 置完成标记；标记置位后的异常一律归 RENAME_FAILED（retryable、保留 .part）。
 * 原实现 rename 失败落进通用 catch，被判不可重试并删 .part —— 完整数据白白
 * 丢光，用户从头再传。上传侧曾只有下载侧有这条腿（同族防护漏一处），本文件
 * 把两侧同时钉住，任何一侧回退都会红。
 *
 * 守卫二（续传边界 D12 上传侧）：源文件缩小后，陈旧、过大的 .part 不能当完整
 * 结果直接 rename —— `>` 时前 4KB 校验无法区分「完成」与「源缩小」，fail-safe
 * 是删 .part 从头传；且续传起点只信「size - 并发窗口」的可信下界（零填充会把
 * size 顶到最远已写终点，以 size 为起点会跳过中段全零块，静默损坏）。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./ssh-manager.ts', import.meta.url), 'utf8')

/** 取类方法体（类成员缩进 2 空格，首个独占一行的 `  }` 即收尾）；按行切避开 CRLF。 */
function methodBody(headerPrefix: string): string {
  const lines = src.split(/\r?\n/)
  const start = lines.findIndex(l => l.startsWith(headerPrefix))
  expect(start, `找不到 ${headerPrefix}`).toBeGreaterThanOrEqual(0)
  const end = lines.findIndex((l, i) => i > start && l === '  }')
  expect(end).toBeGreaterThan(start)
  return lines.slice(start, end + 1).join('\n')
}

/** 去注释后再断言「旧形状不得出现」：注释里原样复述的旧代码不是违规（本仓踩过）。
 *  逐行切：只在行首（可缩进）处识别块注释边界，不用正则去匹配块注释的起止序列 ——
 *  正则字面量自身就含这些起止字符，会把本文件源码当块注释吞掉（本轮亲历）。 */
function stripComments(code: string): string {
  let out = ''
  let inBlock = false
  for (const line of code.split('\n')) {
    const t = line.trim()
    if (inBlock) {
      if (t.endsWith('*/')) inBlock = false
      continue
    }
    if (t.startsWith('/*')) {
      if (!t.endsWith('*/')) inBlock = true
      continue
    }
    if (t.startsWith('//')) continue
    out += line + '\n'
  }
  return out
}

const up = methodBody('  async sftpUpload(')
const dl = methodBody('  async sftpDownload(')

describe('rename 阶段错误分类（P2，上传/下载对称双腿）', () => {
  it.each([
    ['sftpUpload', up, 'uploadComplete', 'await sftpRename(sftp, partPath, remotePath)'],
    ['sftpDownload', dl, 'downloadComplete', 'await renameOverwrite(partPath, localPath)'],
  ])('%s：每一处 rename 收尾语句前都已置完成标记（无标记的 rename 失败会被误删 .part）',
    (_name, body, flag, renameCall) => {
      expect(body).toContain(`${flag} = true`)
      let from = 0
      let seen = 0
      for (;;) {
        const rn = body.indexOf(renameCall, from)
        if (rn < 0) break
        seen++
        const mark = body.lastIndexOf(`${flag} = true`, rn)
        expect(mark, `第 ${seen} 处 rename 之前必须有完成标记`).toBeGreaterThanOrEqual(0)
        expect(mark, `第 ${seen} 处 rename 不得越过自己的标记`).toBeLessThan(rn)
        from = rn + 1
      }
      expect(seen, `${renameCall} 至少存在一处`).toBeGreaterThan(0)
    })

  it.each([
    ['sftpUpload', up, 'uploadComplete'],
    ['sftpDownload', dl, 'downloadComplete'],
  ])('%s：catch 里标记置位且未停滞 → RENAME_FAILED，且优先于通用归类', (_name, body, flag) => {
    const bare = stripComments(body)
    expect(bare).toMatch(new RegExp(`if\\s*\\(\\s*${flag}\\s*&&\\s*!heartbeat\\.isStalled\\(\\)\\s*\\)`))
    expect(bare).toContain("code: 'RENAME_FAILED'")
    // 判定顺序：RENAME_FAILED 分支必须在 heartbeat.isStalled / classifyTransferError
    // 的通用兜底之前，否则标记形同虚设
    const renameBranch = bare.indexOf(`if (${flag} && !heartbeat.isStalled())`)
    const genericBranch = bare.indexOf('classifyTransferError')
    expect(renameBranch).toBeGreaterThanOrEqual(0)
    expect(renameBranch, '通用归类不得抢在 rename 分支之前').toBeLessThan(genericBranch)
  })

  it('RENAME_FAILED 属于 retryable：保留 .part 的判据与渲染端同源', async () => {
    const { isTransferRetryable } = await import('../shared/transfer-error-core')
    expect(isTransferRetryable('RENAME_FAILED'), '不可重试 ⇒ 主进程 catch 会删掉完整 .part').toBe(true)
    // 主进程删 .part 的唯一门径就是 !isTransferRetryable（两侧共用同一纯函数）
    expect(stripComments(up)).toMatch(/if \(!isTransferRetryable\(tErr\.code\) && tErr\.code !== 'CANCELLED'\)/)
    expect(stripComments(dl)).toMatch(/if \(!isTransferRetryable\(tErr\.code\) && tErr\.code !== 'CANCELLED'\)/)
  })
})

describe('上传续传边界（D12 / S5-01 上传侧）', () => {
  const bareUp = stripComments(up)

  it('resumeSize > total 的陈旧残留一律删掉重来，不得残留 `>=` 直接 rename 的旧形状', () => {
    expect(bareUp).toContain('if (resumeSize > total) {')
    expect(bareUp, '`>=` 会把源缩小后的过大 .part 当完整结果').not.toMatch(/if\s*\(partStat\.size\s*>=\s*total/)
  })

  it('续传起点只取可信下界（size - 并发窗口），不再有「长度相等即判完成」的直接 rename', () => {
    expect(bareUp).toMatch(/const trustworthy = Math\.max\(0, resumeSize - RESUME_OVERLAP\)/)
    expect(bareUp).toContain('startOffset = trustworthy')
    // 上传侧本就不该有 size===total 直接 rename 的捷径（下载侧那条由
    // download-total-zero 测试钉住 total > 0 前置，这里是另一条腿）
    expect(bareUp, '相等即完成的旧捷径会让零填充洞蒙混交付').not.toMatch(/partStat\.size === total/)
  })

  it('截回可信下界失败则删 .part 从头传（宁可丢进度不可丢数据）', () => {
    expect(bareUp).toContain('await sftpSetstatSize(sftp, partPath, trustworthy)')
    const trimPos = bareUp.indexOf('await sftpSetstatSize(sftp, partPath, trustworthy)')
    const unlinkAfterFail = bareUp.indexOf('await sftpUnlink(sftp, partPath)', trimPos)
    expect(unlinkAfterFail, '截断失败的兜底删除不得缺失').toBeGreaterThan(trimPos)
    expect(unlinkAfterFail - trimPos, '兜底删除必须紧跟截断分支').toBeLessThan(400)
  })

  it('下载侧 D12 门径未回退（与既有测试互钉，防单侧漂移）', () => {
    expect(stripComments(dl)).toContain('if (resumeSize > total) {')
  })
})

describe('上传记录接线（主进程侧）', () => {
  it('只有上传进度进入记录 sink：下载没有这条腿', () => {
    expect(stripComments(up)).toContain('this.uploadProgressSink?.(')
    expect(stripComments(dl), '下载进度不该写进上传记录').not.toContain('uploadProgressSink')
  })

  it('进度 sink 抛错不得影响传输（记录侧异常隔离）', () => {
    expect(stripComments(up)).toMatch(/try \{ this\.uploadProgressSink\?\.\([^)]*\) \} catch/)
  })

  it('resume 短路：仅显式继续时检查，且比对大小 + 前 4KB 头', () => {
    const bareUp = stripComments(up)
    expect(bareUp).toContain('if (opts?.resume && await alreadyComplete())')
    expect(bareUp).toMatch(/st\.size !== total/)
    expect(bareUp).toContain('localHead.equals(remoteHead)')
    // .part 在场说明确实没传完，不能直接短路
    expect(bareUp).toContain('await sftpStat(sftp, partPath)')
    // 下载侧不接上传的 opts.resume 参数
    expect(stripComments(dl)).not.toContain('opts?.resume')
  })
})
