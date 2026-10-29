import { describe, expect, it } from 'vitest'
import {
  bubbleDeliveryPlan,
  bubbleDelayMs,
  decorateMessages,
  dayDividerLabel,
  expandBubbles,
  peerLabel,
  peerSeed,
  peerTag,
  personaName,
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

  it('微信会话用 wx: 前缀，同样只给尾号代号', () => {
    expect(peerSeed('wx:oXxxxopenid9c3e')).toBe('oXxxxopenid9c3e')
    expect(peerLabel('wx:oXxxxopenid9c3e')).toBe('微信 · 9C3E')
    expect(peerTag('wx:oXxxxopenid9c3e')).toBe('')
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

describe('会话归属人格名 personaName', () => {
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
    const four = [
      '今天这局我先出牌，你把防守的那一手留到最后一轮再跟。',
      '上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。',
      '这周要是真有事就直说，别到点才冒一句不打了，我看着难受。',
      '下回提前半小时说一声，我好把饭吃了再来，不用饿着肚子等你。',
    ]
    expect(splitBubbles(four.join('\n\n'))).toEqual([...four.slice(0, 2), four.slice(2).join('\n\n')])
    expect(splitBubbles('一\n\n二', 1)).toEqual(['一\n\n二'])
    // 多个空行、空行带空格都算同一条边界
    expect(splitBubbles('在的呀\n\n\n晚上八点开一把 \n \n ')).toEqual(['在的呀', '晚上八点开一把'])
  })

  it('三截薄气泡并回一条：与服务端同一兜底', () => {
    // 一句话说完被切三截 —— 模型把「最多 N 条」当配额凑满时的最短那种
    expect(splitBubbles('白饭？\n\n你吃撑了没？\n\n还有吗')).toEqual(['白饭？你吃撑了没？还有吗'])
    // 两段不并：两拍是活的节奏
    expect(splitBubbles('在的\n\n你吃了吗')).toEqual(['在的', '你吃了吗'])
    // 每段都够一句话的三段照旧分开投
    const three = [
      '今天这局我先出牌，你把防守的那一手留到最后一轮再跟。',
      '上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。',
      '这周要是真有事就直说，别到点才冒一句不打了，我看着难受。',
    ]
    expect(splitBubbles(three.join('\n\n'))).toEqual(three)
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

  it('纯图消息（正文空但有图）仍占一个气泡位', () => {
    const out = expandBubbles([{ id: '1', role: 'assistant', content: '', images: ['data:image/webp;base64,AA'], created_at: '2026-10-28 09:00:00' }])
    expect(out).toHaveLength(1)
    expect(out[0].bubble).toBe('')
  })

  it('正文空又没有图的行不占气泡位：空框不该出现在列表里', () => {
    const out = expandBubbles([
      { id: '1', role: 'assistant', content: '  \n ', created_at: '2026-10-28 09:00:00' },
      { id: '2', role: 'assistant', content: '有字', created_at: '2026-10-28 09:00:10' },
    ])
    expect(out.map(b => [b.id, b.bubble])).toEqual([['2', '有字']])
  })

  it('同一轮的后续气泡按续接处理：头像不重复、分隔线只一条', () => {
    // 每段都写够一句话，否则先被上面那条并条规则收走，测不到续接
    const three = [
      '今天这局我先出牌，你把防守的那一手留到最后一轮再跟。',
      '上次你说明天再打，结果拖到周末才上线，我一直惦记着这事。',
      '这周要是真有事就直说，别到点才冒一句不打了，我看着难受。',
    ]
    const out = decorateMessages(expandBubbles([
      { id: '1', role: 'assistant', content: three.join('\n\n'), created_at: '2026-10-28 09:00:00' },
    ]))
    expect(out.map(d => d.cont)).toEqual([false, true, true])
    expect(out.filter(d => d.divider)).toHaveLength(1)
  })
})

describe('在场感（P4）：气泡投递排程', () => {
  it('首气泡零延迟——流式已在渲染它，排程不制造首字延迟', () => {
    const plan = bubbleDeliveryPlan(['早', '在忙吗', '刚看到消息'])
    expect(plan[0]).toBe(0)
    expect(plan.slice(1).every(d => d > 0)).toBe(true)
  })

  it('短句快、长句慢、感叹句多停一拍', () => {
    const short = bubbleDelayMs('嗯', 1)
    const long = bubbleDelayMs('这句话稍微长了一点点，得打一会儿', 1)
    const excited = bubbleDelayMs('天哪！', 1)
    expect(short).toBeLessThan(long)
    expect(excited).toBeGreaterThan(short) // 感叹号 +300
    // 公式抽检：3 字（含全角！）+ 1 感叹 = 500 + 270 + 300 = 1070
    expect(excited).toBe(1070)
  })

  it('上限 4s：再长的气泡也不像「卡了」', () => {
    expect(bubbleDelayMs('很'.repeat(200), 1)).toBe(4000)
  })

  it('jitter 只做乘法缩放，不改变相对次序', () => {
    const a = bubbleDeliveryPlan(['好短', '这一条明显长得多一些'], 1.2)
    const b = bubbleDeliveryPlan(['好短', '这一条明显长得多一些'], 0.8)
    expect(a[1]).toBeGreaterThan(b[1])
    expect(a[0]).toBe(0)
  })

  it('空串与单气泡：只有一位且零延迟', () => {
    expect(bubbleDeliveryPlan([''])).toEqual([0])
    expect(bubbleDeliveryPlan([])).toEqual([])
  })
})
