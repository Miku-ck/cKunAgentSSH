// S9-22：渲染路径的消息定义必须只有一个来源。
// 这条缺陷的形状不是「少个字段」，而是三套并行定义：types/chat.ts 的 BaseMessage、
// MessageItem 手抄的 ChatMessage（把 content 放宽成可选、没有 created_at）、
// SideChatPanel 再手抄一份 DisplayMsg（带 [key: string]: any 兜住一切）。
// 第三套的注释自己写着「兼容 ModeMessage 和 MessageItem 的 ChatMessage」——
// 它是为了弥合前两套已经漂开的分歧而存在的，而分歧的根源正是没人 import 规范定义：
// 给 BaseMessage 加字段不会传到渲染路径，改了三处也只对一处生效。
import { describe, expect, it } from 'vitest'

import MessageItemSource from './MessageItem.vue?raw'
import SideChatPanelSource from './SideChatPanel.vue?raw'
import chatTypesSource from '../../types/chat.ts?raw'
import displayMessagesSource from '../../utils/display-messages.ts?raw'

// 本地消息接口：interface XxxMessage / DisplayMsg / ChatMessage 一律算手抄。
const LOCAL_MESSAGE_INTERFACE = /^[ \t]*(?:export[ \t]+)?interface[ \t]+\w*(?:Message|Msg)\b/m

describe('消息形态只有一个定义来源（S9-22）', () => {
  const files: Array<[string, string]> = [
    ['MessageItem.vue', MessageItemSource],
    ['SideChatPanel.vue', SideChatPanelSource],
  ]

  it.each(files)('%s 不再本地手抄消息接口', (_name, src) => {
    expect(src).not.toMatch(LOCAL_MESSAGE_INTERFACE)
  })

  it.each(files)('%s 从 @/types/chat 取消息类型', (_name, src) => {
    expect(src).toMatch(/import\s+type\s*\{[^}]*\bBaseMessage\b[^}]*\}\s*from\s*'@\/types\/chat'/)
  })

  it('两个消费点的 prop 声明都落在规范类型上', () => {
    expect(MessageItemSource).toContain('message: BaseMessage')
    expect(SideChatPanelSource).toContain('messages: BaseMessage[]')
    // live 判定已收归 utils/display-messages：两处都 import 同一份，
    // 不再各自声明一个签名相同的本地函数（那正是第二套口径的入口）
    expect(displayMessagesSource).toMatch(/messages: readonly BaseMessage\[\],\s*idx: number,\s*running: boolean,\s*\): boolean/)
    expect(SideChatPanelSource).toMatch(/import\s*\{[^}]*\bisLiveAgentRun\b[^}]*\}\s*from\s*'@\/utils\/display-messages'/)
  })

  it('规范定义覆盖渲染路径真正读的那几个键', () => {
    // 合并的前提是 BaseMessage 够用：面板读 content/role/id/streamingContent，
    // 气泡还读 reasoning 与 agentTrace。后两个必须是可选的 —— 运行中的消息确实可能
    // 只有 streamingContent，reasoning 只在思考型输出上存在；写成必填就是类型上做假。
    expect(chatTypesSource).toMatch(/interface\s+BaseMessage\s*\{[\s\S]*?\n\}/)
    const body = /interface\s+BaseMessage\s*\{([\s\S]*?)\n\}/.exec(chatTypesSource)?.[1] ?? ''
    for (const key of ['id: string', 'role:', 'content:', 'reasoning?:', 'streamingContent?:', 'agentTrace?:']) {
      expect(body).toContain(key)
    }
  })
})
