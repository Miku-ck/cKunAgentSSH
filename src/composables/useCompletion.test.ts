/**
 * 历史命令 fuzzy 联想匹配测试（E 阶段）
 */
import { describe, expect, it, vi } from 'vitest'

// useCompletion 依赖 pinia store 与 api client，纯函数测试不需要真实实现
vi.mock('@/stores/ssh', () => ({ useSSHStore: vi.fn() }))
vi.mock('@/api/client', () => ({ api: {} }))
vi.mock('@/stores/settings', () => ({ useSettingsStore: vi.fn() }))

import { fuzzyMatchCommand, readCurrentLine, stripPrompt } from './useCompletion'

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

/**
 * S6-15：删掉那条永不命中的 OSC 6973 精确通道后，边界判定全靠这里的启发式回退 ——
 * 于是 docstring 里那组「验证用例」第一次有了执行它的地方。用例是原样从 docstring
 * 搬来的：注释宣传的行为以前没人测，删代码时也就没人能证明行为没变。
 */
describe('stripPrompt（S6-15 删死通道后的唯一判据）', () => {
  const cases: Array<[string, string, number]> = [
    ['user@host:~/dir$ git ', 'git ', 17],
    ['root@host:/# ls ', 'ls ', 13],
    ['> git', 'git', 2],
    ['PS C:\\> git', 'git', 8],
    ['C:\\> git', 'git', 5],
    ['用户@主机:~$ git ', 'git ', 13],
    ['\x1b[32muser:~$\x1b[0m git', 'git', 8],
    ['user@host:~$', '', 12],
  ]

  for (const [line, input, startCol] of cases) {
    it(`${JSON.stringify(line)} -> input ${JSON.stringify(input)}, startCol ${startCol}`, () => {
      const r = stripPrompt(line)
      expect(r.input).toBe(input)
      expect(r.startCol).toBe(startCol)
    })
  }

  it('空 prompt 与无分隔符的行也不抛（回退支各管一段）', () => {
    expect(stripPrompt('')).toEqual({ input: '', startCol: 0 })
    expect(stripPrompt('   git status').input).toBe('git status')
  })
})
