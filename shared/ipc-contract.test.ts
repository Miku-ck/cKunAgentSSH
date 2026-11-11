/**
 * IPC 契约守恒守卫。
 *
 * 147 个 invoke 频道与 18 个事件频道原先在 preload.ts / main.ts / fs-handler.ts /
 * src/env.d.ts 里各写一份，编译器抓不到漂移。这里把四方按「集合逐字相等」钉死：
 *   主进程 handleTrusted 注册集合 ≡ INVOKE_CHANNELS 值集合
 *   preload 成员键集合 ≡ INVOKE_CHANNELS 键集合 ≡ EVENT_CHANNELS 键集合
 *   env.d.ts 的 ElectronAPI 成员集合 ≡ 上述键集合 + getPathForFile
 *
 * 测试只读源码文本，不启动 Electron；频道名必须与改造前逐字一致。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { INVOKE_CHANNELS, EVENT_CHANNELS } from './ipc-contract'

const read = (rel: string): string =>
  readFileSync(new URL(rel, import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

const main = read('../electron/main.ts')
const fsHandler = read('../electron/handlers/fs-handler.ts')
const preload = read('../electron/preload.ts')
const envDts = read('../src/env.d.ts')

const registered = [...main.matchAll(/handleTrusted\('([^']+)'/g), ...fsHandler.matchAll(/handleTrusted\('([^']+)'/g)]
  .map((m) => m[1])
const invokeKeysInPreload = [...preload.matchAll(/INVOKE_CHANNELS\.(\w+)/g)].map((m) => m[1])
const eventKeysInPreload = [...preload.matchAll(/EVENT_CHANNELS\.(\w+)/g)].map((m) => m[1])

/** ElectronAPI 块内「行首两空格 + 标识符 + 冒号」即一个成员 */
function electronApiMembers(src: string): string[] {
  const lines = src.split('\n')
  const start = lines.findIndex((l) => l.startsWith('interface ElectronAPI {'))
  expect(start, 'env.d.ts 找不到 ElectronAPI 声明，守卫等于空跑').toBeGreaterThanOrEqual(0)
  const names: string[] = []
  for (let i = start + 1; i < lines.length; i++) {
    if (/^\}/.test(lines[i])) break
    const m = /^  ([A-Za-z_][\w]*)\??\s*:/.exec(lines[i])
    if (m) names.push(m[1])
  }
  return names
}

const sorted = (xs: string[]): string[] => [...xs].sort()

/** 改造前 preload.ts 的 18 个事件频道名快照：只作为验收基线，生产代码不得再抄一份 */
const BASELINE_EVENT_CHANNELS = [
  'update:state', 'backend:status', 'pty:data', 'pty:exit',
  'ssh:data', 'ssh:close', 'ssh:reconnect', 'ssh:host-key-new',
  'ssh:host-key-mismatch', 'ssh:host-key-unpinned', 'ghaccel:download',
  'ghaccel:health', 'tunnel:status', 'gateway:status',
  'gateway:host-key-new', 'sftp:progress', 'sftp:stalled', 'sftp:closed',
]

/**
 * 主进程 → 渲染层的推送点。
 *
 * 本次改造只把 preload 侧收敛到常量表，主进程仍保留 18 个事件频道名的字面量
 * （147 个 handleTrusted 注册同样保留字面量），这是「不批量改名、不动核心链路」
 * 档位下的有意取舍。为免改名漏改一侧而编译器不报错，这里用推送点集合与常量表
 * 做双向比对：少推一个、多推一个未知频道，测试都会红。
 *
 * 只认三种推送形态：`safeSend(`、`this.push(`、`.send(`。行首 `//` 与 `*` 注释行
 * 跳过——注释里常出现频道名（如「与 sftp:progress 同一口径」），不能算推送点。
 */
