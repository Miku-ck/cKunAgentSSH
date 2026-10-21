# 终端体验四件套 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 终端标签状态感知、8 主题 ANSI 色板、cursor/selection/搜索高亮跟 accent、标签溢出菜单 + MRU 切换器，顺带两枚走查 minor。

**Architecture:** 数据层先行（store 会话状态、终端色板），UI 面后接（状态点、MRU 浮层、溢出菜单）。重连状态上移到 ssh store 成单一事实源；ANSI 色板走「组件局部调色板」豁免（JS 常量 + vitest 守卫），不进 variables.css。

**Tech Stack:** Vue 3 (script setup) + Pinia + xterm.js + vitest（node 环境，globalThis 垫片）+ electron-vite。

**Spec:** `docs/superpowers/specs/2026-10-21-terminal-tab-experience-design.md`

## Global Constraints

- 颜色一律走令牌 `var(--*)`；组件里不写 `#hex`/`rgba()`（终端色板与 xterm 装饰色属既有「组件局部调色板」豁免形状：一处声明、有名字、有守卫测试）。
- 终端底恒深：`--bg-terminal` 不动，终端不做浅色底。
- 不动布局骨架：不加垂直标签栏、不分屏、不新增顶栏 tab。
- 长命令完成标记（OSC 133）不在本片，禁止顺手做。
- 工作树有并行会话的未提交改动：**每次 commit 必须按 pathspec 精确添加本任务触碰的文件，禁止 `git add -A`/`git add .`**。
- 改了 `.vue` 模板的任务，验证必须含 `npm run build`（vue-tsc/vitest 不查标签平衡）。
- 测试环境是 node（非 jsdom）：需要 `window`/`localStorage` 时按 `src/stores/ssh-hostkey-scope.test.ts` 的 globalThis 垫片模式。
- 提交信息风格：中文 conventional commits（参照 `git log`，如 `feat(ui): ...` / `refactor(ai): ...`）。

---

### Task 1: 终端 ANSI 色板 + accent 联动（数据层）

**Files:**
- Modify: `src/composables/useTerminalConfig.ts`
- Test: `src/composables/terminal-palettes.test.ts`（新建）

**Interfaces:**
- Consumes: `Theme`/`Accent`/`ACCENT_MAP`/`isHexColor`/`contrastRatio`（`@/utils/appearance-tokens`）；`useSettingsStore` 的 `appearance.theme`/`appearance.accent`/`appearance.terminal.*`。
- Produces:
  - `export type ResolvedTheme = Exclude<Theme, 'system'>`
  - `export function resolveThemeName(theme: Theme): ResolvedTheme`
  - `export const TERMINAL_THEMES: Record<ResolvedTheme, ITheme>`
  - `export function resolveTerminalTheme(): ITheme`（签名不变，语义变为「主题色板 + accent 覆盖 cursor/selection」）
  - `export function accentSearchColors(): { match: string; active: string; border: string }`（Task 2 消费）
  - `watchTerminalConfig(...)`（签名不变，内部新增 theme/accent 监听）

- [ ] **Step 1: 写失败测试**

新建 `src/composables/terminal-palettes.test.ts`：

```ts
/**
 * 终端 ANSI 色板守卫：8 主题齐全、键完整、对比度达标、dark/light 钉住基线。
 * 色板是「组件局部调色板」豁免（xterm 吃不到 CSS 令牌），本测试是它的核对方。
 * TERMINAL_BG 与 variables.css 各主题 --bg-terminal 对账，那边改了这里必须跟着改。
 */
import { describe, expect, it } from 'vitest'
import { contrastRatio } from '@/utils/appearance-tokens'
import { TERMINAL_THEMES, resolveThemeName, type ResolvedTheme } from './useTerminalConfig'

const TERMINAL_BG: Record<ResolvedTheme, string> = {
  dark: '#000000',
  light: '#17181c',
  'blue-screen': '#080d16',
  forest: '#0a120d',
  mint: '#071113',
  bee: '#0f0b05',
  coral: '#100908',
  atelier: '#17181c',
}

const ANSI_KEYS = [
  'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
  'brightBlack', 'brightRed', 'brightGreen', 'brightYellow',
  'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite',
] as const

describe('终端 ANSI 色板', () => {
  it('8 个主题齐全', () => {
    expect(Object.keys(TERMINAL_THEMES).sort()).toEqual(Object.keys(TERMINAL_BG).sort())
  })

  it.each(Object.keys(TERMINAL_BG) as ResolvedTheme[])('%s：16 个 ANSI 键 + 前景/光标/选区齐全', (theme) => {
    const t = TERMINAL_THEMES[theme]
    for (const k of ANSI_KEYS) expect(t[k], k).toMatch(/^#[0-9a-f]{6}$/i)
    expect(t.foreground).toMatch(/^#/)
    expect(t.cursor).toMatch(/^#/)
    expect(t.selectionBackground).toMatch(/^#/)
  })

  it.each(Object.keys(TERMINAL_BG) as ResolvedTheme[])('%s：前景对终端底 ≥4.5，ANSI 色 ≥2.5', (theme) => {
    const t = TERMINAL_THEMES[theme]
    const bg = TERMINAL_BG[theme]
    expect(contrastRatio(t.foreground!, bg)).toBeGreaterThanOrEqual(4.5)
    for (const k of ANSI_KEYS) {
      expect(contrastRatio(t[k]!, bg), `${theme}.${k}`).toBeGreaterThanOrEqual(2.5)
    }
  })

  it('dark/light 钉住现状基线（零回归）', () => {
    for (const theme of ['dark', 'light'] as const) {
      expect(TERMINAL_THEMES[theme]).toMatchObject({
        foreground: '#e6edf3', cursor: '#58a6ff', selectionBackground: '#264f78',
        black: '#111111', red: '#f85149', green: '#3fb950', yellow: '#d29922',
        blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39d2c0', white: '#e6edf3',
        brightBlack: '#6e7681', brightRed: '#ff7b72', brightGreen: '#7ee787',
        brightYellow: '#e3b341', brightBlue: '#79c0ff', brightMagenta: '#d2a8ff',
        brightCyan: '#56d4dd', brightWhite: '#f0f6fc',
      })
    }
  })

  it('resolveThemeName：system 在 node 环境回落 dark，其余原样透传', () => {
    expect(resolveThemeName('dark')).toBe('dark')
    expect(resolveThemeName('atelier')).toBe('atelier')
    expect(['dark', 'light']).toContain(resolveThemeName('system'))
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/composables/terminal-palettes.test.ts`
Expected: FAIL（`TERMINAL_THEMES` 未导出）

