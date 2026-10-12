import { describe, expect, it } from 'vitest'
import { grantScopesForRisk, onceGrantBlocked } from './approval-scopes'

describe('审批卡可见的授权档位', () => {
  it('moderate 卡给 once/session/always 三档', () => {
    expect(grantScopesForRisk('moderate')).toEqual(['once', 'session', 'always'])
  })

  it('dangerous 卡没有 session 按钮：后端契约是 dangerous 每次授权', () => {
    // PermissionGate.check_permission 对 risk==='dangerous' 刻意忽略 session 缓存
    // （B-6）。给它摆这个按钮 = 点了当下生效、下次照弹的假承诺。
    expect(grantScopesForRisk('dangerous')).toEqual(['once', 'always'])
  })

  it('脏风险值按 dangerous 处理（fail-closed 方向）', () => {
    for (const junk of ['', 'HIGH', 'DANGEROUS', undefined as unknown as string]) {
      expect(grantScopesForRisk(junk)).toEqual(['once', 'always'])
    }
  })
})

describe('once 授权入口的 run 状态门', () => {
  // P2 回归（2026-10-11）：once 按 run 键控，run 终态后点击只会落进死键
  // （后端已 409），前端必须提前禁用 —— 禁止点出假「已授权」。
  it('run 结束后 once 入口禁用；运行中不禁', () => {
    expect(onceGrantBlocked(true)).toBe(false)
    expect(onceGrantBlocked(false)).toBe(true)
  })
})
