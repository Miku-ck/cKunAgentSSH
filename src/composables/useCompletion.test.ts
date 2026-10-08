/**
 * 历史命令 fuzzy 联想匹配测试（E 阶段）
 */
import { describe, expect, it, vi } from 'vitest'

// useCompletion 依赖 pinia store 与 api client，纯函数测试不需要真实实现
vi.mock('@/stores/ssh', () => ({ useSSHStore: vi.fn() }))
vi.mock('@/api/client', () => ({ api: {} }))
vi.mock('@/stores/settings', () => ({ useSettingsStore: vi.fn() }))

import { fuzzyMatchCommand, readCurrentLine } from './useCompletion'

const CMDS = [
  'ls -lat',
  'ls',
  'git log --oneline -20',
  'git status',
  'docker logs -f web',
  'systemctl restart nginx',
  'kubectl get pods -n prod',
]

describe('fuzzyMatchCommand', () => {
  it('前缀命中优先于 fuzzy 命中', () => {
    // "ls" 前缀命中 ls / ls -lat；"ls -lat" 不是 "ls" 的前缀命中，ls 应排前
    const r = fuzzyMatchCommand(CMDS, 'ls')
    expect(r.length).toBeGreaterThanOrEqual(2)
    expect(r[0].command).toBe('ls')
  })

  it('fuzzy 子序列命中：lslat 命中 ls -lat', () => {
    const r = fuzzyMatchCommand(CMDS, 'lslat')
    expect(r.some(x => x.command === 'ls -lat')).toBe(true)
  })

  it('词首命中优先：git l 命中 git log 与 git status，log 排前', () => {
    const r = fuzzyMatchCommand(CMDS, 'git l')
    const iLog = r.findIndex(x => x.command === 'git log --oneline -20')
    const iStatus = r.findIndex(x => x.command === 'git status')
    expect(iLog).toBeGreaterThanOrEqual(0)
    expect(iStatus).toBe(-1) // status 不含连续 "l"，不命中 "git l"（l 在 git 后空格…g-i-t 空格 s-t-a-t-u-s 无 l）
  })

  it('无命中时返回空数组', () => {
    expect(fuzzyMatchCommand(CMDS, 'zzzz')).toEqual([])
  })

  it('单字符查询不触发（避免噪音）', () => {
    expect(fuzzyMatchCommand(CMDS, 'g')).toEqual([])
  })

  it('limit 截断', () => {
    const r = fuzzyMatchCommand(CMDS, 'g', 1)
    expect(r.length).toBeLessThanOrEqual(1)
  })

  it('大小写不敏感', () => {
    const r = fuzzyMatchCommand(CMDS, 'GIT LOG')
    expect(r.some(x => x.command === 'git log --oneline -20')).toBe(true)
  })

  it('连续字符加分：连续 "log" 得分高于分散命中', () => {
    const r = fuzzyMatchCommand(['abc log x', 'alogbxc'], 'log')
    expect(r[0].command).toBe('abc log x')
  })
})

/**
 * S6-01 回归：readCurrentLine 的视口相对 / 绝对行号换算。
 *
 * xterm 的 buffer.cursorY 是视口相对行号（0..rows-1），而 buffer.getLine(y) 吃
 * buffer 绝对行号 —— 原实现直接 getLine(cursorY)，终端滚过一段后读到的是游标
 * 上方 baseY 行之前的历史输出，Tab 补全据此把陈旧行的候选不确认地写进活动 shell。
 */
describe('readCurrentLine 行号换算', () => {
  const PROMPT_LINE = '[user@host ~]$ ls /var/l'
  const HISTORY_LINE = 'Sep 29 13:37 systemd[1]: Started Session 42 of user root.'

  type TerminalArg = Parameters<typeof readCurrentLine>[0]

  function fakeTerminal(baseY: number, cursorY: number, cursorX: number): TerminalArg {
    const lines = new Map<number, string>()
    lines.set(baseY + cursorY, PROMPT_LINE)
    if (baseY > 0) lines.set(cursorY, HISTORY_LINE)
    return {
      buffer: {
        active: {
          baseY,
          cursorY,
          cursorX,
          viewportY: baseY,
          getLine: (y: number) => {
            const text = lines.get(y)
            return text === undefined ? undefined : { translateToString: () => text }
          },
        },
      },
    } as unknown as TerminalArg
  }

  it('取 baseY + cursorY 的绝对行，而不是视口相对行', () => {
    const at = readCurrentLine(fakeTerminal(33, 9, 21))
    expect(at).not.toBeNull()
    expect(at?.line).toBe(PROMPT_LINE)
    expect(at?.cursorPos).toBe(21)
  })

  it('未滚动（baseY=0）时行为不变', () => {
    expect(readCurrentLine(fakeTerminal(0, 5, 3))?.line).toBe(PROMPT_LINE)
  })
})