- [ ] **Step 3: 实现色板与解析**

`src/composables/useTerminalConfig.ts`：

1. 顶部 import 增加：

```ts
import { ACCENT_MAP, isHexColor, type Theme } from '@/utils/appearance-tokens'
```

（删掉原有的 `isHexColor` 单名 import 行，合并进这条。）

2. 把 `TERMINAL_DARK_THEME` 改名为 `TERMINAL_BASE`（内容不变，作为基线），其后新增：

```ts
export type ResolvedTheme = Exclude<Theme, 'system'>

/** system 主题的运行时解析：浏览器读媒体查询，node（测试/SSR）回落 dark */
export function resolveThemeName(theme: Theme): ResolvedTheme {
  if (theme !== 'system') return theme
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'dark'
}

/**
 * 每主题一套 ANSI 16 色：终端底恒深（--bg-terminal 契约不动），只调色相温度。
 * dark/light 共用基线（中性界面不配色偏）；未列的键继承基线。
 * 守卫：terminal-palettes.test.ts 钉住键完整性、对比度与 dark/light 基线。
 */
export const TERMINAL_THEMES: Record<ResolvedTheme, ITheme> = {
  dark: TERMINAL_BASE,
  light: TERMINAL_BASE,
  'blue-screen': {
    ...TERMINAL_BASE,
    blue: '#6cb6ff', brightBlue: '#8ecbff',
    cyan: '#4fd8e0', brightCyan: '#76e4ea',
    magenta: '#c8a1ff', green: '#56d364',
  },
  forest: {
    ...TERMINAL_BASE,
    red: '#ff6a55', green: '#7ed491', brightGreen: '#97e3a8',
    yellow: '#e0c060', blue: '#82aaff', cyan: '#4fd6be',
  },
  mint: {
    ...TERMINAL_BASE,
    red: '#fb7185', green: '#6ee7b7', brightGreen: '#8ff0c8',
    yellow: '#f0c674', blue: '#7dd3fc', magenta: '#c4b5fd',
    cyan: '#5eead4', brightCyan: '#86f0e0',
  },
  bee: {
    ...TERMINAL_BASE,
    red: '#ff7a66', green: '#9ece6a', brightGreen: '#b5e085',
    yellow: '#ffc777', brightYellow: '#ffd892',
    blue: '#82aaff', cyan: '#7dcfff',
  },
  coral: {
    ...TERMINAL_BASE,
    red: '#ff8a7a', brightRed: '#ffa496', magenta: '#f0a6c0',
    yellow: '#f0b866', green: '#a6d189', blue: '#8caaee', cyan: '#81c8be',
  },
  atelier: {
    ...TERMINAL_BASE,
    foreground: '#e8e2d5', white: '#e8e2d5', brightWhite: '#f5efe2',
    red: '#d67a76', brightRed: '#e3928e',
    green: '#9db384', brightGreen: '#b3c79b',
    yellow: '#d8b26a', brightYellow: '#e6c684',
    blue: '#7e9cc4', brightBlue: '#98b3d6',
    magenta: '#b594b6', brightMagenta: '#caadcc',
    cyan: '#7fb5ac', brightCyan: '#99c9c0',
    brightBlack: '#6e6a60',
  },
}
```

3. `resolveTerminalTheme()` 改为（保持导出签名不变）：

```ts
/**
 * 按设置解析当前终端主题：主题色板定 16 个 ANSI 色，accent 覆盖 cursor/selection
 * （用户显式选的强调色优先；ACCENT_MAP 查不到时回落色板自带值）。
 * solid 背景模式再把用户色转 rgba 写进 background。
 */
export function resolveTerminalTheme(): ITheme {
  const store = useSettingsStore()
  const a = store.settings.appearance
  const palette = TERMINAL_THEMES[resolveThemeName(a.theme)]
  const accentHex = ACCENT_MAP[a.accent]?.hex
  const themed: ITheme = accentHex
    ? { ...palette, cursor: accentHex, selectionBackground: hexToRgba(accentHex, 35) }
    : palette
  const t = a.terminal
  if (t.backgroundMode !== 'solid') return themed
  return { ...themed, background: hexToRgba(t.backgroundColor, t.backgroundOpacity) }
}
```

4. 文件末尾新增（Task 2 消费）：

```ts
/**
 * 终端内搜索的三枚装饰色：从当前 accent 派生（SearchAddon 的装饰直接画在
 * canvas/overview ruler 上，吃不到 CSS 令牌，只能 JS 侧给值）。
 * match 用 40% 透明弱化普通命中，active/border 用实色标当前命中。
 */
export function accentSearchColors(): { match: string; active: string; border: string } {
  const accentHex = ACCENT_MAP[useSettingsStore().settings.appearance.accent]?.hex
  if (!accentHex) return { match: 'rgba(88, 166, 255, 0.4)', active: '#58a6ff', border: '#58a6ff' }
  return { match: hexToRgba(accentHex, 40), active: accentHex, border: accentHex }
}
```

5. `watchTerminalConfig` 里 `unsubBg` 的 watch 源数组末尾追加两项（主题/强调色变化同样要重解析终端 theme）：

```ts
      () => store.settings.appearance.theme,
      () => store.settings.appearance.accent,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/composables/terminal-palettes.test.ts`
