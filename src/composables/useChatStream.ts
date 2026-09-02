// src/composables/useChatStream.ts
import { ref, onScopeDispose } from 'vue'
import { streamChat, type ChatAttachment } from '@/api/client'
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
  attachments?: ChatAttachment[]
  onContent?: (msg: M, token: string) => void
  onReasoning?: (msg: M, token: string) => void
  onError?: (msg: M, err: string, duration: number) => void
  onDone?: (msg: M, duration: number) => void
}

export function useChatStream(options: UseChatStreamOptions) {
  const { mode, getModel } = options
  const streaming = ref(false)
  let abortFn: (() => void) | null = null
  // M15：流代际计数器 — 每代流自增，回调比对代际后再写共享状态，
  // 防止旧流的迟到 onError/onDone 清空新流的 abortFn/streaming 或把旧错误写入新消息
  let currentStreamId = 0

  async function startStream<M extends BaseMessage, C extends BaseConversation<M>>(
    params: StartStreamParams<M, C>,
  ): Promise<boolean> {
    const { content, conversation, systemPrompt, attachments, onContent, onReasoning, onError, onDone } = params
    const model = getModel()
    if (!model) return false

    // 防止重复启动：已有流运行时先中止旧流
    if (streaming.value && abortFn) {
      abortFn()
      abortFn = null
      streaming.value = false
    }

    const conv = conversation
    const streamStartTime = Date.now()
    // M15：本代流 id；isCurrent() 为真才允许回调触碰共享 streaming/abortFn
    const streamId = ++currentStreamId
    const isCurrent = () => streamId === currentStreamId
    streaming.value = true

    // 后端会话 ID 校验，避免传入 undefined 导致难以定位的错误
    if (!conv.backendConvId) {
      streaming.value = false
      throw new Error('会话尚未初始化，请先创建后端会话')
    }

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
      // 附带文件：随用户消息注入 LLM 上下文
      attachments,
      onContent: (token) => {
        if (!isCurrent()) return  // M15：旧流迟到 token，丢弃
        const last = conv.messages[conv.messages.length - 1]
        if (!last || last.role !== 'assistant') return
        if (streamingEnabled) {
          onContent?.(last as M, token)
        } else {
          bufferedContent += token
        }
      },
      onReasoning: (token) => {
        if (!isCurrent()) return  // M15：旧流迟到 token，丢弃
        const last = conv.messages[conv.messages.length - 1]
        if (!last || last.role !== 'assistant') return
        if (streamingEnabled) {
          onReasoning?.(last as M, token)
        } else {
          bufferedReasoning += token
        }
      },
      onError: (err) => {
        if (!isCurrent()) return  // M15：旧流迟到错误，勿清空新流 abortFn/streaming
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
        if (!isCurrent()) return  // M15：旧流迟到完成，勿清空新流状态
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

  // 作用域销毁时中止活跃流，防止 SSE 在后台空跑。
  // 注意：本 composable 在 createModeChatStore 的 store setup 作用域内实例化，
  // 组件卸载（onUnmounted）不会触发；改用 onScopeDispose：
  // - store 被 $dispose（应用销毁）时触发，保留应用级兜底；
  // - 若未来在组件 setup 内使用，组件作用域销毁时同样会触发。
  // 面板关闭路径由 ssh-ai store 的 closeAiPanel() 显式调用 abortStream() 处理。
  onScopeDispose(() => {
    if (abortFn) {
      abortFn()
      abortFn = null
      streaming.value = false
    }
  })

  return {
    streaming,
    startStream,
    abortStream,
  }
}
