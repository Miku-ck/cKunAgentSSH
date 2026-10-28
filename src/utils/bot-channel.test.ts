import { describe, expect, it } from 'vitest'
import { channelLabel, personaName } from './bot-channel'

describe('QQ 会话渠道标签', () => {
  it('group / c2c 前缀分别显示群聊与私聊', () => {
    expect(channelLabel('group:group-openid')).toBe('群聊')
    expect(channelLabel('c2c:user-openid')).toBe('私聊')
  })

  it('缺失或未知来源不冒充私聊', () => {
    expect(channelLabel(null)).toBe('QQ')
    expect(channelLabel('')).toBe('QQ')
    expect(channelLabel('webhook:abc')).toBe('QQ')
  })
})

describe('会话归属人格名', () => {
  it('取服务端下发的 persona.name，人格删除后仍能显示', () => {
    expect(personaName({ name: '小暖' })).toBe('小暖')
  })

  it('缺失或空名时返回空串，由调用方兜底', () => {
    expect(personaName(null)).toBe('')
    expect(personaName(undefined)).toBe('')
    expect(personaName({})).toBe('')
    expect(personaName({ name: '   ' })).toBe('')
  })
})
