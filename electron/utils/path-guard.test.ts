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

import { isCopyTargetSelfNested } from './path-guard'

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