Expected: PASS（5 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/composables/useTerminalConfig.ts src/composables/terminal-palettes.test.ts
git commit -m "feat(terminal): 8 主题 ANSI 色板 + cursor/selection 跟随强调色"
```

---

### Task 2: 终端搜索高亮跟 accent

**Files:**
- Modify: `src/components/ssh/TermSearchBar.vue:48-53,71-81`
- Test: `src/components/ssh/TermSearchBar.accent.guards.test.ts`（新建）

**Interfaces:**
- Consumes: `accentSearchColors()`（Task 1）。
- Produces: 无新导出；`searchOptions()` 的 decorations 改为 accent 派生。

- [ ] **Step 1: 写失败守卫测试**

新建 `src/components/ssh/TermSearchBar.accent.guards.test.ts`（源码守卫，与项目既有 `*.guards.test.ts` 同形状）：

```ts
/** 搜索高亮必须走 accent 派生：GitHub 蓝写死的三枚颜色不许回来 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const src = readFileSync(
  fileURLToPath(new URL('./TermSearchBar.vue', import.meta.url)),
  'utf8',
)

describe('TermSearchBar 高亮色来源', () => {
  it('decorations 由 accentSearchColors() 派生', () => {
    expect(src).toContain('accentSearchColors()')
  })

  it('不再出现写死的高亮 hex', () => {
    expect(src).not.toContain('#8b949e')
    expect(src).not.toContain('#58a6ff')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/ssh/TermSearchBar.accent.guards.test.ts`
Expected: FAIL（源码还没改）

- [ ] **Step 3: 改 TermSearchBar**

`src/components/ssh/TermSearchBar.vue`：

1. 删除第 48-53 行三枚常量及头注，import 改为：

```ts
import { onMounted, ref } from 'vue'
import { accentSearchColors } from '@/composables/useTerminalConfig'
```

2. `searchOptions()` 改为：

```ts
function searchOptions() {
  const colors = accentSearchColors()
  return {
    caseSensitive: caseSensitive.value,
    regex: regex.value,
    decorations: {
      matchOverviewRuler: colors.match,
      activeMatchColorOverviewRuler: colors.active,
      activeMatchBorder: colors.border,
    } as const,
  }
}
```

（查询时取色：accent 切换后下一次 find 自然生效，无需监听。）

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/components/ssh/TermSearchBar.accent.guards.test.ts src/composables/terminal-palettes.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/components/ssh/TermSearchBar.vue src/components/ssh/TermSearchBar.accent.guards.test.ts
git commit -m "feat(terminal): 搜索高亮三枚装饰色改从强调色派生"
```

---

### Task 3: 会话状态进 store（connStatus / hasActivity / MRU 栈）

**Files:**
- Modify: `src/stores/ssh.ts`
- Test: `src/stores/ssh-session-status.test.ts`（新建）

**Interfaces:**
- Consumes: `window.electronAPI.onSshReconnect`（preload 已有，事件负载 `SshReconnectStatus`：`{ id, phase: 'reconnecting'|'retry'|'success'|'gave-up', attempt, maxAttempts, reason? }`）。
- Produces:
  - `TerminalSession` 新增可选字段：`hasActivity?: boolean`、`reconnect?: { attempt: number; maxAttempts: number } | null`、`reconnectGaveUp?: boolean`
  - `store.markActivity(id: string): void`
  - `store.mruIds: Ref<string[]>`（最久未用在尾，栈顶是最近激活）
  - `switchSession(id)` 副作用：清该会话 `hasActivity`、把 id touch 到 `mruIds` 栈顶（Task 4/5 消费此口径）

- [ ] **Step 1: 写失败测试**

新建 `src/stores/ssh-session-status.test.ts`（垫片与 mock 模式照抄 `ssh-hostkey-scope.test.ts`）：

```ts
/** 会话状态层：重连事件映射、活动标记、MRU 栈 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

type ReconnectCb = (d: { id: string; phase: string; attempt: number; maxAttempts: number; reason?: string }) => void
let reconnectCb: ReconnectCb | null = null
const onSshReconnect = vi.fn((cb: ReconnectCb) => { reconnectCb = cb; return () => {} })
const sshListConnections = vi.fn(async () => [])

const g = globalThis as unknown as Record<string, unknown>
g.window = { electronAPI: { onSshReconnect, sshListConnections } }
g.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }

vi.mock('./toast', () => ({
  useToastStore: () => ({ info: vi.fn(), warning: vi.fn(), error: vi.fn(), success: vi.fn() }),
}))
vi.mock('@/api/client', () => ({
  toFriendlyMessage: (e: unknown) => String(e),
  unwrapIpcErrorEnvelope: (s: string) => s,
}))

import { useSSHStore } from './ssh'

function seed(store: ReturnType<typeof useSSHStore>, ...ids: string[]) {
  for (const id of ids) {
    store.sessions.push({ id, name: id, type: 'local', shell: 'CMD' })
  }
}

describe('会话状态层', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    reconnectCb = null
  })

  it('init() 注册重连跟踪监听', async () => {
    const store = useSSHStore()
    await store.init()
    expect(onSshReconnect).toHaveBeenCalledTimes(1)
  })

  it('重连事件映射：reconnecting/retry→reconnect 对象；success→清空；gave-up(exhausted)→reconnectGaveUp', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 's1')
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnect).toEqual({ attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnectGaveUp).toBeFalsy()
    reconnectCb!({ id: 's1', phase: 'gave-up', attempt: 3, maxAttempts: 3, reason: 'exhausted' })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(true)
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    reconnectCb!({ id: 's1', phase: 'success', attempt: 1, maxAttempts: 3 })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(false)
  })

  it('gave-up 的 cancelled/config-missing 清状态不置 gaveUp；未知会话静默丢弃', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 's1')
    reconnectCb!({ id: 'ghost', phase: 'reconnecting', attempt: 1, maxAttempts: 3 }) // 不炸
    reconnectCb!({ id: 's1', phase: 'reconnecting', attempt: 1, maxAttempts: 3 })
    reconnectCb!({ id: 's1', phase: 'gave-up', attempt: 0, maxAttempts: 3, reason: 'cancelled' })
    expect(store.sessions[0].reconnect).toBeNull()
    expect(store.sessions[0].reconnectGaveUp).toBe(false)
  })

  it('markActivity 置位；switchSession 清零并 touch MRU 栈顶', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 'a', 'b', 'c')
    store.markActivity('b')
    expect(store.sessions.find(s => s.id === 'b')!.hasActivity).toBe(true)
    store.switchSession('a')
    store.switchSession('c')
    store.switchSession('b')
    expect(store.mruIds.slice(0, 3)).toEqual(['b', 'c', 'a'])
    expect(store.sessions.find(s => s.id === 'b')!.hasActivity).toBe(false)
  })

  it('removeSession 同步剔除 MRU 栈', async () => {
    const store = useSSHStore()
    await store.init()
    seed(store, 'a', 'b')
    store.switchSession('a')
    store.switchSession('b')
    store.removeSession('b')
    expect(store.mruIds).not.toContain('b')
    expect(store.mruIds).toContain('a')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/stores/ssh-session-status.test.ts`
Expected: FAIL（`markActivity`/`mruIds` 不存在）

- [ ] **Step 3: 实现 store 改动**

`src/stores/ssh.ts`：

1. `TerminalSession` 接口追加：

```ts
  /** 后台会话有新输出（切走时置位，激活时清零） */
  hasActivity?: boolean
  /** 重连进行中（主进程 ssh:reconnect 事件驱动）；null = 无重连 */
  reconnect?: { attempt: number; maxAttempts: number } | null
  /** 自动重连已放弃（等待用户手动重连） */
  reconnectGaveUp?: boolean
