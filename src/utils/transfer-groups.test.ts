/**
 * transfer-groups —— 目录任务折叠成组头之后，数字还对不对
 *
 * 钉住的是"折叠"这一步最容易悄悄错的地方：
 *   · 一个成员的组不该长出组头（否则多一层没信息的壳）；
 *   · 组头状态不能被最后一个完成的成员带跑（还有在传的就不是「完成」）；
 *   · 组头汇总必须等于成员求和，且收起时不能谎报"还有 N 项没画"；
 *   · tab 口径必须与 store.activeTransfers 对齐，否则徽标数与列表数对不上。
 */
import { describe, expect, it } from 'vitest'

// ?raw 由 vite/client 声明类型（本文件在 tsconfig.web 下检查），照 variables.test.ts 同一手法
import rawRow from '../components/ssh/TransferRow.vue?raw'
import rawDialog from '../components/ssh/SftpTransferDialog.vue?raw'

import {
  buildRenderUnits,
  commonDir,
  type GroupableTransfer,
  type TransferTabKey,
} from '@/utils/transfer-groups'

const T0 = 1_700_000_000_000

function t(over: Partial<GroupableTransfer> = {}): GroupableTransfer {
  return {
    id: `t${Math.random().toString(36).slice(2, 8)}`,
    name: 'file.bin',
    from: '/src/file.bin',
    to: '/dst/file.bin',
    direction: 'up',
    kind: 'upload',
    status: 'running',
    transferred: 0,
    total: 100,
    startTime: T0,
    ...over,
  }
}

function dirItems(n: number, over: Partial<GroupableTransfer> = {}): GroupableTransfer[] {
  return Array.from({ length: n }, (_, i) => t({
    groupId: 'g1',
    groupLabel: 'dist/',
    name: `f${i}.bin`,
    ...over,
  }))
}

describe('单行与分组的边界', () => {
  it('无 groupId 的任务逐条平铺，不产生组头', () => {
    const units = buildRenderUnits({ transfers: [t(), t()], tab: 'all' })
    expect(units).toHaveLength(2)
    expect(units.every(u => !u.isGroup)).toBe(true)
  })

  it('只有一个成员的组直接当单行：一个组头套一个文件纯属噪音', () => {
    const units = buildRenderUnits({ transfers: dirItems(1), tab: 'all' })
    expect(units).toHaveLength(1)
    expect(units[0].isGroup).toBe(false)
    expect(units[0].ids).toHaveLength(1)
  })

  it('默认收起：组头一行，子行不渲染', () => {
    const units = buildRenderUnits({ transfers: dirItems(5), tab: 'all' })
    expect(units).toHaveLength(1)
    expect(units[0].isGroup).toBe(true)
    expect(units[0].expanded).toBe(false)
    expect(units[0].label).toBe('dist/')
  })

  it('展开后组头在前、子行紧随且标成 child', () => {
    const units = buildRenderUnits({ transfers: dirItems(3), tab: 'all', expanded: new Set(['g1']) })
    expect(units.map(u => u.isGroup)).toEqual([true, false, false, false])
    expect(units.slice(1).every(u => u.isChild)).toBe(true)
  })
})

