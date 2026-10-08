/**
 * S5-08 回归：同侧复制（fs:copy）的重叠判定。
 *
 * 递归 collect 会在自己刚 mkdir 出来的子目录里再收一遍源树，所以「把文件夹粘贴进
 * 它自己」不是错误而是无限递归 —— 一路撞到系统路径长度上限，留下一串删不掉的
 * 长路径残骸，且中途没有取消原语。判定必须在收集之前。
 */
import { describe, expect, it, vi } from 'vitest'
import { join } from 'path'

vi.mock('electron', () => ({
  app: { getPath: () => '/tmp/ckun-agent-ssh-test', getAppPath: () => '/tmp/ckun-agent-ssh-test' },
  BrowserWindow: class {},
}))

import { isCopyTargetSelfNested, rejectSymlinkTarget } from './path-guard'

const A = join('tree', 'a')
const B = join('tree', 'b')

describe('isCopyTargetSelfNested', () => {
  it('粘贴进自身 = 拒绝', () => {
    expect(isCopyTargetSelfNested(A, A)).toBe(true)
  })

  it('目标在源内部 = 拒绝', () => {
    expect(isCopyTargetSelfNested(A, join(A, 'sub'))).toBe(true)
    expect(isCopyTargetSelfNested(A, join(A, 'sub', 'deep'))).toBe(true)
  })

  it('同前缀但不是目录边界 = 放行（tree-a 不是 tree-a 的子项）', () => {
    expect(isCopyTargetSelfNested(A, `${A}-copy`)).toBe(false)
  })

  it('兄弟目录与父目录 = 放行', () => {
    expect(isCopyTargetSelfNested(A, B)).toBe(false)
    expect(isCopyTargetSelfNested(A, join('tree'))).toBe(false)
  })

  it('等价写法（带 .. 与显式当前目录段）也判成自身', () => {
    expect(isCopyTargetSelfNested(A, join('tree', '.', 'a'))).toBe(true)
    expect(isCopyTargetSelfNested(join('tree', 'x', '..', 'a'), A)).toBe(true)
  })
})

/**
 * S5-15：目标侧的符号链接。源侧早已 lstat + 跳过，但目标侧原先直接 copyFile ——
 * 目标目录里一条外部植入的链接就能把这一次写带到两个根之外，而那条路径从未经过
 * assertSafePath（它是敏感目录黑名单，不是围栏）。
 */
describe('rejectSymlinkTarget（S5-15）', () => {
  const dest = join('tree', 'a', 'notes.txt')
  const statOf = (kind: 'symlink' | 'file' | 'dir'): { isSymbolicLink(): boolean } => ({
    isSymbolicLink: () => kind === 'symlink',
  })

  it('目标是符号链接 = 中止，且文案带上是哪一条', () => {
    const err = rejectSymlinkTarget(dest, statOf('symlink'))
    expect(err).not.toBeNull()
    expect(err).toContain(dest)
  })

  it('普通文件与目录 = 放行（覆盖写是复制的正常语义）', () => {
    expect(rejectSymlinkTarget(dest, statOf('file'))).toBeNull()
    expect(rejectSymlinkTarget(dest, statOf('dir'))).toBeNull()
  })

  it('目标还不存在（lstat 抛错）= 放行', () => {
    expect(rejectSymlinkTarget(dest, null)).toBeNull()
  })
})
