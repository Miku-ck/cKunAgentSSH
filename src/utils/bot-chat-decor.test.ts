import { describe, expect, it } from 'vitest'
import {
  avatarPlaceholderStyle,
  decorateMessages,
  dayDividerLabel,
  peerLabel,
  peerSeed,
  placeholderInitial,
} from './bot-chat-decor'

describe('头像淡色首字占位（Phase 4 §4.1，取代马赛克）', () => {
  it('同一 seed 恒定同色，不同 seed 色相不同', () => {
    expect(avatarPlaceholderStyle('晚风')).toEqual(avatarPlaceholderStyle('晚风'))
    expect(avatarPlaceholderStyle('晚风')).not.toEqual(avatarPlaceholderStyle('小刻'))
  })

  it('低饱和高明度淡底 + 同色系深字：明暗主题都可读', () => {
    const { background, color } = avatarPlaceholderStyle('晚风')
    expect(background).toMatch(/^hsl\(\d+, 30%, 88%\)$/)
    expect(color).toMatch(/^hsl\(\d+, 40%, 30%\)$/)
    // 深字与底同色系（色相一致），只是明度拉开
    expect(background.replace(/^hsl\((\d+),.*$/, '$1')).toBe(color.replace(/^hsl\((\d+),.*$/, '$1'))
  })

  it('空 seed 回退固定种子，不抛错', () => {
    expect(avatarPlaceholderStyle('')).toEqual(avatarPlaceholderStyle('  '))
    expect(avatarPlaceholderStyle('').background).toBeTruthy()
  })

  it('首字优先取名字，名字为空取 seed，全空给空串', () => {
    expect(placeholderInitial('晚风')).toBe('晚')
    expect(placeholderInitial('', 'a3f1')).toBe('a')
    expect(placeholderInitial('', '')).toBe('')
    expect(placeholderInitial(null, null)).toBe('')
  })

  it('emoji 名不会被拆成半个代理对', () => {
    expect(placeholderInitial('🌙晚风')).toBe('🌙')
  })

  it('QQ 匿名对端用代号首字：群/私 + 每 seed 异色区分', () => {
    expect(placeholderInitial('群聊 · A3F1')).toBe('群')
    expect(placeholderInitial('私聊 · 7B22')).toBe('私')
    expect(avatarPlaceholderStyle('G-openid-AAA')).not.toEqual(avatarPlaceholderStyle('U-openid-BBB'))
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