describe('组头汇总', () => {
  it('字节求和 + 完成计数进 meta', () => {
    const items = [
      t({ groupId: 'g1', groupLabel: 'dist/', transferred: 40, total: 100, status: 'done' }),
      t({ groupId: 'g1', groupLabel: 'dist/', transferred: 30, total: 100, status: 'running' }),
      t({ groupId: 'g1', groupLabel: 'dist/', transferred: 0, total: 100, status: 'pending' }),
    ]
    const head = buildRenderUnits({ transfers: items, tab: 'all' })[0]
    expect(head.transferred).toBe(70)
    expect(head.total).toBe(300)
    expect(head.meta).toBe('1 / 3 个文件')
  })

  it('只要还有一个在传，组头就不能是「完成」', () => {
    const items = [
      t({ groupId: 'g1', groupLabel: 'd/', status: 'done' }),
      t({ groupId: 'g1', groupLabel: 'd/', status: 'running' }),
      t({ groupId: 'g1', groupLabel: 'd/', status: 'pending' }),
    ]
    expect(buildRenderUnits({ transfers: items, tab: 'all' })[0].status).toBe('running')
  })

  it('全完成后组头落到「完成」；混合失败时失败优先于完成', () => {
    const done = [t({ groupId: 'g', groupLabel: 'd/', status: 'done' }), t({ groupId: 'g', groupLabel: 'd/', status: 'done' })]
    expect(buildRenderUnits({ transfers: done, tab: 'all' })[0].status).toBe('done')
    const mixed = [t({ groupId: 'g', groupLabel: 'd/', status: 'done' }), t({ groupId: 'g', groupLabel: 'd/', status: 'error' })]
    expect(buildRenderUnits({ transfers: mixed, tab: 'all' })[0].status).toBe('error')
  })

  it('展开时子行按可操作度排：在传 > 排队 > 已完成', () => {
    const items = [
      t({ groupId: 'g1', groupLabel: 'd/', status: 'pending', name: 'p' }),
      t({ groupId: 'g1', groupLabel: 'd/', status: 'done', name: 'd' }),
      t({ groupId: 'g1', groupLabel: 'd/', status: 'running', name: 'r' }),
    ]
    const units = buildRenderUnits({ transfers: items, tab: 'all', expanded: new Set(['g1']) })
    expect(units.slice(1).map(u => u.status)).toEqual(['running', 'pending', 'done'])
  })
})

describe('渲染上限', () => {
  it('截断到 childLimit，并把未渲染数记在组头上', () => {
    const units = buildRenderUnits({
      transfers: dirItems(10), tab: 'all', expanded: new Set(['g1']), childLimit: 4,
    })
    expect(units).toHaveLength(5)
    expect(units[0].truncated).toBe(6)
  })

  it('收起时不设 truncated：收起是用户的选择，不是渲染上限', () => {
    const units = buildRenderUnits({ transfers: dirItems(10), tab: 'all', childLimit: 4 })
    expect(units[0].truncated ?? 0).toBe(0)
  })

  it('两万成员的组不炸栈（earliestStart 走 reduce 而非 Math.min(...arr)）', () => {
    const items = dirItems(20_000)
    const units = buildRenderUnits({ transfers: items, tab: 'all', childLimit: 50 })
    expect(units[0].startTime).toBe(T0)
  })
})

describe('tab 与筛选', () => {
  it('active = running + paused，与 store.activeTransfers 的未结束口径对齐', () => {
    const items = [
      t({ status: 'running' }), t({ status: 'paused' }), t({ status: 'pending' }),
      t({ status: 'done' }), t({ status: 'error' }), t({ status: 'cancelled' }),
    ]
    const ids = (tab: TransferTabKey) => buildRenderUnits({ transfers: items, tab }).map(u => u.status)
    expect(ids('active')).toEqual(['running', 'paused'])
    expect(ids('queued')).toEqual(['pending'])
    expect(ids('done')).toEqual(['done', 'cancelled'])
    expect(ids('error')).toEqual(['error'])
    expect(ids('all')).toHaveLength(6)
  })

  it('筛选命中名称或任一侧路径', () => {
    const items = [t({ name: 'alpha.js', from: '/x/beta.log' }), t({ name: 'gamma.css' })]
    expect(buildRenderUnits({ transfers: items, tab: 'all', needle: 'alpha' })).toHaveLength(1)
    expect(buildRenderUnits({ transfers: items, tab: 'all', needle: 'BETA' })).toHaveLength(1)
    expect(buildRenderUnits({ transfers: items, tab: 'all', needle: 'zzz' })).toHaveLength(0)
  })
})