```

2. State 区（`activeSessionId` 声明之后）加：

```ts
  /** MRU 栈：栈顶是最近激活的会话（Ctrl+Tab 切换器与「回到上次会话」的数据源） */
  const mruIds = ref<string[]>([])

  /** 后台活动标记：终端数据到达且会话非活跃时由终端组件置位 */
  function markActivity(id: string): void {
    if (id === activeSessionId.value) return
    const s = sessions.value.find(x => x.id === id)
    if (s) s.hasActivity = true
  }
```

3. `switchSession` 改为：

```ts
  function switchSession(id: string) {
    activeSessionId.value = id
    // MRU touch：任何激活路径（标签/快捷键/命令面板）都汇到这一个出口
    mruIds.value = [id, ...mruIds.value.filter(x => x !== id)]
    // 看见即清：激活的会话不再有「未读输出」
    const s = sessions.value.find(x => x.id === id)
    if (s) s.hasActivity = false
  }
```

4. `removeSession` 尾部（`delete cwdBySession.value[id]` 之后）加：

```ts
    mruIds.value = mruIds.value.filter(x => x !== id)
```

5. 重连跟踪：新增函数（放在 `_setupHostKeyNotifications` 之后），并在 `init()` 里紧接 `_setupHostKeyNotifications()` 后调用 `_setupReconnectTracking()`：

```ts
  /** s-标签状态：重连事件的全局跟踪（RemoteTerminal 的状态条与标签状态点共用这份状态） */
  function _setupReconnectTracking(): void {
    const api = window.electronAPI
    if (!api?.onSshReconnect) return
    const off = api.onSshReconnect((data) => {
      const s = sessions.value.find(x => x.id === data.id)
      if (!s) return // 重连完成前用户已关标签：静默丢弃
      if (data.phase === 'reconnecting' || data.phase === 'retry') {
        s.reconnect = { attempt: data.attempt, maxAttempts: data.maxAttempts }
        s.reconnectGaveUp = false
      } else if (data.phase === 'success') {
        s.reconnect = null
        s.reconnectGaveUp = false
      } else if (data.phase === 'gave-up') {
        s.reconnect = null
        // cancelled/config-missing 是「收摊」不是「失败」：清状态，不亮红点
        s.reconnectGaveUp = data.reason === 'exhausted' || data.reason === 'not-retryable'
      }
    })
    if (_setupScope) _setupScope.run(() => onScopeDispose(() => off()))
  }
```

6. store 的 return 导出表追加：`markActivity`、`mruIds`。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/stores/ssh-session-status.test.ts src/stores/ssh-hostkey-scope.test.ts`
Expected: PASS（两个文件全绿，确认没碰坏 hostkey 监听）

- [ ] **Step 5: 提交**

```bash
git add src/stores/ssh.ts src/stores/ssh-session-status.test.ts
git commit -m "feat(ssh): 会话状态层——重连跟踪/后台活动标记/MRU 栈入 store"
```

---

### Task 4: 标签状态点 + 活动标记接线（UI 面）

**Files:**
- Create: `src/utils/tabStatus.ts`
- Test: `src/utils/tabStatus.test.ts`（新建）
- Modify: `src/views/ssh/SshView.vue:16-40`（模板）、`<style>` 段
- Modify: `src/components/ssh/LocalTerminal.vue`（onPtyData 处理器）
- Modify: `src/components/ssh/RemoteTerminal.vue`（onSshData 处理器 + 状态条改读 store）

**Interfaces:**
- Consumes: Task 3 的 `session.hasActivity`/`session.reconnect`/`session.reconnectGaveUp`/`store.markActivity`。
- Produces:
  - `export function tabStatusKind(s: Pick<TerminalSession, 'id' | 'hasActivity' | 'reconnect' | 'reconnectGaveUp'>, activeId: string): 'gave-up' | 'reconnecting' | 'activity' | null`（Task 5/6 的切换器与溢出菜单复用同一判据）

- [ ] **Step 1: 写失败测试**

