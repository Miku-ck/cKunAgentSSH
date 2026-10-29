/**
 * 连接页守卫 —— 渠道分组渲染真实存在的机器人：
 * 一次请求带回全部状态（卡片不各自轮询），没有条目就不摆空占位。
 */
import pageRaw from './ConnectionPage.vue?raw'
import cardRaw from './ChannelCard.vue?raw'
import { describe, expect, it } from 'vitest'

const src = pageRaw.replace(/\r\n/g, '\n')
const card = cardRaw.replace(/\r\n/g, '\n')

describe('连接页结构', () => {
  it('页内不再重复标签页标题，也不挂解释性副文案', () => {
    // 「连接」已经是顶部标签页的名字，页内再摆一个大标题只是白占一行
    expect(src).not.toContain('<h2>连接</h2>')
    expect(src).not.toContain('section-heading')
    expect(src).not.toContain('渠道决定机器人出现在哪里')
    expect(src).not.toContain('机器人随应用启动')
  })

  it('渠道组按列表渲染卡片，并提供新增入口', () => {
    expect(src).toContain('<h3 id="connection-channels">渠道</h3>')
    expect(src).toContain('v-for="channel in channels"')
    expect(src).toContain(':channel="channel"')
    expect(src).toContain('新增机器人')
  })

  it('草稿卡只在点新增后出现，取消即收起', () => {
    expect(src).toContain('v-if="draftOpen"')
    expect(src).toContain(':channel="null"')
    expect(src).toContain('@discard="draftOpen = false"')
  })

  it('集成组等真实条目再出现，现在不摆空卡', () => {
    expect(src).not.toContain('<h3 id="connection-integrations">集成</h3>')
    expect(src).not.toContain('敬请期待')
  })
})

describe('连接页轮询', () => {
  it('整页 5 秒一次拉全部，卸载时清理定时器', () => {
    expect(src).toMatch(/setInterval\(\(\) => \{ void refresh\(\) \}, 5000\)/)
    expect(src).toContain('botApi.listChannels()')
    expect(src).toContain('botApi.getWebhook()')
    expect(src).toContain(':webhook-base="webhookBase"')
    expect(src).toContain('clearInterval(timer)')
    expect(src).toContain('onUnmounted')
  })

  it('并发保护：上一轮没回来不起第二轮', () => {
    expect(src).toContain('if (refreshing) return')
  })

  it('卡片自己不轮询，N 个机器人也只有页面那一份请求', () => {
    expect(card).not.toContain('setInterval')
    expect(card).not.toContain('clearInterval')
  })

  it('轮询不覆盖正在编辑的表单', () => {
    expect(card).toContain('if (!expanded.value) fill(channel)')
    expect(card).toContain('if (expanded.value) fill(props.channel)')
  })
})
