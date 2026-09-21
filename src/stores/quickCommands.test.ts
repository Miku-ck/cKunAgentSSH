/**
 * 快捷命令 store：localStorage 脏数据的读取容错 + 拖拽排序契约
 *
 * 原实现整份 load 包在一个 try 里，一条脏记录（null / 字符串混进数组）会让
 * item.name 取值抛错 → catch 把 commands 清成 [] → 之后任意一次 addCommand 的
 * save() 把「只剩这一条」写回 localStorage，等于一次读取抖动清空用户全部命令。
 *
 * 测试跑在 node 环境，故先补一个最小 localStorage 垫片。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useQuickCommandsStore, PRESET_COLORS } from '@/stores/quickCommands'

const KEY = 'ckun-ssh-quick-commands'
const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}

function seed(rows: unknown[]) {
  lsMap.set(KEY, JSON.stringify(rows))
}

describe('quickCommands store', () => {
  beforeEach(() => {
    lsMap.clear()
    setActivePinia(createPinia())
  })

  it('脏记录只丢掉它自己，不连坐整表', () => {
    seed([
      { id: 'a', name: 'A', command: 'ls', color: '#fff', order: 0 },
      null,
      'garbage',
      { id: 'b', name: 'B', command: 'pwd', order: 1 },
    ])
    const store = useQuickCommandsStore()
    store.load()
    expect(store.commands.map(c => c.id)).toEqual(['a', 'b'])
    // 缺色补色板色，而不是留 undefined 让边框样式失效
    expect(store.commands[1].color).toBe(PRESET_COLORS[1])
  })

  it('order 缺失或非数字时退回位置序号（否则排序结果不确定）', () => {
    seed([
      { id: 'b', name: 'B', command: 'b', order: 2 },
      { id: 'a', name: 'A', command: 'a', order: 'oops' },
    ])
    const store = useQuickCommandsStore()
    store.load()
    expect(store.sortedCommands.map(c => [c.id, c.order])).toEqual([['a', 1], ['b', 2]])
  })

  it('load 后新增命令，保存的是全量而不是被清空后的列表', () => {
    seed([null, { id: 'a', name: 'A', command: 'ls', order: 0 }])
    const store = useQuickCommandsStore()
    store.load()
    store.addCommand('B', 'pwd')
    const saved = JSON.parse(lsMap.get(KEY) as string) as Array<{ name: string }>
    expect(saved.map(c => c.name)).toEqual(['A', 'B'])
  })

  it('order 乱序的历史数据下，拖拽落点按显示序生效', () => {
    seed([
      { id: 'a', name: 'A', command: 'a', order: 5 },
      { id: 'b', name: 'B', command: 'b', order: 3 },
      { id: 'c', name: 'C', command: 'c', order: 4 },
    ])
    const store = useQuickCommandsStore()
    store.load()
    expect(store.sortedCommands.map(c => c.id)).toEqual(['b', 'c', 'a'])
    store.reorder('c', 'b')
    expect(store.sortedCommands.map(c => c.id)).toEqual(['c', 'b', 'a'])
    expect(store.commands.map(c => c.order)).toEqual(expect.arrayContaining([0, 1, 2]))
  })
})