新建 `src/utils/tabStatus.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import { tabStatusKind } from './tabStatus'

describe('tabStatusKind：标签状态点判据', () => {
  const base = { id: 's1' }

  it('健康会话无状态点', () => {
    expect(tabStatusKind({ ...base }, '')).toBeNull()
  })

  it('后台活动：非活跃会话才有，活跃会话不亮（人正看着它）', () => {
    expect(tabStatusKind({ ...base, hasActivity: true }, 'other')).toBe('activity')
    expect(tabStatusKind({ ...base, hasActivity: true }, 's1')).toBeNull()
  })

  it('重连中：活跃会话也亮（状态比位置重要）', () => {
    const s = { ...base, reconnect: { attempt: 1, maxAttempts: 3 } }
    expect(tabStatusKind(s, 's1')).toBe('reconnecting')
    expect(tabStatusKind(s, 'other')).toBe('reconnecting')
  })

  it('优先级：gave-up > reconnecting > activity', () => {
    expect(tabStatusKind({ ...base, reconnectGaveUp: true, reconnect: { attempt: 1, maxAttempts: 3 }, hasActivity: true }, 'other')).toBe('gave-up')
    expect(tabStatusKind({ ...base, reconnect: { attempt: 1, maxAttempts: 3 }, hasActivity: true }, 'other')).toBe('reconnecting')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/utils/tabStatus.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 tabStatus + 接线**

1. 新建 `src/utils/tabStatus.ts`：

```ts
import type { TerminalSession } from '@/stores/ssh'

/**
 * 标签状态点的单一判据：标签栏、溢出菜单、MRU 切换器三处共用。
 * 优先级 gave-up > reconnecting > activity；活动点在活跃标签上不亮
 * （人正看着它），重连/断连不受此限（状态比位置重要）。
 */
export function tabStatusKind(
  s: Pick<TerminalSession, 'id' | 'hasActivity' | 'reconnect' | 'reconnectGaveUp'>,
  activeId: string,
): 'gave-up' | 'reconnecting' | 'activity' | null {
  if (s.reconnectGaveUp) return 'gave-up'
  if (s.reconnect) return 'reconnecting'
  if (s.hasActivity && s.id !== activeId) return 'activity'
  return null
}
```

2. `src/views/ssh/SshView.vue` 模板：`.term-tab` 里图标 svg 之前插入：

```html
              <span
                v-if="tabStatusKind(s, store.activeSessionId)"
                class="tab-dot"
                :class="tabStatusKind(s, store.activeSessionId)"
                aria-hidden="true"
              />
```

script 的 import 区加：

```ts
import { tabStatusKind } from '@/utils/tabStatus'
```

`<style>` 段（`.term-tab svg` 规则之前）加：

```css
/* 状态点：后台活动=强调色 / 重连中=警示色脉冲 / 断连放弃=红 */
.tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tab-dot.activity { background: var(--accent-primary); }
.tab-dot.gave-up { background: var(--accent-red); }

.tab-dot.reconnecting {
  background: var(--status-warn-text);
  animation: tab-dot-pulse 1.2s ease-in-out infinite;
}

@keyframes tab-dot-pulse {
  50% { opacity: 0.25; }
}
```

（脉冲停效由全局 `[data-animation="false"]` 兜底，无需自处理。）

3. `src/components/ssh/LocalTerminal.vue`：`onPtyData` 处理器（约 235 行，`if (ptyId === props.ptyId)` 分支内、`terminal?.write` 之后）加一行：

```ts
        sshStore.markActivity(props.ptyId)
```

（该文件已持有 `sshStore`，无需新增 import。）

4. `src/components/ssh/RemoteTerminal.vue`：
   - import 区加 `import { useSSHStore } from '@/stores/ssh'`，setup 里加 `const sshStore = useSSHStore()`。
   - `onSshData` 处理器（`if (id === props.sshId)` 分支内）加 `sshStore.markActivity(id)`。
   - 状态条改读 store：删除本地 `reconnectState`/`reconnectGaveUp` 两个 `ref` 声明，替换为：

```ts
// 重连状态条改读 store（标签状态点的单一事实源）；系统行写入仍由本地事件处理器负责
const sessionSelf = computed(() => sshStore.sessions.find(s => s.id === props.sshId))
const reconnectState = computed(() => sessionSelf.value?.reconnect ?? null)
const reconnectGaveUp = computed(() => sessionSelf.value?.reconnectGaveUp ?? false)
```

   - import 行补 `computed`（`from 'vue'`）。
   - 本地 `onSshReconnect` 处理器瘦身：删掉所有对 `reconnectState.value`/`reconnectGaveUp.value` 的赋值，只保留 `appendReconnectLine(...)` 系统行写入与 `config-missing` 时的 `emit('close')`（store 已负责状态，组件只负责终端内文字）。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/utils/tabStatus.test.ts src/stores/`
Expected: PASS

- [ ] **Step 5: 模板改动验证（build 必须跑）**

Run: `npm run typecheck && npm run build`
Expected: 0 错误（vue-tsc/vitest 不查模板标签平衡，vite:vue 才查）

- [ ] **Step 6: 提交**

```bash
git add src/utils/tabStatus.ts src/utils/tabStatus.test.ts src/views/ssh/SshView.vue src/components/ssh/LocalTerminal.vue src/components/ssh/RemoteTerminal.vue
git commit -m "feat(ui): 终端标签状态点——后台活动/重连中/断连放弃"
```

---

### Task 5: MRU 切换器（Ctrl+Tab 升级）

**Files:**
- Modify: `src/composables/useGlobalShortcuts.ts`
- Modify: `src/composables/useGlobalShortcuts.test.ts`
- Create: `src/components/ssh/MruSwitcher.vue`
- Modify: `src/views/ssh/SshView.vue`（挂载 MruSwitcher）

