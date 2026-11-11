/**
 * `git worktree list --porcelain` 解析守卫。
 *
 * 任务台「工作树」落点靠这份列表选分支工作树，路径随后会作为本地 PTY 的 cwd ——
 * 路径被截断会直接把命令落到错误的目录，所以含空格 / CRLF / 末块无空行都要钉住。
 */
import { describe, expect, it } from 'vitest'
import { parseWorktreeList } from './git-repos-core'

describe('parseWorktreeList（git worktree list --porcelain）', () => {
  it('主工作树 + 分支工作树：分支名去 refs/heads 前缀，首项标主仓', () => {
    const raw = [
      'worktree D:/repo',
      'HEAD abc123',
      'branch refs/heads/main',
      '',
      'worktree D:/repo-wt-feature',
      'HEAD def456',
      'branch refs/heads/feature/x',
      '',
    ].join('\n')
    const list = parseWorktreeList(raw)
    expect(list).toHaveLength(2)
    expect(list[0]).toMatchObject({ path: 'D:/repo', head: 'abc123', branch: 'main', main: true })
    expect(list[1]).toMatchObject({ path: 'D:/repo-wt-feature', branch: 'feature/x', main: false })
  })

  it('路径含空格整段保留，不按空白切', () => {
    const raw = 'worktree D:/My Projects/repo\nHEAD 111\nbranch refs/heads/main\n'
    expect(parseWorktreeList(raw)[0].path).toBe('D:/My Projects/repo')
  })

  it('detached / bare / locked / prunable 各自标出，detached 的 branch 为 null', () => {
    const raw = [
      'worktree /tmp/bare.git',
      'bare',
      '',
      'worktree /tmp/wt-detached',
      'HEAD 222',
      'detached',
      '',
      'worktree /tmp/wt-locked',
      'HEAD 333',
      'branch refs/heads/hold',
      'locked release freeze',
      'prunable gitdir file points to non-existent location',
      '',
    ].join('\n')
    const list = parseWorktreeList(raw)
    expect(list).toHaveLength(3)
    expect(list[0]).toMatchObject({ bare: true, branch: null, main: true })
    expect(list[1]).toMatchObject({ detached: true, branch: null })
    expect(list[2]).toMatchObject({ locked: true, prunable: true, branch: 'hold' })
  })

  it('末块不带空行、CRLF、未知键：仍能收尾且不因新字段失效', () => {
    const raw = [
      'worktree /repo',
      'HEAD 444',
      'branch refs/heads/dev',
      'some-future-git-key whatever',
      '',
      'worktree /repo-wt',
      'HEAD 555',
      'branch refs/heads/topic',
    ].join('\r\n')
    const list = parseWorktreeList(raw)
    expect(list).toHaveLength(2)
    expect(list[1]).toMatchObject({ path: '/repo-wt', branch: 'topic' })
  })

  it('空输出 / 只有脏行一律返回空列表，不臆造落点', () => {
    expect(parseWorktreeList('')).toEqual([])
    expect(parseWorktreeList('   \n\n')).toEqual([])
    expect(parseWorktreeList('HEAD 999\nbranch refs/heads/ghost')).toEqual([])
  })
})
