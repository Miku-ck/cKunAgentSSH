/**
 * HostsStatus 的两端形状守卫。
 *
 * 主进程 `electron/gh-hosts.ts` 的 interface 与渲染层 `src/env.d.ts` 里手写的
 * `ghaccelHostsStatus` 返回形状是同一份契约的两份抄本 —— 抄本之间 typecheck 抓不到
 * 漂移：主进程多返一个字段，渲染层的类型里就永远看不见它，UI 读 `hosts.stale` 会拿到
 * undefined 而没有任何报错（IPC 返回的运行时值明明带着）。本轮加 generatedAt/staleDays
 * 时两边都要动，正是最容易漏一边的那种改动，所以把它钉成断言。
 *
 * 手法与 electron/app-user-model-id.test.ts 同族：读源码文本比对，不启动 Electron。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

/**
 * 从一段 TS 源码里抠出「某个块」的直接字段名。
 * startRe 定位块的开头，字段取"比开始行多一层缩进"的那些，遇到收口的缩进即停。
 */
function topLevelFieldNames(src: string, startRe: RegExp): string[] {
  const lines = src.split(/\r?\n/)
  const start = lines.findIndex((l) => startRe.test(l))
  expect(start, `找不到匹配 ${startRe} 的起始行`).toBeGreaterThanOrEqual(0)
  const baseIndent = /^\s*/.exec(lines[start])![0].length
  const names: string[] = []
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i]
    if (/^\s*[})\]];?\s*$/.test(line) && /^\s*/.exec(line)![0].length <= baseIndent + 1) break
    if (line.trim().startsWith('*') || line.trim().startsWith('/*') || line.trim().startsWith('//')) continue
    const indent = /^\s*/.exec(line)![0].length
    if (indent <= baseIndent) break
    const f = /^\s*([A-Za-z_][\w]*)\??\s*:/.exec(line)
    if (f && indent === baseIndent + 2) names.push(f[1])
  }
  return names
}

const hostsSrc = readFileSync(new URL('./gh-hosts.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')
const accelSrc = readFileSync(new URL('./github-accel.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')
const envSrc = readFileSync(new URL('../src/env.d.ts', import.meta.url), 'utf-8').replace(/\r\n/g, '\n')

describe('HostsStatus 主进程与渲染层抄本同源', () => {
  it('两端字段集合逐字相等', () => {
    const impl = topLevelFieldNames(hostsSrc, /^export interface HostsStatus \{/)
    const decl = topLevelFieldNames(envSrc, /ghaccelHostsStatus: \(\) => Promise<\{/)

    expect(impl.length, '主进程 interface 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect(decl.length, 'env.d.ts 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect(
      decl,
      `渲染层抄本与主进程不一致 —— 主进程: [${impl.sort()}] / 渲染层: [${decl.sort()}]`,
    ).toEqual([...impl].sort())
  })

  it('提取器不是空跑：少一个字段就能被抓出来', () => {
    const sample = [
      'export interface HostsStatus {',
      '  applied: boolean',
      '  /** 注释行不该被当成字段 */',
      '  staleDays: number | null',
      '  entries: { domain: string; ip: string }[]',
      '}',
    ].join('\n')
    expect(topLevelFieldNames(sample, /^export interface HostsStatus \{/)).toEqual([
      'applied', 'staleDays', 'entries',
    ])
    const dropped = sample.replace('  staleDays: number | null\n', '')
    expect(topLevelFieldNames(dropped, /^export interface HostsStatus \{/)).not.toContain('staleDays')
  })
})

describe('巡检/自检契约主进程与渲染层抄本同源', () => {
  it('HealthStatus 字段集合逐字相等', () => {
    const impl = topLevelFieldNames(accelSrc, /^export interface HealthStatus \{/)
    const decl = topLevelFieldNames(envSrc, /^interface GhaccelHealthStatus \{/)

    expect(impl.length, '主进程 HealthStatus 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect(decl.length, 'env.d.ts 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect(
      [...decl].sort(),
      `渲染层抄本与主进程不一致 —— 主进程: [${[...impl].sort()}] / 渲染层: [${[...decl].sort()}]`,
    ).toEqual([...impl].sort())
  })

  it('SelfCheckRow 字段集合逐字相等', () => {
    const impl = topLevelFieldNames(accelSrc, /^export interface SelfCheckRow \{/)
    const decl = topLevelFieldNames(envSrc, /^interface GhaccelSelfCheckRow \{/)

    expect(impl.length, '主进程 SelfCheckRow 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect(decl.length, 'env.d.ts 一个字段都没提取到，守卫等于空跑').toBeGreaterThan(0)
    expect([...decl].sort()).toEqual([...impl].sort())
  })
})