**Interfaces:**
- Consumes: `store.mruIds`（Task 3）、`tabStatusKind`（Task 4）。
- Produces:
  - `export function mruInitialIndex(len: number): number`（恒 1，单会话由调用方拦）
  - `export function mruStep(len: number, index: number, dir: 1 | -1): number`
  - `export const mruSwitcher: { open: boolean; index: number }`（reactive 单例，MruSwitcher.vue 消费）
  - `useGlobalShortcuts` 返回值变为 `{ mruSwitcher }`（SshView 透传给组件）

- [ ] **Step 1: 改写测试**

`src/composables/useGlobalShortcuts.test.ts` 全量替换为：

```ts
import { describe, expect, it } from 'vitest'
import { mruInitialIndex, mruStep } from './useGlobalShortcuts'

describe('MRU 切换器的纯函数', () => {
  it('打开时高亮落在「上一个会话」（栈顶下一位）', () => {
    expect(mruInitialIndex(2)).toBe(1)
    expect(mruInitialIndex(9)).toBe(1)
  })

  it('连点 Tab 在栈内循环移动', () => {
    expect(mruStep(3, 1, 1)).toBe(2)
    expect(mruStep(3, 2, 1)).toBe(0)
    expect(mruStep(3, 0, -1)).toBe(2)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/composables/useGlobalShortcuts.test.ts`
Expected: FAIL（`mruInitialIndex`/`mruStep` 未导出，`nextSessionId` 已删）

- [ ] **Step 3: 实现 MRU 状态机**

`src/composables/useGlobalShortcuts.ts`：

1. 删除 `nextSessionId` 函数及其头注，替换为：

```ts
import { reactive } from 'vue'

/** MRU 浮层的共享状态（快捷键侧写、MruSwitcher.vue 读） */
export const mruSwitcher = reactive({ open: false, index: 0 })

/** 打开浮层时的初始高亮：恒为「上一个会话」（栈顶下一位） */
export function mruInitialIndex(len: number): number {
  return len > 1 ? 1 : 0
}

/** 按住 Ctrl 连点 Tab：在 MRU 栈内循环移动 */
export function mruStep(len: number, index: number, dir: 1 | -1): number {
  return (index + dir + len) % len
}
```

2. `cycleSession` 与 handler 的 Tab 分支替换为 MRU 状态机；`useGlobalShortcuts` 内加：

```ts
  /** 当前参与切换的 MRU 会话序列（剔除已关闭残留） */
  function mruSessionIds(): string[] {
    const alive = new Set(store.sessions.map(s => s.id))
    return store.mruIds.filter(id => alive.has(id))
  }

  function mruCommit(): void {
    const ids = mruSessionIds()
    const target = ids[mruSwitcher.index]
    mruSwitcher.open = false
    mruSwitcher.index = 0
    if (target) deps.activateTerminal(target)
  }

  function mruCancel(): void {
    mruSwitcher.open = false
    mruSwitcher.index = 0
  }

  function onKeyup(e: KeyboardEvent): void {
    // 松开 Ctrl = 落定：切到高亮会话
    if ((e.key === 'Control' || e.key === 'Meta') && mruSwitcher.open) mruCommit()
  }

  function onBlur(): void {
    // 窗口失焦时 Ctrl 的 keyup 可能丢失：直接落定，别留一个关不掉的浮层
    if (mruSwitcher.open) mruCommit()
  }
```

3. `handler` 的改动：
   - 函数体最开头（Ctrl 守卫**之前**）加 Esc 取消分支：

```ts
    if (e.key === 'Escape' && mruSwitcher.open) {
      e.preventDefault()
      mruCancel()
      return
    }
```

   - `if (e.key === 'Tab')` 分支改为：

```ts
    if (e.key === 'Tab') {
      e.preventDefault()
      const dir = e.shiftKey ? -1 : 1
      if (!mruSwitcher.open) {
        // 单会话保持旧行为：直接切过去（首页没有标签条，这是唯一可见入口）
        if (store.sessions.length <= 1) {
          const only = store.sessions[0]
          if (only) deps.activateTerminal(only.id)
          return
        }
        mruSwitcher.open = true
        mruSwitcher.index = mruInitialIndex(mruSessionIds().length)
        return
      }
      mruSwitcher.index = mruStep(mruSessionIds().length, mruSwitcher.index, dir)
      return
    }
```

4. 挂载/卸载（替换原有两行）：

```ts
  onMounted(() => {
    window.addEventListener('keydown', handler)
    window.addEventListener('keyup', onKeyup)
    window.addEventListener('blur', onBlur)
  })
  onUnmounted(() => {
    window.removeEventListener('keydown', handler)
    window.removeEventListener('keyup', onKeyup)
    window.removeEventListener('blur', onBlur)
    mruCancel()
  })
```

5. 函数末尾 `return { mruSwitcher }`。

6. 新建 `src/components/ssh/MruSwitcher.vue`：

```vue
<script setup lang="ts">
/** MRU 切换浮层：Ctrl+Tab 按住期间的会话列表。贴标签条下缘，不抢焦点不居中。 */
import { computed } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { mruSwitcher } from '@/composables/useGlobalShortcuts'
import { tabStatusKind } from '@/utils/tabStatus'
import IconMonitor from '@/components/icons/IconMonitor.vue'
import IconTerminal from '@/components/icons/IconTerminal.vue'

const store = useSSHStore()

const entries = computed(() => {
  const alive = new Map(store.sessions.map(s => [s.id, s]))
  return store.mruIds.filter(id => alive.has(id)).map(id => alive.get(id)!)
})
</script>

<template>
  <div class="mru-switcher" role="listbox" aria-label="切换终端会话">
    <div
      v-for="(s, i) in entries"
      :key="s.id"
      class="mru-row"
      :class="{ on: i === mruSwitcher.index }"
      role="option"
      :aria-selected="i === mruSwitcher.index"
    >
      <span
        v-if="tabStatusKind(s, store.activeSessionId)"
        class="mru-dot"
        :class="tabStatusKind(s, store.activeSessionId)"
        aria-hidden="true"
      />
      <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
      <IconTerminal v-else :width="12" :height="12" :stroke-width="2" />
      <span class="mru-name">{{ s.name }}</span>
    </div>
  </div>
</template>

<style scoped>
.mru-switcher {
  position: fixed;
  top: calc(var(--titlebar-h) + var(--tabbar-h) + 4px);
  left: 8px;
  z-index: 900;
  min-width: 200px;
  max-width: 320px;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.mru-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 12px;
}

.mru-row.on {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.mru-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mru-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.mru-dot.activity { background: var(--accent-primary); }
.mru-dot.gave-up { background: var(--accent-red); }
.mru-dot.reconnecting { background: var(--status-warn-text); }
</style>
```

