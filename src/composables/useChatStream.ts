// src/composables/useChatStream.ts
import { ref } from 'vue'
import { streamChat } from '@/api/client'
import type { ModelConfig } from '@/api/client'
import type { BaseConversation, BaseMessage, ChatMode } from '@/types/chat'
import { useSettingsStore } from '@/stores/settings'

export interface UseChatStreamOptions {
  mode: ChatMode
  getModel: () => ModelConfig | null
}

export interface StartStreamParams<
  M extends BaseMessage = BaseMessage,
  C extends BaseConversation<M> = BaseConversation<M>,
> {
  content: string
  conversation: C
  systemPrompt?: string
  onContent?: (msg: M, token: string) => void
  onReasoning?: (msg: M, token: string) => void
  onError?: (msg: M, err: string, duration: number) => void
  onDone?: (msg: M, duration: number) => void
}

export function useChatStream(options: UseChatStreamOptions) {
  const { mode, getModel } = options
  const streaming = ref(false)
  let abortFn: (() => void) | null = null

  async function startStream<M extends BaseMessage, C extends BaseConversation<M>>(
    params: StartStreamParams<M, C>,
  ): Promise<boolean> {
    const { content, conversation, systemPrompt, onContent, onReasoning, onError, onDone } = params
    const model = getModel()
    if (!model) return false

    const conv = conversation
    const streamStartTime = Date.now()
    streaming.value = true

    // 流式输出开关：关闭时缓冲所有 token，done 时一次性回调（保留 SSE 通道，最小改动）
    const settingsStore = useSettingsStore()
    const streamingEnabled = settingsStore.settings.chat.streaming
    let bufferedContent = ''
    let bufferedReasoning = ''

    abortFn = streamChat({
      message: content,
      conversationId: conv.backendConvId!,
      modelId: model.id,
      enableThinking: model.supports_thinking ?? false,
      systemPrompt,
      mode,
      // 上下文窗口：透传用户设置到后端，覆盖默认 limit=20
      contextWindow: settingsStore.settings.chat.contextWindow,
      onContent: (token) => {
        const last = conv.messages[conv.messages.length - 1]
        if (!last || last.role !== 'assistant') return
        if (streamingEnabled) {
          onContent?.(last as M, token)
        } else {
          bufferedContent += token
        }
      },
      onReasoning: (token) => {
        const last = conv.messages[conv.messages.length - 1]
        if (!last || last.role !== 'assistant') return
        if (streamingEnabled) {
          onReasoning?.(last as M, token)
        } else {
          bufferedReasoning += token
        }
      },
      onError: (err) => {
        streaming.value = false
        abortFn = null
        const last = conv.messages[conv.messages.length - 1]
        if (last && last.role === 'assistant') {
          const duration = Math.round(((Date.now() - streamStartTime) / 1000) * 10) / 10
          // 非流式模式下出错时，把已缓冲的内容回写给消息，避免丢失
          if (!streamingEnabled) {
            if (bufferedContent) onContent?.(last as M, bufferedContent)
            if (bufferedReasoning) onReasoning?.(last as M, bufferedReasoning)
          }
          onError?.(last as M, err, duration)
        }
      },
      onDone: () => {
        streaming.value = false
        abortFn = null
        const last = conv.messages[conv.messages.length - 1]
        if (last && last.role === 'assistant') {
          const duration = Math.round(((Date.now() - streamStartTime) / 1000) * 10) / 10
          // 非流式模式：done 时一次性回调完整内容
          if (!streamingEnabled) {
            if (bufferedContent) onContent?.(last as M, bufferedContent)
            if (bufferedReasoning) onReasoning?.(last as M, bufferedReasoning)
          }
          onDone?.(last as M, duration)
        }
      },
    })

    return true
  }

  function abortStream() {
    if (abortFn) {
      abortFn()
      abortFn = null
      streaming.value = false
    }
  }

  return {
    streaming,
    startStream,
    abortStream,
  }
}
