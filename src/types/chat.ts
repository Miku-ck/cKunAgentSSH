// src/types/chat.ts

import type { AgentTraceSnapshot } from '@/api/client'

export type ChatMode = 'ssh'

export type PersistenceStrategy = 'backend' | 'dual'

export interface BaseMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  streamingContent?: string
  streamingReasoning?: string
  duration?: number
  /** Agent 执行过程快照（Agent 模式完成后固化到消息上） */
  agentTrace?: AgentTraceSnapshot
  created_at: string
}

export interface BaseConversation<M extends BaseMessage = BaseMessage> {
  id: string
  title: string
  messages: M[]
  /** 后端数据库中的会话 ID（dual 策略需要；backend 策略与 id 相同或无用） */
  backendConvId?: string
  created_at: string
  updated_at: string
}

export interface ModeConfig<
  TContext = unknown,
  TMessage extends BaseMessage = BaseMessage,
  TConversation extends BaseConversation<TMessage> = BaseConversation<TMessage>,
> {
  /** 模式标识 */
  mode: ChatMode
  /** 持久化策略，未指定时默认为 dual */
  persistence?: PersistenceStrategy
  /** localStorage key（dual 策略使用） */
  storageKey: string
  /** 模型选择持久化 key（dual 策略使用） */
  modelStorageKey: string
}
