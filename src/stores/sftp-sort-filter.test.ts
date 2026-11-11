/**
 * 列表排序 / 筛选 / Shift 选择的纯逻辑单测（S5-23 / S5-24）。
 *
 * 没有组件测试环境（未装 @vue/test-utils/jsdom），这里直接驱动 store：
 * sortEntries/filterEntries 是无状态纯函数，selectRange/selectAll 只读写
 * 选择集合与可见序，都能脱离 DOM 验证。
 */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSftpStore } from './sftp'

vi.mock('@/api/client', () => ({
  toFriendlyMessage: (e: unknown) => (e as Error)?.message || 'err',
  unwrapIpcErrorEnvelope: (m: string) => m,
}))

vi.mock('electron', () => ({ ipcRenderer: { invoke: vi.fn(), on: vi.fn() } }))

interface Row { name: string; size: number; modified: number; isDirectory: boolean }

const rows: Row[] = [
  { name: 'b.txt', size: 10, modified: 300, isDirectory: false },
  { name: 'a10.txt', size: 30, modified: 100, isDirectory: false },
  { name: 'a2.txt', size: 20, modified: 200, isDirectory: false },
  { name: 'dirB', size: 0, modified: 500, isDirectory: true },
  { name: 'dirA', size: 0, modified: 50, isDirectory: true },
]

describe('sortEntries / filterEntries', () => {
  let store: ReturnType<typeof useSftpStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSftpStore()
  })

  it('目录恒在前，组内再按列排序', () => {
    const out = store.sortEntries(rows, 'name', 'asc')
    expect(out.filter(r => r.isDirectory).map(r => r.name)).toEqual(['dirA', 'dirB'])
    expect(out.filter(r => !r.isDirectory).map(r => r.name)).toEqual(['a2.txt', 'a10.txt', 'b.txt'])
  })

  it('名称用自然序：a2 排在 a10 前', () => {
    const out = store.sortEntries(rows, 'name', 'asc')
    const names = out.filter(r => !r.isDirectory).map(r => r.name)
    expect(names.indexOf('a2.txt')).toBeLessThan(names.indexOf('a10.txt'))
  })

  it('大小降序时数值大的在前，目录仍在最前', () => {
    const out = store.sortEntries(rows, 'size', 'desc')
    expect(out.slice(0, 2).every(r => r.isDirectory)).toBe(true)
    expect(out.filter(r => !r.isDirectory).map(r => r.size)).toEqual([30, 20, 10])
  })

  it('同值按名称兜底（排序稳定，不随方向抖动）', () => {
    const out = store.sortEntries(rows, 'size', 'asc')
    // 两个目录 size 都是 0，应按名称排序而非保持原序
    expect(out.slice(0, 2).map(r => r.name)).toEqual(['dirA', 'dirB'])
  })

  it('不修改原数组', () => {
    const input: Row[] = [...rows]
    store.sortEntries(input, 'name', 'desc')
    expect(input.map(r => r.name)).toEqual(rows.map(r => r.name))
  })
})

describe('filterEntries', () => {
  let store: ReturnType<typeof useSftpStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSftpStore()
  })

  it('空关键字返回原集', () => {
    expect(store.filterEntries(rows, '')).toHaveLength(rows.length)
    expect(store.filterEntries(rows, '   ')).toHaveLength(rows.length)
  })

  it('大小写不敏感的子串匹配', () => {
    const out = store.filterEntries(rows, 'A')
    expect(out.map(r => r.name)).toEqual(['a10.txt', 'a2.txt', 'dirA'])
  })
})

describe('selectRange / selectAll', () => {
  let store: ReturnType<typeof useSftpStore>

  beforeEach(() => {
    setActivePinia(createPinia())
    store = useSftpStore()
    store.clearSelection()
    store.lastSelectedLocal = null
    store.lastSelectedRemote = null
  })

  it('Shift 以最后单独点选为锚点，取可见序闭区间', () => {
    const visible = ['a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt']
    store.toggleSelectLocal('b.txt')
    expect(store.lastSelectedLocal).toBe('b.txt')
    store.selectRange('local', 'd.txt', visible)
    expect([...store.selectedLocal].sort()).toEqual(['b.txt', 'c.txt', 'd.txt'].sort())
  })

  it('Shift 反向选择（目标在锚点之前）同样取闭区间', () => {
    const visible = ['a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt']
    store.toggleSelectLocal('d.txt')
    store.selectRange('local', 'b.txt', visible)
    expect([...store.selectedLocal].sort()).toEqual(['b.txt', 'c.txt', 'd.txt'].sort())
  })

  it('锚点不在可见序（已被筛选掉）时按列表首项处理', () => {
    const visible = ['a.txt', 'b.txt', 'c.txt']
    store.lastSelectedLocal = 'x.txt'
    store.selectRange('local', 'b.txt', visible)
    expect([...store.selectedLocal].sort()).toEqual(['a.txt', 'b.txt'].sort())
  })

  it('无锚点（首次即 Shift）按列表首项处理', () => {
    const visible = ['a.txt', 'b.txt', 'c.txt']
    store.selectRange('local', 'c.txt', visible)
    expect([...store.selectedLocal].sort()).toEqual(['a.txt', 'b.txt', 'c.txt'].sort())
  })

  it('Shift 选择后锚点前移到目标', () => {
    const visible = ['a.txt', 'b.txt', 'c.txt']
    store.toggleSelectLocal('a.txt')
    store.selectRange('local', 'c.txt', visible)
    expect(store.lastSelectedLocal).toBe('c.txt')
  })

  it('selectAll 只覆盖传入的可见集', () => {
    store.selectAll('local', ['a.txt', 'c.txt'])
    expect([...store.selectedLocal].sort()).toEqual(['a.txt', 'c.txt'].sort())
  })

  it('两侧选择与锚点互不干扰', () => {
    store.toggleSelectLocal('a.txt')
    store.toggleSelectRemote('x.txt')
    expect(store.lastSelectedLocal).toBe('a.txt')
    expect(store.lastSelectedRemote).toBe('x.txt')
    store.selectRange('remote', 'z.txt', ['x.txt', 'y.txt', 'z.txt'])
    expect([...store.selectedLocal].sort()).toEqual(['a.txt'].sort())
    expect([...store.selectedRemote].sort()).toEqual(['x.txt', 'y.txt', 'z.txt'].sort())
  })
})
