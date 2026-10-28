/**
 * 连接页守卫 —— 渠道与集成分组，但只渲染真实存在的条目：
 * 目前只有 QQ 一张卡，不摆空的「集成」占位卡。
 */
import pageRaw from './ConnectionPage.vue?raw'
import { describe, expect, it } from 'vitest'

const src = pageRaw.replace(/\r\n/g, '\n')

describe('连接页结构', () => {
  it('只留标题与分组标题，不再挂解释性副文案', () => {
    expect(src).toContain('<h2>连接</h2>')
    expect(src).not.toContain('渠道决定机器人出现在哪里')
    expect(src).not.toContain('机器人随应用启动')
  })

  it('渠道组挂 QQ 渠道卡', () => {
    expect(src).toContain('<h3 id="connection-channels">渠道</h3>')
    expect(src).toContain('<QqChannelCard :personas="personas" />')
  })

  it('集成组等真实条目再出现，现在不摆空卡', () => {
    expect(src).not.toContain('<h3 id="connection-integrations">集成</h3>')
    expect(src).not.toContain('敬请期待')
  })
})
