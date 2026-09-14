// src/types/chat.ts

import type { AgentTraceSnapshot } from '@/api/client'

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
  /** 后端数据库中的会话 ID（dual 策略下首次运行时懒创建回填） */
  backendConvId?: string
  created_at: string
  updated_at: string
}

/** SSH 模式会话消息（context 为 store 注入的 SSH/AI 上下文对象，见 ssh-ai.SshAiContext） */
export interface ModeMessage extends BaseMessage {
  context?: any
}

export type ModeConversation = BaseConversation<ModeMessage>

/** 历史会话分组（今天/昨天/更早 展示契约，HistoryPanel 与 SideChatPanel 共用） */
export interface ConvGroup {
  label: string
  items: Array<{ id: string; title: string; updated_at: string }>
}

/** 会话/模型选择的 localStorage 键（固定 dual 持久化策略） */
export interface ChatStorageKeys {
  storageKey: string
  modelStorageKey: string
}
