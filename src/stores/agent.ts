/**
 * Agent Store — 管理 Agent 运行状态
 *
 * 职责:
 * - 管理 Agent 运行事件流（state / content / reasoning / tool_call / done）
 * - 提供 runAgent / abort 方法
 * - 持久化运行历史到后端 agent_runs 表
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { streamAgentRun, type AgentSSEEvent, type AgentCostSnapshot, type AgentFileChangesSummary, type AgentTraceSnapshot, type AgentIntentionInfo } from '@/api/client'
import { StreamingThinkScrubber } from '@/utils/think-scrubber'

export interface ToolCallEntry {
  id: string
  tool: string
  args: Record<string, unknown>
  result?: string
  success?: boolean
  timestamp: number
  /** 所属迭代号（用于按批次分组渲染） */
  iteration?: number
}

export interface HumanHelpRequest {
  id: string
  reason: string
  tool?: string
  args?: Record<string, unknown>
  timestamp: number
}

export interface PermissionRequest {
  id: string
  tool: string
  args: Record<string, unknown>
  risk: string
  reversible: boolean
  message: string
  timestamp: number
  /** 是否正在授权中（防止重复点击） */
  granting?: boolean
  /** 是否已处理（已授权或已忽略） */
  resolved?: boolean
}

export type AgentRunStatus = 'idle' | 'running' | 'completed' | 'failed'