7. `src/views/ssh/SshView.vue`：
   - import 加 `import MruSwitcher from '@/components/ssh/MruSwitcher.vue'`。
   - `useGlobalShortcuts({...})` 调用处接返回值：`const { mruSwitcher } = useGlobalShortcuts({ ... })`。
   - 模板 `</div><!-- /.ssh-main -->` 之前（QuickCommandBar 之后）加：`<MruSwitcher v-if="mruSwitcher.open" />`。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/composables/useGlobalShortcuts.test.ts`
Expected: PASS

- [ ] **Step 5: 全量前端验证**

Run: `npm run typecheck && npx vitest run && npm run build`
Expected: typecheck 0 错误；vitest 全绿；build 成功

- [ ] **Step 6: 提交**

```bash
git add src/composables/useGlobalShortcuts.ts src/composables/useGlobalShortcuts.test.ts src/components/ssh/MruSwitcher.vue src/views/ssh/SshView.vue
git commit -m "feat(ui): Ctrl+Tab 升级为 MRU 切换器（浮层 + 循环移动 + Esc 取消）"
```

---

### Task 6: 标签溢出菜单

**Files:**
- Create: `src/components/ssh/TabOverflowMenu.vue`
- Modify: `src/views/ssh/SshView.vue`（tabs-scroll 挂 ResizeObserver + 渲染入口）

**Interfaces:**
- Consumes: `store.sessions`/`store.activeSessionId`、Task 4 的 `tabStatusKind`、SshView 的 `activateTerminal`。
- Produces: `<TabOverflowMenu :anchor="HTMLElement" @activate="(id: string) => void" @close="void" />`——由 SshView 控制开关与锚点。

- [ ] **Step 1: 写守卫测试**

新建 `src/views/ssh/tab-overflow.guards.test.ts`：

```ts
/** 溢出菜单的存在性与接线守卫（交互行为靠 rendererOnly 视觉验收） */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const sshView = readFileSync(
  fileURLToPath(new URL('./SshView.vue', import.meta.url)),
  'utf8',
)
const menu = readFileSync(
  fileURLToPath(new URL('../components/ssh/TabOverflowMenu.vue', import.meta.url)),
  'utf8',
)