describe('排序', () => {
  const items = [
    t({ name: 'b.bin', transferred: 90, total: 100 }),
    t({ name: 'a.bin', transferred: 10, total: 100 }),
    t({ name: 'c.bin', transferred: 50, total: 100 }),
  ]

  it('queue（默认）保持加入顺序', () => {
    expect(buildRenderUnits({ transfers: items, tab: 'all' }).map(u => u.label))
      .toEqual(['b.bin', 'a.bin', 'c.bin'])
  })

  it('按百分比与名称升降序都真的翻转', () => {
    expect(buildRenderUnits({ transfers: items, tab: 'all', sortKey: 'percent' }).map(u => u.transferred))
      .toEqual([10, 50, 90])
    expect(buildRenderUnits({ transfers: items, tab: 'all', sortKey: 'percent', sortDir: -1 }).map(u => u.transferred))
      .toEqual([90, 50, 10])
    expect(buildRenderUnits({ transfers: items, tab: 'all', sortKey: 'name' }).map(u => u.label))
      .toEqual(['a.bin', 'b.bin', 'c.bin'])
  })
})

describe('commonDir', () => {
  it('空集给空串，单条给全路径', () => {
    expect(commonDir([])).toBe('')
    expect(commonDir(['/a/b/c.bin'])).toBe('/a/b/c.bin')
  })

  it('公共前缀回退到最后一个分隔符，不留半截文件名', () => {
    expect(commonDir(['/dist/assets/index-4f2a.js', '/dist/assets/vendor-9c1e.js']))
      .toBe('/dist/assets')
    expect(commonDir(['D:\\dist\\a.js', 'D:\\dist\\b.js'])).toBe('D:\\dist')
  })

  it('完全没有公共前缀时退回第一条，而不是空串', () => {
    expect(commonDir(['/x/a.js', '/y/b.js'])).toBe('/x/a.js')
  })
})

describe('表头与行的列定义不分叉', () => {
  // 表头住在弹窗、行住在 TransferRow，两边各写一份 grid-template-columns。
  // 注释里那句"逐字一致"只是口头承诺 —— 抄第二份迟早分叉（formatSize 曾经三份
  // 副本各错一处，最后收敛进 utils/format 就是同一件事的前车）。这里钉成断言。
  const colsOf = (src: string, selector: string): string => {
    const at = src.indexOf(selector)
    expect(at, `找不到选择器 ${selector}`).toBeGreaterThan(-1)
    const m = src.slice(at).match(/grid-template-columns:\s*([^;]+);/)
    expect(m, `${selector} 里没有 grid-template-columns`).not.toBeNull()
    return m![1].replace(/\s+/g, ' ').trim()
  }

  it('.tc-thead 与 .tr 的列宽完全相同', () => {
    const row = colsOf(rawRow, '.tr {')
    expect(colsOf(rawDialog, '.tc-thead {')).toBe(row)
    // 变异自检：解析器绿着骗人比没有更糟。轨道数按括号外空格切 ——
    // minmax(0, 1fr) 内部那个空格不算分轨。
    const tracks = row.split(/\s+(?![^(]*\))/).filter(Boolean)
    expect(tracks).toHaveLength(9)
    // 表头单元格数必须等于轨道数（最左那列是方向/折叠，没有标题）——
    // 加了列忘了表头，或反过来，都会让整表错位而不会报错。
    // 只数 `{ key: '...'` 这种带引号的成员：类型标注 `Array<{ key: SortKey; ... }>`
    // 里也有 `{ key:` ，按它数会多算一个。
    const cols = rawDialog.match(/const COLS[\s\S]*?\n\]/)
    expect(cols, '找不到 COLS 数组').not.toBeNull()
    expect((cols![0].match(/\{\s*key:\s*'/g) || []).length + 1).toBe(tracks.length)
    expect(colsOf('.x { grid-template-columns: 1fr 2px; }', '.x {')).toBe('1fr 2px')
  })
})
