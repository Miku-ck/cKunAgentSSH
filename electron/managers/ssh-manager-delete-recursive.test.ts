/**
 * 远端递归删除（S5-26）的形状守卫。
 *
 * 原实现只发 rmdir：非空目录必然失败并提示「目录不为空」。而同一套 UI 里
 * 远端复制早已 `cp -r` 整树、下载能展开整树，唯独删除不能删非空目录 —— 用户
 * 要清空一个项目目录只能去开终端敲 `rm -rf`，能力不对称。
 *
 * 这里钉三件事：
 * 1. 目录分支必须走 `rm -rf`（由服务端递归），且路径经 shSingleQuote 转义 ——
 *    exec 通道没有 argv 形态，不转义就是命令拼接注入；
 * 2. 「结果未知」（通道未回 exit-status）不得谎报成功也不得直接判失败 ——
 *    递归删除可能已经删完，两条极端的文案都会把人推向错误的下一次操作；
 * 3. 单文件仍走 unlink，不跟着目录分支一起去 exec —— 那是把一次本地系统调用
 *    换成一次网络往返，且 unlink 的错误映射（Permission denied / No such file）
 *    比解析 stderr 可靠得多。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = readFileSync(new URL('./ssh-manager.ts', import.meta.url), 'utf8')

/** 取方法体：按行切避开 CRLF。sftpDelete 的签名行没有前导缩进（历史格式），
 *  但函数内每个语句块都以「  }」收尾，所以结束判定仍用独占一行的 `  }`。
 *  注意不能取到下一个方法 —— sftpDelete 是类里最后一个方法，其后紧跟的
 *  modeToString 会一起被切进来。故在此显式截到 class 的收尾。 */
function deleteBody(): string {
  const lines = src.split(/\r?\n/)
  const start = lines.findIndex(l => l.startsWith('async sftpDelete('))
  expect(start, '找不到 sftpDelete').toBeGreaterThanOrEqual(0)
  // 类内方法收尾是「  }」，类收尾是「}」；取第一个出现在 start 之后的「  }」
  // 之后的下一个「  }」会越界 —— 直接找类收尾更稳。
  const end = lines.findIndex((l, i) => i > start && l === '}')
  expect(end).toBeGreaterThan(start)
  return lines.slice(start, end + 1).join('\n')
}

/** 去注释后再断言「旧形状不得出现」：注释里原样复述的旧代码不是违规。 */
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

const del = stripComments(deleteBody())

describe('sftpDelete 目录递归分支（S5-26）', () => {
  it('目录走 rm -rf，且路径经 shSingleQuote 转义', () => {
    expect(del).toContain('rm -rf ')
    expect(del).toContain('shSingleQuote(remotePath)')
  })

  it('退出码未知 → 抛「结果未知」并提示刷新确认，不谎报成功', () => {
    expect(del).toContain("classifyExit(result.exitCode) === 'unknown'")
    expect(del).toContain('删除结果未知')
    // 未知分支必须早退，不得继续走到「return」当成功 —— 钉住 throw 在 unknown
    // 判定之后、且 unknown 段里没有 return（否则就是两条路都通）。
    const unknownAt = del.indexOf("classifyExit(result.exitCode) === 'unknown'")
    const unknownBlockEnd = del.indexOf('}', unknownAt)
    expect(del.slice(unknownAt, unknownBlockEnd)).toContain('throw')
    expect(del.slice(unknownAt, unknownBlockEnd)).not.toContain('return')
  })

  it('非零退出 → 带远端 stderr 报失败', () => {
    expect(del).toContain('result.exitCode !== 0')
    expect(del).toContain('删除失败')
  })

  it('单文件仍走 unlink（不把系统调用换成 exec 往返）', () => {
    expect(del).toContain('sftp.unlink(remotePath')
  })

  it('旧的 rmdir 分支已移除（否则非空目录依旧删不掉）', () => {
    expect(del).not.toContain('sftp.rmdir(')
  })
})
