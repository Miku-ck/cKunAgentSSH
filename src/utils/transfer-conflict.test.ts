/**
 * transfer-conflict —— 冲突裁决翻成传输入参的纯逻辑
 *
 * 钉住的都是「悄悄传错/传丢」级别的地方：
 *   · skip 必须真的把该项整条剔除（否则还是覆盖了对端文件）；
 *   · 没有裁决的 path 一律按覆盖保留，绝不能被误当成 skip 丢掉；
 *   · rename 的空 newName 要回落到 basename 自动生成，且自动名要登记进 taken 不撞车；
 *   · 顺序沿用原序（大文件优先排序是 store 的事，这里不打乱语义）。
 */
import { describe, expect, it } from 'vitest'

import { autoRename, buildTransferPlan } from '@/utils/transfer-conflict'

describe('autoRename', () => {
  it('名字未被占用时原样返回', () => {
    expect(autoRename('report.pdf', new Set(['other.pdf']))).toBe('report.pdf')
  })

  it('占用时递增后缀，直到不与 taken 冲突', () => {
    expect(autoRename('report.pdf', new Set(['report.pdf']))).toBe('report (1).pdf')
    expect(autoRename('report.pdf', new Set(['report.pdf', 'report (1).pdf']))).toBe('report (2).pdf')
  })

  it('无扩展名的隐藏文件整体当主体', () => {
    expect(autoRename('.gitignore', new Set(['.gitignore']))).toBe('.gitignore (1)')
  })

  it('多重扩展只切最后一段', () => {
    expect(autoRename('a.tar.gz', new Set(['a.tar.gz']))).toBe('a.tar (1).gz')
  })
})

describe('buildTransferPlan', () => {
  it('overwrite 保持原名与原顺序，skip 整条剔除', () => {
    const plan = buildTransferPlan(
      ['a.txt', 'b.txt', 'c.txt'],
      [
        { path: 'a.txt', action: 'overwrite' },
        { path: 'b.txt', action: 'skip' },
      ],
    )
    expect(plan.paths).toEqual(['a.txt', 'c.txt'])
    expect(plan.renameMap).toEqual({})
  })

  it('没有裁决的 path 视为覆盖保留，不被误删', () => {
    const plan = buildTransferPlan(['a.txt', 'b.txt'], [{ path: 'a.txt', action: 'overwrite' }])
    expect(plan.paths).toEqual(['a.txt', 'b.txt'])
  })

  it('rename 用显式 newName 登记映射并占用该名', () => {
    const plan = buildTransferPlan(
      ['/x/data.csv'],
      [{ path: '/x/data.csv', action: 'rename', newName: 'data-2026.csv' }],
    )
    expect(plan.paths).toEqual(['/x/data.csv'])
    expect(plan.renameMap).toEqual({ '/x/data.csv': 'data-2026.csv' })
  })

  it('rename 空 newName 回落到 basename 自动生成，多条改同名会互相错开', () => {
    const plan = buildTransferPlan(
      ['/src/a.txt', '/src/b.txt'],
      [
        { path: '/src/a.txt', action: 'rename' },
        { path: '/src/b.txt', action: 'rename' },
      ],
      new Set(['a.txt', 'b.txt']),
    )
    expect(plan.renameMap['/src/a.txt']).toBe('a (1).txt')
    expect(plan.renameMap['/src/b.txt']).toBe('b (1).txt')
  })

  it('takenNames 里已有的名字不会再次生成', () => {
    const plan = buildTransferPlan(
      ['/src/a.txt'],
      [{ path: '/src/a.txt', action: 'rename' }],
      new Set(['a.txt', 'a (1).txt']),
    )
    expect(plan.renameMap['/src/a.txt']).toBe('a (2).txt')
  })
})