describe('标签溢出菜单', () => {
  it('SshView 以 ResizeObserver 判定溢出并挂载菜单', () => {
    expect(sshView).toContain('ResizeObserver')
    expect(sshView).toContain('TabOverflowMenu')
  })

  it('菜单列出全部会话并带状态点判据', () => {
    expect(menu).toContain('store.sessions')
    expect(menu).toContain('tabStatusKind')
  })

  it('菜单 Teleport 到 body（逃出 .term-tabs 的 overflow:hidden）', () => {
    expect(menu).toContain('Teleport')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/views/ssh/tab-overflow.guards.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现**

1. 新建 `src/components/ssh/TabOverflowMenu.vue`（定位/收起模式照抄 NewTerminalMenu：定点坐标 + 滚动即收起 + 文档点击关闭）：

```vue
<script setup lang="ts">
/** 标签溢出菜单：tabs-scroll 横向装不下时，标签条右侧「▾ N」的全部会话列表。 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useSSHStore } from '@/stores/ssh'
import { tabStatusKind } from '@/utils/tabStatus'
import IconMonitor from '@/components/icons/IconMonitor.vue'
import IconTerminal from '@/components/icons/IconTerminal.vue'

const props = defineProps<{ anchor: HTMLElement | null }>()
const emit = defineEmits<{ activate: [id: string]; close: [] }>()

const store = useSSHStore()

// 定点坐标：菜单必须逃出 .term-tabs 的 overflow:hidden（与 NewTerminalMenu 同一约束）
const r = props.anchor?.getBoundingClientRect()
const pos = { top: (r?.bottom ?? 0) + 4, left: Math.max(8, (r?.right ?? 280) - 280) }

function onDocClick(e: MouseEvent) {
  if (!(e.target as HTMLElement).closest('.tof-menu, .tof-btn')) emit('close')
}
function onScroll() { emit('close') }

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('scroll', onScroll, true)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('scroll', onScroll, true)
})

function pick(id: string) {
  emit('activate', id)
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div class="tof-menu" :style="{ top: pos.top + 'px', left: pos.left + 'px' }">
      <button
        v-for="s in store.sessions"
        :key="s.id"
        type="button"
        class="tof-item"
        :class="{ on: s.id === store.activeSessionId }"
        @click="pick(s.id)"
      >
        <span
          v-if="tabStatusKind(s, store.activeSessionId)"
          class="tof-dot"
          :class="tabStatusKind(s, store.activeSessionId)"
          aria-hidden="true"
        />
        <IconMonitor v-if="s.type === 'remote'" :width="12" :height="12" :stroke-width="2" />
        <IconTerminal v-else :width="12" :height="12" :stroke-width="2" />
        <span class="lbl">{{ s.name }}</span>
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.tof-menu {
  position: fixed;
  width: 280px;
  max-height: 340px;
  overflow-y: auto;
  padding: 4px;
  background: var(--bg-elevated-solid);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 900;
}

.tof-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 5px 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  font-family: var(--font-sans);
  text-align: left;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.tof-item:hover { background: var(--bg-hover); color: var(--text-primary); }
.tof-item.on { color: var(--text-primary); }

.lbl {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tof-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tof-dot.activity { background: var(--accent-primary); }
.tof-dot.gave-up { background: var(--accent-red); }
.tof-dot.reconnecting { background: var(--status-warn-text); }
</style>
```

2. `src/views/ssh/SshView.vue`：
   - import 加 `import TabOverflowMenu from '@/components/ssh/TabOverflowMenu.vue'`。
   - script 加溢出判定：

```ts
// 标签溢出：tabs-scroll 装不下时在标签条右侧给「▾ N」入口
const tabsScrollRef = ref<HTMLElement | null>(null)
const overflowBtnRef = ref<HTMLElement | null>(null)
const tabsOverflow = ref(false)
const overflowMenuOpen = ref(false)
let tabsObserver: ResizeObserver | null = null

function measureTabsOverflow(): void {
  const el = tabsScrollRef.value
  tabsOverflow.value = !!el && el.scrollWidth > el.clientWidth + 1
}

onMounted(() => {
  const el = tabsScrollRef.value
  if (el) {
    tabsObserver = new ResizeObserver(measureTabsOverflow)
    tabsObserver.observe(el)
  }
})

onUnmounted(() => {
  tabsObserver?.disconnect()
  tabsObserver = null
})

watch(() => store.sessions.length, () => nextTick(measureTabsOverflow))
```

   - import 行补 `onUnmounted`（`from 'vue'`）。
   - 模板：`.tabs-scroll` 开标签加 `ref="tabsScrollRef"`；在 `.tabs-scroll` 的**闭合标签之后**（`.term-tabs` 内，与 tabs-scroll 同级——按钮要钉在标签条右缘，不跟着内容滚动）加：

```html
          <button
            v-if="tabsOverflow"
            ref="overflowBtnRef"
            type="button"
            class="tof-btn"
            :title="`全部终端（${store.sessions.length}）`"
            :aria-label="`全部终端（${store.sessions.length}）`"
            @click.stop="overflowMenuOpen = !overflowMenuOpen"
          >▾</button>
          <TabOverflowMenu
            v-if="overflowMenuOpen && tabsOverflow"
            :anchor="overflowBtnRef"
            @activate="activateTerminal"
            @close="overflowMenuOpen = false"
          />
```

   - `<style>` 加（按钮视觉对齐 NewTerminalMenu 的 `.ntm-btn`）：

```css
.tof-btn {
  height: calc(var(--tabbar-h) - 4px);
  width: calc(var(--tabbar-h) - 4px);
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  flex-shrink: 0;
  transition: color var(--transition-fast), background var(--transition-fast);
}
.tof-btn:hover { color: var(--text-secondary); background: var(--bg-hover); }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/views/ssh/tab-overflow.guards.test.ts`
Expected: PASS

- [ ] **Step 5: 模板验证**

Run: `npm run typecheck && npm run build`
Expected: 0 错误

- [ ] **Step 6: 提交**

```bash
git add src/components/ssh/TabOverflowMenu.vue src/views/ssh/SshView.vue src/views/ssh/tab-overflow.guards.test.ts
git commit -m "feat(ui): 标签溢出菜单——tabs 装不下时右侧「▾」列出全部会话"
```

---

### Task 7: 走查 minor 两枚 + DESIGN.md 增补 + 全量验证

**Files:**
- Modify: `src/views/ssh/SshView.vue:830,842`（右键菜单字面量）
- Modify: `src/components/ssh/NewTerminalMenu.vue:91-98`（aria-label）
- Modify: `DESIGN.md`（组件约定补一行）

- [ ] **Step 1: 修字面量与 aria-label**

`src/views/ssh/SshView.vue`：
- `.tab-ctx-menu` 的 `border-radius: 6px;` → `border-radius: var(--radius-md);`
- `.tab-ctx-item` 的 `transition: background 0.1s;` → `transition: background var(--transition-fast);`

`src/components/ssh/NewTerminalMenu.vue`：`.ntm-btn` 开标签补 `aria-label="新建终端"`。

`DESIGN.md`「组件约定」的图标条目末尾补一行：

```markdown
- 标签状态点：6px 圆点，活动 `--accent-primary` / 重连中 `--status-warn-text` 脉冲 / 断连放弃 `--accent-red`；判据单一来源 `src/utils/tabStatus.ts`。
```

- [ ] **Step 2: 全量验证**

Run: `npm run typecheck && npx vitest run && npm run build`
Expected: 全绿

- [ ] **Step 3: 视觉验收（rendererOnly + 桩）**

```bash
npx electron-vite dev --rendererOnly
```

浏览器打开终端页，Proxy 桩造 3+ 会话，逐项核对：
- [ ] 非活跃会话注入数据 → 标签出现活动点；切过去 → 点消失
- [ ] 缩窄窗口到标签溢出 → 「▾」出现，点开列表带状态点，点击跳转
- [ ] Ctrl+Tab 按住 → 浮层贴标签条下缘，连点移动，松开落定，Esc 取消
- [ ] 设置里切换 8 个主题 → 终端 ANSI 色温跟随；切换 accent → cursor/搜索高亮跟随

- [ ] **Step 4: 提交**

```bash
git add src/views/ssh/SshView.vue src/components/ssh/NewTerminalMenu.vue DESIGN.md
git commit -m "fix(ui): 右键菜单字面量归令牌、＋按钮补 aria-label，DESIGN.md 补状态点约定"
```

---

## Self-Review 记录

- Spec 覆盖：①=Task 3+4，②=Task 1，③=Task 1+2，④=Task 5+6，minor=Task 7，「长命令完成」明确排除。✓
- 类型一致性：`tabStatusKind` 在 Task 4 定义、Task 5/6 消费（签名一致）；`mruIds`/`markActivity` Task 3 产出、Task 4/5 消费。✓
- 已知偏差（对 spec 的有意调整）：RemoteTerminal 的系统行写入保留在本地（状态归 store，文字归组件，避免把 appendReconnectLine 的行缓冲依赖搬上楼）。