export const useAgentStore = defineStore('agent', () => {
  // ── State ──
  const status = ref<AgentRunStatus>('idle')
  const content = ref('')
  const reasoning = ref('')
  const currentState = ref('')
  const iteration = ref(0)
  const toolCalls = ref<ToolCallEntry[]>([])
  const warnings = ref<string[]>([])
  const errorMessage = ref('')
  const totalIterations = ref(0)
  const stateHistory = ref<Array<{ from: string; event: string; to: string }>>([])
  const humanHelpRequests = ref<HumanHelpRequest[]>([])
  const permissionRequests = ref<PermissionRequest[]>([])
  // P7: 成本信息 + 最近 run_id（供 TraceViewer 使用）
  const costInfo = ref<AgentCostSnapshot | null>(null)
  // 流式 think 块擦洗器（reasoning 模型可能在 content 流里混入 <think> 块）
  const _contentScrubber = new StreamingThinkScrubber()
  const _reasoningScrubber = new StreamingThinkScrubber()
  const lastRunId = ref<string>('')
  // C3: 文件变更摘要（done 事件携带，供 ChatMessages 徽标 + TraceViewer 展示）
  const fileChanges = ref<AgentFileChangesSummary | null>(null)
  // C3 修复：记录当前运行模式，避免 keep-alive 下跨组件 watch 重复 push
  const currentMode = ref<'ssh' | ''>('')
  // 当前正在进行的工具调用（用于状态栏显示真实动作）
  const currentToolCall = ref<ToolCallEntry | null>(null)
  // H14: 意图识别结果（用于规划列表展示）
  const intentionInfo = ref<AgentIntentionInfo | null>(null)
  // H14 修复：人工介入去重标志（同一 run 只接受一次，独立于数组长度）
  let _helpRequestedThisRun = false

  let abortFn: (() => void) | null = null
  // 记录运行开始时间，用于 snapshot() 计算 duration
  let runStartTime: number = 0

  // ── Computed ──
  const isRunning = computed(() => status.value === 'running')
  const hasToolCalls = computed(() => toolCalls.value.length > 0)
  const hasHumanHelpRequest = computed(() => humanHelpRequests.value.length > 0)
  const hasPermissionRequest = computed(() => permissionRequests.value.some(r => !r.resolved))

  /** 当前状态对应的主题色（thinking 蓝色 / acting 橙色 / failed 红色 等） */
  const stateColor = computed((): string => {
    const map: Record<string, string> = {
      init: 'var(--text-secondary)',
      thinking: 'var(--accent-blue)',
      acting: 'var(--accent-orange)',
      observing: 'var(--accent-yellow)',
      reflecting: 'var(--accent-purple)',
      completed: 'var(--accent-green)',
      failed: 'var(--accent-red)',
      human_help: 'var(--accent-red)',
      max_reached: 'var(--accent-yellow)',
    }
    return map[currentState.value] || 'var(--text-secondary)'
  })

  /** 当前正在进行的动作文本，例如"思考中..."/"正在读取文件：main.py" */
  const currentActionText = computed((): string => {
    const state = currentState.value
    const tc = currentToolCall.value

    if (state === 'thinking') return '思考中...'
    if (state === 'reflecting') return '反思总结中...'
    if (state === 'observing') return '观察工具结果...'
    // H14 修复：human_help 终止态显示为"需要人工介入"，而不是"思考中..."
    if (state === 'human_help') return '需要人工介入'

    if (tc && (state === 'acting' || state === 'executing' || !state)) {
      const tool = tc.tool
      const args = tc.args
      const value =
        args.file_path || args.path || args.url || args.query || args.command || args.shell || args.target || args.code || ''
      let target = ''
      if (typeof value === 'string') {
        target = value.length > 30 ? value.slice(0, 27) + '...' : value
      } else if (Array.isArray(value) && value.length > 0) {
        const first = String(value[0])
        target = first.length > 30 ? first.slice(0, 27) + '...' : first
      }
      const toolNameMap: Record<string, string> = {
        read_file: '读取文件',
        write_file: '写入文件',
        list_dir: '列出目录',
        shell_exec: '执行命令',
        web_search: '搜索网络',
        web_fetch: '获取网页',
      }
      const verb = toolNameMap[tool] || tool
      return target ? `正在${verb}：${target}` : `正在调用 ${tool}...`
    }

    if (state === 'acting') return '执行工具中...'
    return '思考中...'
  })

  // ── Actions ──

  function runAgent(options: {
    task: string
    conversationId: string
    modelId: string
    mode?: 'ssh'
    systemPrompt?: string
    sshSessionId?: string
    enableThinking?: boolean
    enableTools?: boolean
    maxIterations?: number
    enableDynamicBudget?: boolean
    skillIds?: string[]
  }) {
    // C10 修复：并发保护 — 如果有正在运行的流，先中止它，避免多个流并行导致状态混乱
    if (abortFn) {
      abortFn()
      abortFn = null
    }

    // 重置状态
    _reset()

    runStartTime = Date.now()

    status.value = 'running'
    currentState.value = 'thinking'
    // C3 修复：记录当前模式，watch 内据此判断是否为本组件的运行
    currentMode.value = options.mode || 'ssh'

    abortFn = streamAgentRun({
      ...options,
      sshSessionId: options.sshSessionId,
      onEvent: (event: AgentSSEEvent) => _handleEvent(event),
      onError: (err: string) => {
        errorMessage.value = err?.trim()
          ? err
          : 'Agent 运行失败，后端未返回具体错误，请查看后端日志'
        status.value = 'failed'
        currentState.value = 'failed'
        // H14 修复：流结束后清除 abortFn，避免对已结束的流调用 abort 重置状态
        abortFn = null
      },
      onDone: () => {
        if (status.value === 'running') {
          status.value = 'completed'
        }
        // H14 修复：流结束后清除 abortFn
        abortFn = null
      },
    })
  }

  function abort() {
    if (abortFn) {
      abortFn()
      abortFn = null
    }
    status.value = 'idle'
  }

  /** 生成当前运行状态的快照（不重置状态），用于固化到 ChatMessage.agentTrace */
  function snapshot(): AgentTraceSnapshot {
    const durationMs = runStartTime > 0 ? Date.now() - runStartTime : 0
    return {
      // H14 修复：human_help 状态在快照中显示为 failed 以兼容类型，currentState 保留 human_help
      status: status.value === 'failed' || currentState.value === 'human_help' ? 'failed' : 'completed',
      currentState: currentState.value || 'completed',
      iterations: totalIterations.value || iteration.value + 1,
      duration: Math.round(durationMs / 100) / 10, // 秒，保留1位小数
      // 深拷贝 toolCalls 避免引用共享
      toolCalls: toolCalls.value.map(tc => ({
        id: tc.id,
        tool: tc.tool,
        args: { ...tc.args },
        result: tc.result,
        success: tc.success,
        timestamp: tc.timestamp,
        iteration: tc.iteration,
      })),
      reasoning: reasoning.value,
      lastRunId: lastRunId.value,
      fileChanges: fileChanges.value || undefined,
      costInfo: costInfo.value || undefined,
      errorMessage: errorMessage.value || undefined,
    }
  }

  function _reset() {
    status.value = 'idle'
    content.value = ''
    reasoning.value = ''
    // 重置流式擦洗器，避免上一轮残留 buf 污染新一轮
    _contentScrubber.reset()
    _reasoningScrubber.reset()
    currentState.value = ''
    iteration.value = 0
    toolCalls.value = []
    warnings.value = []
    errorMessage.value = ''
    totalIterations.value = 0
    stateHistory.value = []
    humanHelpRequests.value = []
    permissionRequests.value = []
    costInfo.value = null
    lastRunId.value = ''
    fileChanges.value = null
    currentMode.value = ''
    currentToolCall.value = null
    intentionInfo.value = null
    _helpRequestedThisRun = false
  }

  /** 用户确认/忽略某次求助请求 */
  function dismissHelp(id: string) {
    humanHelpRequests.value = humanHelpRequests.value.filter(r => r.id !== id)
  }

  /** 用户忽略某次权限请求 */
  function dismissPermission(id: string) {
    permissionRequests.value = permissionRequests.value.filter(r => r.id !== id)
  }

  /** 标记权限请求已处理 */
  function resolvePermission(id: string, granted: boolean) {
    const req = permissionRequests.value.find(r => r.id === id)
    if (req) {
      req.resolved = true
      req.granting = false
    }
  }

  function _handleEvent(event: AgentSSEEvent) {
    switch (event.type) {
      case 'state':
        if (event.state) currentState.value = event.state
        if (event.iteration !== undefined) iteration.value = event.iteration
        break

      case 'content':
        if (event.content) {
          // 流式擦洗 <think> 块（reasoning 模型可能把 CoT 混入 content）
          const visible = _contentScrubber.feed(event.content)
          if (visible) content.value += visible
        }
        break

      case 'reasoning':
        if (event.reasoning) {
          // reasoning 流也擦洗（防止外层重复显示 think 块）
          const visible = _reasoningScrubber.feed(event.reasoning)
          if (visible) reasoning.value += visible
        }
        break

      case 'tool_call_start':
        if (event.tool) {
          const entry: ToolCallEntry = {
            id: `${event.tool}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            tool: event.tool,
            args: event.args || {},
            timestamp: Date.now(),
            iteration: event.iteration ?? iteration.value,
          }
          toolCalls.value.push(entry)
          // 记录当前正在执行的工具，用于状态栏显示真实动作
          currentToolCall.value = entry
        }
        break

      case 'tool_call_result':
        if (event.tool) {
          // 优先按 iteration + tool 精确匹配
          const eventIter = event.iteration
          let tc: ToolCallEntry | undefined
          if (eventIter !== undefined) {
            tc = [...toolCalls.value].reverse().find(t => t.tool === event.tool && t.iteration === eventIter && !t.result)
          }
          // 降级：反向找同名无 result
          if (!tc) {
            tc = [...toolCalls.value].reverse().find(t => t.tool === event.tool && !t.result)
          }
          if (tc) {
            tc.result = event.result
            tc.success = event.success
          }
          // 工具调用完成，清空当前动作显示
          if (currentToolCall.value?.tool === event.tool && !currentToolCall.value.result) {
            currentToolCall.value = null
          }
        }
        break

      case 'warning':
        if (event.message) {
          warnings.value.push(event.message)
          // 把导致运行终止的关键警告同步到 errorMessage，
          // 避免最终 content 为空时前端显示 "未产生输出"
          if (/达到最大迭代数|强制终止|运行失败/.test(event.message)) {
            errorMessage.value = event.message
          }
        }
        break

      case 'human_help_needed':
        // H14 修复：显式标志位去重，避免用户忽略后 length===0 导致重复接受
        if (!_helpRequestedThisRun) {
          _helpRequestedThisRun = true
          humanHelpRequests.value.push({
            id: `help_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            reason: event.reason || 'Agent 请求人类介入',
            tool: event.tool,
            args: event.args,
            timestamp: Date.now(),
          })
        }
        break

      case 'permission_needed':
        // H14 修复：按工具名去重，避免同一工具多次触发产生重复卡片
        if (!permissionRequests.value.some(r => r.tool === event.tool)) {
          permissionRequests.value.push({
            id: `perm_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            tool: event.tool || '',
            args: event.args || {},
            risk: event.risk || 'moderate',
            reversible: event.reversible ?? false,
            message: event.message || `需要授权: ${event.tool}`,
            timestamp: Date.now(),
          })
        }
        break

      case 'error':
        errorMessage.value = event.message || 'Unknown error'
        status.value = 'failed'
        // H13 修复：同步更新 currentState，避免 UI 仍显示"思考中"
        currentState.value = 'failed'
        // H1 修复：不在 error 事件清理 abortFn，此时流可能仍未关闭
        // abortFn 由 onDone/onError 回调（真正代表流结束）清理
        break

      case 'intention':
        if (event.intention) {
          intentionInfo.value = event.intention
        }
        break

      case 'done':
        // 流结束：flush 擦洗器，输出残留 buf（仍在 think 块内则丢弃，防泄露 partial CoT）
        {
          const contentTail = _contentScrubber.flush()
          if (contentTail) content.value += contentTail
          const reasoningTail = _reasoningScrubber.flush()
          if (reasoningTail) reasoning.value += reasoningTail
        }
        if (event.iterations !== undefined) totalIterations.value = event.iterations
        if (event.state_history) stateHistory.value = event.state_history
        // P7: 捕获成本快照 + run_id
        if (event.cost) costInfo.value = event.cost
        if (event.run_id) lastRunId.value = event.run_id
        // C3: 捕获文件变更摘要
        if (event.file_changes) fileChanges.value = event.file_changes
        // H14 修复：支持 human_help 终止态，保留 currentState 供 UI 显示"需要人工介入"
        status.value = event.state === 'failed' || event.state === 'human_help' ? 'failed' : 'completed'
        // 保留有意义的 currentState（如 human_help），仅在 event.state 为空时回退
        if (event.state) {
          currentState.value = event.state
        } else if (status.value !== 'failed') {
          currentState.value = 'completed'
        }
        break
    }
  }

  return {
    // state
    status,
    content,
    reasoning,
    currentState,
    iteration,
    toolCalls,
    warnings,
    errorMessage,
    totalIterations,
    stateHistory,
    humanHelpRequests,
    permissionRequests,
    costInfo,
    lastRunId,
    fileChanges,
    currentMode,
    currentToolCall,
    intentionInfo,
    // computed
    isRunning,
    hasToolCalls,
    hasHumanHelpRequest,
    hasPermissionRequest,
    stateColor,
    currentActionText,
    // actions
    runAgent,
    abort,
    snapshot,
    dismissHelp,
    dismissPermission,
    resolvePermission,
  }
})
