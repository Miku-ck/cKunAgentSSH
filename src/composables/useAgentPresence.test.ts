/**
 * 首页状态点与顶栏 AI 徽章的唯一判定（spec 2026-10-20-home-agent-workbench §5.2）。
 *
 * 两处若各自写一遍 if/else，优先级迟早漂移：首页亮「待审批」而顶栏亮「运行中」，
 * 用户按颜色找过去会发现两处指点的是同一场 run 的不同说法。纯函数钉死顺序，
 * 两个消费方只读它的结果。
 */
import { describe, expect, it } from 'vitest'
import { resolveAgentPresence } from './useAgentPresence'

const input = (over: Partial<{ pendingCount: number; running: boolean; unread: boolean }> = {}) => ({
  pendingCount: 0,
  running: false,
  unread: false,
  ...over,
})

describe('resolveAgentPresence', () => {
  it('空闲：无待批、未运行、无未读', () => {
    expect(resolveAgentPresence(input())).toBe('idle')
  })

  it('未读：面板关着时跑完一轮', () => {
    expect(resolveAgentPresence(input({ unread: true }))).toBe('unread')
  })

  it('运行中压过未读：新一轮已在跑，旧回复的未读点不再当主角', () => {
    expect(resolveAgentPresence(input({ running: true, unread: true }))).toBe('running')
  })

  it('待审批压过运行中：审批是运行中的子态，也是最需要用户动作的那一格', () => {
    expect(resolveAgentPresence(input({ pendingCount: 2, running: true, unread: true }))).toBe('pending')
  })

  it('待审批即使 run 已收尾（残留卡）也优先', () => {
    expect(resolveAgentPresence(input({ pendingCount: 1, unread: true }))).toBe('pending')
  })
})
