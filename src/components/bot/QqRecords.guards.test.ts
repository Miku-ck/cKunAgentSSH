/**
 * QqRecords 守卫 —— 本仓没有组件测试环境（vitest 为 node 环境），沿用 `?raw`
 * 读源码钉契约。钉三件事：
 * 1. 只读：记录区不能长出输入框或发送按钮，回复仍在 QQ 客户端完成；
 * 2. 数据来源：QQ 渠道列表 + 消息接口，5 秒轮询且卸载时清理；
 * 3. 可辨识：群聊/私聊徽标 + 本地时区时间。
 */
import recordsRaw from './QqRecords.vue?raw'
import { describe, expect, it } from 'vitest'

const src = recordsRaw.replace(/\r\n/g, '\n')

describe('QQ 记录区只读', () => {
  it('不渲染输入框、表单或发送动作', () => {
    expect(src).not.toContain('<textarea')
    expect(src).not.toContain('@submit')
    expect(src).not.toContain('sendMessage')
  })

  it('空态引导用户去 QQ 里触发一次', () => {
    expect(src).toContain('还没有 QQ 消息')
  })
})

describe('QQ 记录区数据与刷新', () => {
  it('只拉 qq 渠道会话与对应消息', () => {
    expect(src).toContain('botApi.listQqConversations()')
    expect(src).toContain('botApi.getMessages(activeId.value)')
  })

  it('5 秒轮询，卸载时清理定时器', () => {
    expect(src).toMatch(/setInterval\(\(\) => \{ void refresh\(\) \}, 5000\)/)
    expect(src).toContain('clearInterval(timer)')
    expect(src).toContain('onUnmounted')
  })

  it('轮询不打断正在上翻的用户：仅在贴底时自动滚到底', () => {
    expect(src).toContain('const stick = atBottom()')
    expect(src).toContain('if (stick) await scrollToBottom()')
  })
})

describe('QQ 记录区可辨识', () => {
  it('群聊/私聊徽标来自 external_id', () => {
    expect(src).toContain("startsWith('group:') ? '群聊' : '私聊'")
  })

  it('时间按本地时区格式化', () => {
    expect(src).toContain('formatLocalDateTime(c.updated_at)')
    expect(src).toContain('formatLocalDateTime(m.created_at)')
  })
})