const PUSH_MARK = /(?:safeSend\(|\bpush\(|\.send\()/
const CHANNEL_LITERAL = /'([a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*)'/g

function mainProcessPushSites(): { channel: string; site: string }[] {
  const sites: { channel: string; site: string }[] = []
  const walk = (dir: URL): void => {
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
      if (ent.isDirectory()) {
        if (ent.name !== 'node_modules' && ent.name !== 'out') {
          walk(new URL(ent.name + '/', dir))
        }
        continue
      }
      if (!ent.name.endsWith('.ts') || ent.name.endsWith('.test.ts')) continue
      const text = readFileSync(new URL(ent.name, dir), 'utf-8').replace(/\r\n/g, '\n')
      text.split('\n').forEach((line, i) => {
        const trimmed = line.trimStart()
        if (trimmed.startsWith('//') || trimmed.startsWith('*')) return
        if (!PUSH_MARK.test(line)) return
        for (const m of line.matchAll(CHANNEL_LITERAL)) {
          sites.push({ channel: m[1], site: `${ent.name}:${i + 1}` })
        }
      })
    }
  }
  walk(new URL('../electron/', import.meta.url))
  return sites
}

const pushSites = mainProcessPushSites()

describe('IPC invoke 契约守恒（147 频道）', () => {
  it('常量表 147 项且频道名唯一', () => {
    expect(Object.keys(INVOKE_CHANNELS)).toHaveLength(147)
    expect(new Set(Object.values(INVOKE_CHANNELS)).size).toBe(147)
    expect(registered, '主进程注册集合提取为空，守卫等于空跑').toHaveLength(147)
  })

  it('主进程 handleTrusted 注册集合 ≡ 常量表值集合', () => {
    expect(sorted(registered)).toEqual(sorted(Object.values(INVOKE_CHANNELS)))
  })

  it('preload 的 INVOKE_CHANNELS 引用集合 ≡ 常量表键集合', () => {
    expect(sorted(invokeKeysInPreload)).toEqual(sorted(Object.keys(INVOKE_CHANNELS)))
    expect(new Set(invokeKeysInPreload).size).toBe(147)
  })

  it('preload 里不再有硬编码的 invoke 频道字符串', () => {
    expect(preload).not.toMatch(/ipcRenderer\.invoke\('[^']+'/)
  })
})

describe('IPC 事件契约守恒（18 频道）', () => {
  it('常量表 18 项且与改造前基线逐字相等', () => {
    expect(Object.keys(EVENT_CHANNELS)).toHaveLength(18)
    expect(sorted(Object.values(EVENT_CHANNELS))).toEqual(sorted(BASELINE_EVENT_CHANNELS))
    expect(new Set(Object.values(EVENT_CHANNELS)).size).toBe(18)
  })

  it('preload 的 EVENT_CHANNELS 引用集合 ≡ 常量表键集合', () => {
    expect(sorted(eventKeysInPreload)).toEqual(sorted(Object.keys(EVENT_CHANNELS)))
  })

  it('preload 里不再有硬编码的事件频道字符串', () => {
    expect(preload).not.toMatch(/ipcRenderer\.on\('[^']+'/)
    expect(preload).not.toMatch(/ipcRenderer\.removeListener\('[^']+'/)
  })
})

describe('IPC 事件推送端守恒（主进程 → 渲染层）', () => {
  const known: string[] = Object.values(EVENT_CHANNELS)
  const pushed = new Set(pushSites.map((s) => s.channel))

  it('推送点提取非空（守卫防空跑）', () => {
    expect(pushSites.length, '主进程推送点提取为空，守卫等于空跑').toBeGreaterThan(18)
    expect(pushed.size).toBe(18)
  })

  it('主进程不推送常量表之外的频道', () => {
    const unknown = [...pushed].filter((c) => !known.includes(c))
    expect(unknown, '主进程推送了常量表之外的频道：改名时漏改了一侧').toEqual([])
  })

  it('每个事件频道在主进程都至少有一处推送点', () => {
    const missing = known.filter((c) => !pushed.has(c))
    expect(missing, '常量表声明了该事件，但主进程从不推送').toEqual([])
  })

  it('推送点覆盖 18 个频道（明细可打印，便于改名时定位）', () => {
    const detail = [...pushed].sort().join(', ')
    expect(detail).toBe(sorted(BASELINE_EVENT_CHANNELS).join(', '))
  })
})

describe('ElectronAPI 成员守恒（166 成员）', () => {
  it('env.d.ts 成员集合 ≡ 147 invoke + 18 监听 + getPathForFile', () => {
    const members = electronApiMembers(envDts)
    expect(members, 'env.d.ts 成员提取为空，守卫等于空跑').toHaveLength(166)
    const expected = [
      ...Object.keys(INVOKE_CHANNELS),
      ...Object.keys(EVENT_CHANNELS),
      'getPathForFile',
    ]
    expect(sorted(members)).toEqual(sorted(expected))
    expect(new Set(members).size).toBe(166)
  })

  it('preload 暴露对象仍为字面量且带 satisfies 编译期守恒', () => {
    expect(preload).toContain("contextBridge.exposeInMainWorld('electronAPI', {")
    expect(preload).toContain('} satisfies Record<ApiKey, (...args: never[]) => unknown>)')
  })
})
