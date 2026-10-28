import { describe, expect, it } from 'vitest'
import { channelLabel, personaNameFromSnapshot } from './bot-channel'

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

describe('人格快照名', () => {
  it('从 JSON 快照里取名字，人格删除后仍能显示', () => {
    expect(personaNameFromSnapshot('{"name":"小暖","personality":"温和"}')).toBe('小暖')
  })

  it('快照缺失或损坏时返回空串', () => {
    expect(personaNameFromSnapshot('')).toBe('')
    expect(personaNameFromSnapshot(null)).toBe('')
    expect(personaNameFromSnapshot('not json')).toBe('')
    expect(personaNameFromSnapshot('{"id":"x"}')).toBe('')
  })
})
