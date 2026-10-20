/**
 * SFTP 路径收藏 store：localStorage 脏数据容错 + 作用域隔离 + 上限契约
 *
 * 与 quickCommands.test.ts 同款最小 localStorage 垫片（测试跑在 node 环境）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  MAX_BOOKMARKS_PER_SCOPE,
  STORAGE_KEY,
  labelFromPath,
  useSftpBookmarksStore,
} from '@/stores/sftpBookmarks'

const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}

function seed(payload: unknown) {
  lsMap.set(STORAGE_KEY, JSON.stringify(payload))
}

function saved() {
  return JSON.parse(lsMap.get(STORAGE_KEY) as string) as {
    local: Array<{ path: string; label: string; order: number }>
    remote: Record<string, Array<{ path: string }>>
  }
}

describe('labelFromPath', () => {
  it('POSIX 路径取末段，尾斜杠不影响', () => {
    expect(labelFromPath('/var/log/nginx')).toBe('nginx')
    expect(labelFromPath('/data/backup/')).toBe('backup')
  })

  it('Windows 路径按反斜杠取末段', () => {
    expect(labelFromPath('D:\\workspace\\project')).toBe('project')
  })

  it('根路径原样回落，不变成空串', () => {
    expect(labelFromPath('/')).toBe('/')
    expect(labelFromPath('C:\\')).toBe('C:')
    expect(labelFromPath('')).toBe('/')
  })
})

describe('sftpBookmarks store', () => {
  beforeEach(() => {
    lsMap.clear()
    setActivePinia(createPinia())
  })

  it('脏记录只丢自己，不连坐整表', () => {
    seed({
      local: [
        { id: 'a', path: '/var/log', label: 'log', order: 0 },
        null,
        'garbage',
        { id: 'b', path: '/www' },
      ],
      remote: { 'u@h:22': [{ path: '/srv' }] },
    })
    const store = useSftpBookmarksStore()
    store.load()
    expect(store.listFor('local', '').map(b => b.path)).toEqual(['/var/log', '/www'])
    expect(store.listFor('remote', 'u@h:22').map(b => b.path)).toEqual(['/srv'])
  })

  it('缺 id 补齐、非法 order 退回位置序号', () => {
    seed({ local: [{ path: '/a' }, { id: '', path: '/b', order: 'oops' }] })
    const store = useSftpBookmarksStore()
    store.load()
    const rows = store.listFor('local', '')
    expect(rows.every(b => typeof b.id === 'string' && b.id.length > 0)).toBe(true)
    expect(rows.map(b => b.order)).toEqual([0, 1])
  })

  it('历史数据里同路径重复时只保留第一条', () => {
    seed({ local: [{ id: 'a', path: '/dup', order: 0 }, { id: 'b', path: '/dup', order: 1 }] })
    const store = useSftpBookmarksStore()
    store.load()
    expect(store.listFor('local', '').map(b => b.id)).toEqual(['a'])
  })

  it('裸数组旧格式按本地收藏读，不整份丢弃', () => {
    seed([{ id: 'a', path: '/legacy', label: 'legacy', order: 0 }])
    const store = useSftpBookmarksStore()
    store.load()
    expect(store.listFor('local', '').map(b => b.path)).toEqual(['/legacy'])
  })

  it('toggle 在收藏与取消之间切换，并如实落盘', () => {
    const store = useSftpBookmarksStore()
    store.load()
    expect(store.toggle('local', '', '/var/log')).toBe('added')
    expect(store.isBookmarked('local', '', '/var/log')).toBe(true)
    expect(saved().local.map(b => b.path)).toEqual(['/var/log'])

    expect(store.toggle('local', '', '/var/log')).toBe('removed')
    expect(store.isBookmarked('local', '', '/var/log')).toBe(false)
    expect(saved().local).toEqual([])
  })

  it('本地与远程、不同 host 之间互不串档', () => {
    const store = useSftpBookmarksStore()
    store.load()
    store.toggle('local', '', '/local-a')
    store.toggle('remote', 'u@h1:22', '/remote-1')
    store.toggle('remote', 'u@h2:22', '/remote-2')

    expect(store.listFor('local', '').map(b => b.path)).toEqual(['/local-a'])
    expect(store.listFor('remote', 'u@h1:22').map(b => b.path)).toEqual(['/remote-1'])
    expect(store.listFor('remote', 'u@h2:22').map(b => b.path)).toEqual(['/remote-2'])
    expect(saved().remote['u@h1:22'].map(b => b.path)).toEqual(['/remote-1'])
  })

  it('远程 hostKey 为空时不写入（离线态不产生无主收藏）', () => {
    const store = useSftpBookmarksStore()
    store.load()
    expect(store.toggle('remote', '', '/nowhere')).toBe('noop')
    expect(store.listFor('remote', '')).toEqual([])
    expect(lsMap.get(STORAGE_KEY)).toBeUndefined()
  })

  it('达到上限后不再新增，且不影响取消已有收藏', () => {
    const store = useSftpBookmarksStore()
    store.load()
    for (let i = 0; i < MAX_BOOKMARKS_PER_SCOPE; i++) {
      expect(store.toggle('local', '', `/dir-${i}`)).toBe('added')
    }
    expect(store.toggle('local', '', '/over-limit')).toBe('limit')
    expect(store.listFor('local', '')).toHaveLength(MAX_BOOKMARKS_PER_SCOPE)

    expect(store.toggle('local', '', '/dir-0')).toBe('removed')
    expect(store.toggle('local', '', '/after-remove')).toBe('added')
  })

  it('remove 按 id 删除并落盘；不存在的 id 不写盘', () => {
    const store = useSftpBookmarksStore()
    store.load()
    store.toggle('local', '', '/a')
    const [row] = store.listFor('local', '')
    expect(store.remove('local', '', row.id)).toBe(true)
    expect(store.listFor('local', '')).toEqual([])
    expect(store.remove('local', '', 'missing')).toBe(false)
  })

  it('重新 load 能读回上一位用户写入的收藏顺序', () => {
    const first = useSftpBookmarksStore()
    first.load()
    first.toggle('local', '', '/a')
    first.toggle('local', '', '/b')

    setActivePinia(createPinia())
    const second = useSftpBookmarksStore()
    second.load()
    expect(second.listFor('local', '').map(b => b.path)).toEqual(['/a', '/b'])
  })
})
