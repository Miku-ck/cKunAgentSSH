import { describe, expect, it } from 'vitest'
import {
  decorateMessages,
  dayDividerLabel,
  peerLabel,
  peerSeed,
  peerTag,
} from './bot-chat-decor'

describe('会话对方标识 peerSeed / peerLabel', () => {
  it('从 group/c2c external_id 抽出 openid 作为头像种子', () => {
    expect(peerSeed('group:G-openid-AAA:ctx1')).toBe('G-openid-AAA')
    expect(peerSeed('c2c:U-openid-BBB:ctx1')).toBe('U-openid-BBB')
  })

  it('群/私聊各带类型前缀与 openid 尾号代号', () => {
    expect(peerLabel('group:Gxxxopenida3f1:ctx')).toBe('群聊 · A3F1')
    expect(peerLabel('c2c:Uxxxopenid7b22:ctx')).toBe('私聊 · 7B22')
  })

  it('本地会话或脏值不冒充，回落空串与 QQ', () => {
    expect(peerSeed(null)).toBe('')
    expect(peerSeed('')).toBe('')
    expect(peerSeed('weird-no-colon')).toBe('')
    expect(peerLabel(null)).toBe('QQ')
    expect(peerLabel('weird-no-colon')).toBe('QQ')
  })

  it('列表行只给群聊压一个「群」标：私聊是默认，标出来反而添噪', () => {
    expect(peerTag('group:Gxxxopenida3f1:ctx')).toBe('群')
    expect(peerTag('c2c:Uxxxopenid7b22:ctx')).toBe('')
    expect(peerTag(null)).toBe('')
    expect(peerTag('weird-no-colon')).toBe('')
  })
})

describe('日期分隔 dayDividerLabel', () => {
  const now = new Date('2026-10-28T10:00:00')
  it('当天显示今天', () => {
    expect(dayDividerLabel('2026-10-28 12:00:00', now)).toBe('今天')
  })
  it('前一天显示昨天', () => {
    expect(dayDividerLabel('2026-10-27 12:00:00', now)).toBe('昨天')
  })
  it('本年更早显示月日', () => {
    expect(dayDividerLabel('2026-09-05 12:00:00', now)).toBe('9月5日')
  })
  it('跨年显示完整年月日', () => {
    expect(dayDividerLabel('2025-12-31 12:00:00', now)).toBe('2025年12月31日')
  })
  it('空/非法返回空串，由调用方跳过分隔线', () => {
    expect(dayDividerLabel('', now)).toBe('')
    expect(dayDividerLabel(null, now)).toBe('')
  })
})

describe('连续消息合并 decorateMessages', () => {
  const mk = (role: string, created_at: string) => ({ role, created_at })
  it('同角色相邻标记为续接，隐藏重复头像、收紧间距', () => {
    const out = decorateMessages([
      mk('user', '2026-10-28 10:00:00'),
      mk('user', '2026-10-28 10:00:30'),
    ])
    expect(out[0].cont).toBe(false)
    expect(out[1].cont).toBe(true)
  })
  it('角色切换处不是续接', () => {
    const out = decorateMessages([
      mk('user', '2026-10-28 10:00:00'),
      mk('assistant', '2026-10-28 10:00:30'),
    ])
    expect(out[1].cont).toBe(false)
  })
  it('跨天在首条挂日期分隔线', () => {
    const out = decorateMessages(
      [
        mk('user', '2026-10-27 12:00:00'),
        mk('user', '2026-10-28 12:00:00'),
      ],
      new Date('2026-10-28 12:00:00'),
    )
    expect(out[0].divider).toBe('昨天')
    expect(out[1].divider).toBe('今天')
  })
  it('system 消息永不作为续接首锚', () => {
    const out = decorateMessages([
      mk('system', '2026-10-28 10:00:00'),
      mk('system', '2026-10-28 10:00:30'),
    ])
    expect(out[1].cont).toBe(false)
  })
})
