// S9-22：渲染路径的消息定义必须只有一个来源。
// 这条缺陷的形状不是「少个字段」，而是三套并行定义：types/chat.ts 的 BaseMessage、
// MessageItem 手抄的 ChatMessage（把 content 放宽成可选、没有 created_at）、
// SideChatPanel 再手抄一份 DisplayMsg（带 [key: string]: any 兜住一切）。
// 第三套的注释自己写着「兼容 ModeMessage 和 MessageItem 的 ChatMessage」——
// 它是为了弥合前两套已经漂开的分歧而存在的，而分歧的根源正是没人 import 规范定义：
// 给 BaseMessage 加字段不会传到渲染路径，改了三处也只对一处生效。
// 2026-10-20：SideChatPanel 随 AI 侧栏退役删除，渲染路径的第二个消费点换成首页
// 任务台的消息流（HomeWorkspace）—— 守卫跟着换目标，条件一项不放宽。
import { describe, expect, it } from 'vitest'

import MessageItemSource from './MessageItem.vue?raw'
import HomeWorkspaceSource from '../home/HomeWorkspace.vue?raw'
import chatTypesSource from '../../types/chat.ts?raw'
import displayMessagesSource from '../../utils/display-messages.ts?raw'

// 本地消息接口：interface XxxMessage / DisplayMsg / ChatMessage 一律算手抄。
const LOCAL_MESSAGE_INTERFACE = /^[ \t]*(?:export[ \t]+)?interface[ \t]+\w*(?:Message|Msg)\b/m

describe('消息形态只有一个定义来源（S9-22）', () => {
  const files: Array<[string, string]> = [
    ['MessageItem.vue', MessageItemSource],
    ['HomeWorkspace.vue', HomeWorkspaceSource],
  ]

  it.each(files)('%s 不再本地手抄消息接口', (_name, src) => {
    expect(src).not.toMatch(LOCAL_MESSAGE_INTERFACE)
  })

  it.each(files)('%s 从 @/types/chat 取消息类型', (_name, src) => {
    expect(src).toMatch(/import\s+type\s*\{[^}]*\bBaseMessage\b[^}]*\}\s*from\s*'@\/types\/chat'/)
  })

  it('两个消费点的 prop 声明与 live 判定都落在规范类型 / 单一实现上', () => {
    expect(MessageItemSource).toContain('message: BaseMessage')
    // 任务台不再自己声明消息数组类型：列表直接来自 ssh-ai store，
    // live 合并与逐条判定都走 utils/display-messages 那一份纯函数
    expect(HomeWorkspaceSource).toMatch(/buildDisplayMessages\(\s*sshAi\.messages/)
    expect(HomeWorkspaceSource).toMatch(/import\s*\{[^}]*\bisLiveAgentRun\b[^}]*\}\s*from\s*'@\/utils\/display-messages'/)
    // blockedOnUser：run 停在澄清闸口等用户作答时不算"正在输出"（用户报的
    // "确认期间 AI 还在输出"）。仍是同一个函数、同一处判定，没有第二份口径。
    expect(displayMessagesSource).toMatch(/messages: readonly BaseMessage\[\],\s*idx: number,\s*running: boolean,\s*blockedOnUser = false,\s*\): boolean/)
  })

  it('规范定义覆盖渲染路径真正读的那几个键', () => {
    // 合并的前提是 BaseMessage 够用：消息流读 content/role/id/streamingContent，
    // 气泡还读 reasoning 与 agentTrace。后两个必须是可选的 —— 运行中的消息确实可能
    // 只有 streamingContent，reasoning 只在思考型输出上存在；写成必填就是类型上做假。
    expect(chatTypesSource).toMatch(/interface\s+BaseMessage\s*\{[\s\S]*?\n\}/)
    const body = /interface\s+BaseMessage\s*\{([\s\S]*?)\n\}/.exec(chatTypesSource)?.[1] ?? ''
    for (const key of ['id: string', 'role:', 'content:', 'reasoning?:', 'streamingContent?:', 'agentTrace?:']) {
      expect(body).toContain(key)
    }
  })
})
