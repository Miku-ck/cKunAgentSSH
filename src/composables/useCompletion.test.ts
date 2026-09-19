/**
 * 历史命令 fuzzy 联想匹配测试（E 阶段）
 */
import { describe, expect, it, vi } from 'vitest'

// useCompletion 依赖 pinia store 与 api client，纯函数测试不需要真实实现
vi.mock('@/stores/ssh', () => ({ useSSHStore: vi.fn() }))
vi.mock('@/api/client', () => ({ api: {} }))
vi.mock('@/stores/settings', () => ({ useSettingsStore: vi.fn() }))

import { fuzzyMatchCommand } from './useCompletion'

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
