/**
 * 标题行与首条消息同文时不画（首页"两个用户气泡"缺陷的回归）。
 *
 * 现场：首页顶部一行「你好」+ 下方一个「你好」用户气泡。第一条是会话标题
 * （`.home-title`），因为隐式建会话的标题就是首条消息的 30 字摘要 —— 标题
 * 随消息流滚动、贴在气泡正上方，同文时读起来就是一问一答之外的第二条消息。
 */
import { describe, expect, it } from 'vitest'
import { isTitleEcho } from './conversation-title'

describe('isTitleEcho', () => {
  it('标题与首条消息同文', () => {
    expect(isTitleEcho('你好', '你好')).toBe(true)
    expect(isTitleEcho('  你好  ', '你好')).toBe(true)
  })

  it('标题是首条消息的 30 字摘要（带省略号）也算同文', () => {
    const long = '帮我检查一下 nginx 502 报错，顺便看看 upstream 的连接数'
    expect(isTitleEcho(long.slice(0, 30) + '...', long)).toBe(true)
    expect(isTitleEcho(long.slice(0, 30) + '…', long)).toBe(true)
  })

  it('后端另起的标题与首条消息不同 → 保留标题行', () => {
    expect(isTitleEcho('nginx 502 排查', '帮我检查一下 nginx 502 报错')).toBe(false)
  })

  it('空标题 / 空消息不误判', () => {
    expect(isTitleEcho('', '你好')).toBe(false)
    expect(isTitleEcho('你好', '')).toBe(false)
    expect(isTitleEcho('...', '你好')).toBe(false)
  })
})
