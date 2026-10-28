import { describe, expect, it } from 'vitest'
import {
  decorateMessages,
  dayDividerLabel,
  expandBubbles,
  peerLabel,
  peerSeed,
  peerTag,
  splitBubbles,
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

describe('分条 splitBubbles / expandBubbles', () => {
  it('空行就是另发一条：与服务端 split_reply_bubbles 同一规则', () => {
    expect(splitBubbles('在的\n\n晚上八点开一把')).toEqual(['在的', '晚上八点开一把'])
    expect(splitBubbles('就一句')).toEqual(['就一句'])
    expect(splitBubbles('  \n\n  ')).toEqual([])
    // 一条内部的换行是内容（列表、引文），不算分条
    expect(splitBubbles('清单：\na\nb')).toEqual(['清单：\na\nb'])
    // 超出条数不丢字：余下的并回最后一条
    expect(splitBubbles('一\n\n二\n\n三\n\n四')).toEqual(['一', '二', '三\n\n四'])
    expect(splitBubbles('一\n\n二', 1)).toEqual(['一\n\n二'])
    expect(splitBubbles('一\n\n\n二 \n \n 三')).toEqual(['一', '二', '三'])
  })

  it('只展开助手的一轮，用户自己打的段落不替他拆条', () => {
    const out = expandBubbles([
      { id: '1', role: 'user', content: '在吗\n\n晚上打不打', created_at: '2026-10-28 09:00:00' },
      { id: '2', role: 'assistant', content: '在的\n\n打', created_at: '2026-10-28 09:00:20' },
    ])
    expect(out.map(b => [b.id, b.bubble, b.bubbleIndex])).toEqual([
      ['1', '在吗\n\n晚上打不打', 0],
      ['2', '在的', 0],
      ['2', '打', 1],
    ])
  })

  it('纯图消息（正文空）仍占一个气泡位，不能整条消失', () => {
    const out = expandBubbles([{ id: '1', role: 'assistant', content: '', created_at: '2026-10-28 09:00:00' }])
    expect(out).toHaveLength(1)
    expect(out[0].bubble).toBe('')
  })

  it('同一轮的后续气泡按续接处理：头像不重复、分隔线只一条', () => {
    const out = decorateMessages(expandBubbles([
      { id: '1', role: 'assistant', content: '一\n\n二\n\n三', created_at: '2026-10-28 09:00:00' },
    ]))
    expect(out.map(d => d.cont)).toEqual([false, true, true])
    expect(out.filter(d => d.divider)).toHaveLength(1)
  })
})
