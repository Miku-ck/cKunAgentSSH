import { describe, expect, it } from 'vitest'
import {
  decorateMessages,
  dayDividerLabel,
  identiconSvg,
  peerLabel,
  peerSeed,
} from './bot-chat-decor'

describe('生成式匿名头像 identiconSvg', () => {
  it('同一 seed 恒定产出同一 SVG（可辨识且稳定）', () => {
    expect(identiconSvg('group-openid-A3F1')).toBe(identiconSvg('group-openid-A3F1'))
  })

  it('不同 seed 产出不同 SVG（区分不同联系人）', () => {
    expect(identiconSvg('user-7B22')).not.toBe(identiconSvg('user-1E99'))
  })

  it('缺失 seed 也返回合法 SVG，不抛错不渲染破图', () => {
    const svg = identiconSvg('')
    expect(svg.startsWith('<svg')).toBe(true)
    expect(svg.endsWith('</svg>')).toBe(true)
  })

  it('产出的是纯 SVG 标记，不含外部真实头像数据', () => {
    const svg = identiconSvg('peer-x')
    expect(svg).not.toContain('http')
    expect(svg).toContain('</svg>')
  })
})

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
