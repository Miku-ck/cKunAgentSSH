/**
 * 页面清单的三处对账（命令面板 / 快捷键表 / NavPage 联合类型）。
 *
 * `useSshNavigation.ts` 把 NavPage 立为「独立功能页的唯一清单」时写明：加一页
 * 得同时改多处，漏一处的表现是「图标永远不亮，类型检查却过得去」。命令面板的
 * PAGE_ITEMS 和快捷键表 PAGE_SHORTCUTS 都是各自手抄的数组 —— 漏快捷键的表现为
 * 「这页存在但 Ctrl+Shift+数字 打不开」，漏面板条目的表现为「命令面板里搜不到
 * 这页」，两种都不会红。这里把三份清单按集合对齐，形状守卫按本仓约定用 ?raw
 * 读源（node:fs 在 tsconfig.web 下没有 node 类型，见 styles/variables.test.ts）。
 */
import { describe, expect, it } from 'vitest'
import paletteRaw from './CommandPalette.vue?raw'
import navRaw from '@/composables/useSshNavigation.ts?raw'
import { PAGE_SHORTCUTS } from '@/composables/useGlobalShortcuts'

const paletteSrc = paletteRaw.replace(/\r\n/g, '\n')
const navSrc = navRaw.replace(/\r\n/g, '\n')

function pageKeysInPalette(): string[] {
  const start = paletteSrc.indexOf('const PAGE_ITEMS')
  expect(start, 'CommandPalette.vue 里找不到 PAGE_ITEMS —— 页面条目被改名或整体搬走').toBeGreaterThanOrEqual(0)
  const end = paletteSrc.indexOf('\n]', start)
  expect(end, 'PAGE_ITEMS 数组没有闭合的 `]`').toBeGreaterThan(start)
  const keys = [...paletteSrc.slice(start, end).matchAll(/page:\s*'([^']+)'/g)].map((m) => m[1])
  expect(keys.length, 'PAGE_ITEMS 里没切到任何 page —— 切法已失效，别让它空转').toBeGreaterThan(0)
  return keys
}

function navPages(): string[] {
  const decl = navSrc.match(/export type NavPage =([\s\S]*?)\n\s*\n/)
  expect(decl, 'useSshNavigation.ts 里找不到 `export type NavPage =`').toBeTruthy()
  const keys = [...decl![1].matchAll(/'([^']+)'/g)].map((m) => m[1])
  expect(keys.length, 'NavPage 联合里没切到任何页').toBeGreaterThan(0)
  return keys
}

describe('页面清单同源', () => {
  it('命令面板的页面条目 = 快捷键表的页面集合', () => {
    expect(new Set(pageKeysInPalette())).toEqual(new Set(PAGE_SHORTCUTS.map(p => p.page)))
  })

  it('快捷键表覆盖除 home 外的每一个 NavPage', () => {
    const missing = navPages().filter(p => p !== 'home' && !PAGE_SHORTCUTS.some(s => s.page === p))
    expect(missing, `新页 ${missing.join(', ')} 有标题条按钮却没有快捷键`).toEqual([])
  })

  it('序号不重复且从 1 连续排', () => {
    const nums = PAGE_SHORTCUTS.map(p => p.num)
    expect(new Set(nums).size, '两页抢了同一个 Ctrl+Shift+数字').toBe(nums.length)
    expect(nums).toEqual([...nums].sort((a, b) => a - b))
    expect(nums[0]).toBe(1)
    expect(nums[nums.length - 1]).toBe(nums.length)
  })
})
