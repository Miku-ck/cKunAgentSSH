/**
 * HTTP 客户端封装 — 基于 backendPort 构建 API URL
 *
 * 三种访问场景：
 * 1. Electron 渲染进程：通过 IPC 拿主进程分配的 backendPort + 127.0.0.1（loopback，最稳）
 * 2. 浏览器本机访问（http://localhost:5173）：用当前页面 host + 8010（与后端 dev 端口一致）
 * 3. 浏览器外部访问（http://[本机IP]:5173）：必须用 window.location.hostname 替代 127.0.0.1，
 *    否则请求会指向用户本机的 8010（不存在）
 */
import { useAppStore } from '@/stores/app'

const FALLBACK_PORT = 8010  // 后端 dev 默认端口（与 backend/main.py --port default 一致）

/**
 * 统一错误对象：携带用户友好的 message，同时保留原始错误用于调试。
 */
export class FriendlyError extends Error {
  constructor(
    message: string,
    public original: unknown,
    public code?: string
  ) {
    super(message)
    this.name = 'FriendlyError'
  }
}

const TECHNICAL_ERROR_PATTERNS = [
  /SQL|database|sqlite|psycopg|mysql|postgres/i,
  /Traceback|Exception|Error:\s/i,
  /Internal Server Error/i,
  /Failed to fetch|NetworkError|fetch failed|net::/i,
  /Backend not ready/i,
  /ENOTFOUND|ECONNREFUSED|ETIMEDOUT|ECONNRESET/i,
]

function isTechnicalErrorMessage(msg: string): boolean {
  return TECHNICAL_ERROR_PATTERNS.some(p => p.test(msg))
}

/**
 * 把底层/代码级错误转换为用户可读的友好提示。
 * 保留业务性错误（如"用户名或密码错误"）不变。
 */
export function toFriendlyMessage(e: unknown, fallback = '操作失败，请稍后重试'): string {
  if (e instanceof FriendlyError) return e.message
  if (e instanceof Error) {
    const msg = e.message
    if (!msg) return fallback
    if (/Backend not ready|后端未就绪/i.test(msg)) return '后端服务未就绪，请稍后重试'
    if (/fetch failed|NetworkError|Failed to fetch|网络连接失败/i.test(msg)) return '网络连接失败，请检查服务是否运行'
    if (/未登录|登录已过期|Unauthorized|401/i.test(msg)) return '未登录或登录已过期，请重新登录'
    if (isTechnicalErrorMessage(msg)) return '服务暂时不可用，请稍后重试'
    return msg
  }
  return fallback
}

function isElectron(): boolean {
  return typeof window !== 'undefined' && !!window.electronAPI?.getBackendPort
}

function getBaseUrl(): string {
  if (isElectron()) {
    // Electron：走 IPC 拿主进程真实端口
    const appStore = useAppStore()
    const port = appStore.backendPort
    if (!port) throw new Error('Backend not ready')
    return `http://127.0.0.1:${port}/api`
  }
  // 浏览器：用当前页面 host（dev 端口 5173 → 后端 8010）
  let host = typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1'
  // H13 修复：IPv6 [::1] → IPv4 127.0.0.1
  // 原因：vite dev server 可能绑定到 [::1]:5173（IPv6 only），导致 window.location.hostname 返回 [::1]；
  // 但后端监听 0.0.0.0:8010（IPv4 only），用 [::1]:8010 发请求会被拒绝（Failed to fetch）。
  // 统一映射到 127.0.0.1，确保 IPv4 可达。
  if (host === '[::1]' || host === '::1' || host === 'localhost') {
    host = '127.0.0.1'
  }
  return `http://${host}:${FALLBACK_PORT}/api`
}

/**
 * 轮询后端健康检查，等待后端恢复就绪
 * 通过 IPC 获取最新端口（后端重启可能更换端口）
 * @param timeoutMs 最大等待时间，超时后抛出错误
 */
let _healthCheckPromise: Promise<void> | null = null

async function waitForBackendReady(timeoutMs = 8000): Promise<void> {
  // 并发去重：多个请求同时遇到网络错误时，复用同一个健康检查 Promise，避免 hammer /api/health
  if (_healthCheckPromise) {
    return _healthCheckPromise
  }
  _healthCheckPromise = (async () => {
    try {
      const start = Date.now()
      while (Date.now() - start < timeoutMs) {
        // 解析后端 host:port（Electron 走 IPC，浏览器走 window.location）
        const baseUrl = getBaseUrl()
        try {
          const res = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(2000) })
          if (res.ok) return
        } catch {
          // 后端未就绪，继续等待
        }
        await new Promise(r => setTimeout(r, 800))
      }
      throw new Error('Backend not ready after waiting')
    } finally {
      _healthCheckPromise = null
    }
  })()
  return _healthCheckPromise
}

async function request<T>(path: string, options?: RequestInit, _retrying = false): Promise<T> {
  const url = `${getBaseUrl()}${path}`
  const { headers: customHeaders, ...restOptions } = options || {}
  // FormData 请求由浏览器自动设置 Content-Type（含 multipart boundary），不要手动覆盖
  const isFormData = restOptions.body instanceof FormData
  try {
    const res = await fetch(url, {
      ...restOptions,
      headers: {
        ...(restOptions.method && restOptions.method !== 'GET' && !isFormData ? { 'Content-Type': 'application/json' } : {}),
        ...(customHeaders as Record<string, string>),
      },
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || res.statusText)
    }
    if (res.status === 204) return {} as T
    return res.json()
  } catch (e: unknown) {
    // 网络层错误（后端崩溃/重启）：等待后端恢复后重试一次
    const isNetworkError = e instanceof TypeError && /fetch/i.test(e.message)
    if (isNetworkError && !_retrying) {
      console.warn(`[API] Request ${path} failed (network), waiting for backend to recover...`)
      try {
        await waitForBackendReady()
        // 后端已恢复，继续重试请求
        return request<T>(path, options, true)
      } catch {
        // 后端未恢复，抛出统一友好错误
        throw new FriendlyError('网络连接失败，请检查服务是否运行', e)
      }
    }
    // 将技术/代码级错误转换为用户友好提示；业务错误（如用户名或密码错误）保持不变
    const friendly = toFriendlyMessage(e, '请求失败，请稍后重试')
    // 已经是 FriendlyError 时，避免二次包装导致信息丢失
    if (e instanceof FriendlyError) throw e
    if (e instanceof Error && friendly === e.message) throw e
    throw new FriendlyError(friendly, e)
  }
}

// ── 会话 API ──

export interface Conversation {
  id: string
  title: string
  status: 'in_progress' | 'completed' | 'failed'
  mode?: string
  created_at: string
  updated_at: string
  preview?: string
}

export interface Message {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  reasoning?: string
  model_used?: string
  tokens_used: number
  run_id?: string
  agent_trace?: AgentTraceSnapshot
  created_at: string
}

export interface ModelConfig {
  id: string
  name: string
  model_id?: string
  provider?: string
  supports_thinking: boolean
  supports_image?: boolean
  supports_tool_calling?: boolean
  is_default?: boolean
  is_default_ssh?: boolean
  base_url?: string
  api_key?: string
  temperature?: number
  max_tokens?: number
  max_output_tokens?: number
  top_p?: number
  sort_order?: number
  created_at?: string
  updated_at?: string
}

export interface ModelConfigPayload {
  name: string
  base_url: string
  api_key?: string
  model_id?: string
  provider?: string
  supports_thinking?: boolean
  supports_image?: boolean
  supports_tool_calling?: boolean
  temperature?: number
  max_tokens?: number
  max_output_tokens?: number
  top_p?: number
}

export interface Skill {
  id: string
  name: string
  slug: string
  description: string
  category: string
  system_prompt: string
  source: 'builtin' | 'manual' | 'auto'
  is_builtin: number
  enabled: number
  priority: number
  trigger_words: string
  mode_enabled?: boolean
  created_at: string
  updated_at: string
}

export const api = {
  // 会话
  listConversations: (limit = 50, offset = 0, mode?: string) =>
    request<Conversation[]>(`/chat/conversations?limit=${limit}&offset=${offset}${mode ? `&mode=${mode}` : ''}`),

  createConversation: (title?: string, mode: string = 'ssh') =>
    request<Conversation>('/chat/conversations', {
      method: 'POST',
      body: JSON.stringify({ title, mode }),
    }),

  deleteConversation: (id: string) =>
    request<{ deleted: boolean }>(`/chat/conversations/${id}`, {
      method: 'DELETE',
    }),

  updateConversationTitle: (id: string, title: string) =>
    request<Conversation>(`/chat/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),

  // 消息
  getMessages: (conversationId: string, limit = 50, offset = 0) =>
    request<Message[]>(`/chat/conversations/${conversationId}/messages?limit=${limit}&offset=${offset}`),

  // 模型
  getModels: () =>
    request<ModelConfig[]>('/models'),

  // 模型配置管理
  getModelConfigs: () =>
    request<ModelConfig[]>('/settings/models'),

  getDefaultModel: (mode?: string) =>
    request<{ id: string; name?: string; model_id?: string }>(`/settings/models/default${mode ? `?mode=${mode}` : ''}`),

  createModelConfig: (payload: ModelConfigPayload) =>
    request<ModelConfig>('/settings/models', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateModelConfig: (id: string, payload: Partial<ModelConfigPayload>) =>
    request<ModelConfig>(`/settings/models/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  deleteModelConfig: (id: string) =>
    request<{ deleted: boolean }>(`/settings/models/${id}`, {
      method: 'DELETE',
    }),

  // 后处理设置
  getPostProcessSettings: () =>
    request<{ enabled: boolean; generate_title: boolean; extract_memory: boolean; generate_skill: boolean }>(
      '/settings/post-process',
    ),

  updatePostProcessSettings: (payload: {
    enabled: boolean
    generate_title: boolean
    extract_memory: boolean
    generate_skill: boolean
  }) =>
    request<{ enabled: boolean; generate_title: boolean; extract_memory: boolean; generate_skill: boolean }>(
      '/settings/post-process',
      {
        method: 'PUT',
        body: JSON.stringify(payload),
      },
    ),

  testModelConnection: async (id: string): Promise<{ status: 'ok'; latency_ms: number; model_name?: string } | { status: 'error'; detail: string; model_name?: string }> => {
    try {
      const body = await request<{ status: 'ok'; latency_ms: number; model_name?: string } | { status: 'error'; detail: string; model_name?: string }>(
        `/settings/models/${id}/test`,
        { method: 'POST' },
      )
      return body
    } catch (e) {
      console.error('[TEST-CONN] request threw:', e)
      return { status: 'error', detail: e instanceof Error ? e.message : String(e) }
    }
  },

  // 技能
  listSkills: (mode?: string) =>
    request<Skill[]>(`/skills${mode ? `?mode=${mode}` : ''}`),

  createSkill: (skill: Partial<Skill>) =>
    request<Skill>('/skills', {
      method: 'POST',
      body: JSON.stringify(skill),
    }),

  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) =>
    request<{ id: string; enabled: boolean }>(`/skills/${id}/enabled${mode ? `?mode=${mode}` : ''}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),

  deleteSkill: (id: string) =>
    request<{ deleted: boolean }>(`/skills/${id}`, {
      method: 'DELETE',
    }),

  // 终端预测
  predictTerminalCommand: (payload: {
    session_id: string
    current_input?: string
    recent_commands?: string[]
    cwd?: string
    platform?: string
    model_id?: string
  }) =>
    request<{ predictions: string[]; model_used: string }>('/terminal/predict', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}

// ── SSE 流式聊天 ──

export interface StreamChatOptions {
  message: string
  conversationId: string
  modelId: string
  enableThinking: boolean
  systemPrompt?: string
  mode?: 'ssh'
  contextWindow?: number  // 历史消息条数上限，覆盖后端默认 20
  onContent: (token: string) => void
  onReasoning: (token: string) => void
  onError: (error: string) => void
  onDone: () => void
}

export function streamChat(options: StreamChatOptions): () => void {
  const abortController = new AbortController()

  const run = async () => {
    try {
      const url = `${getBaseUrl()}/chat/stream`
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const res = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          message: options.message,
          conversation_id: options.conversationId,
          model_id: options.modelId,
          enable_thinking: options.enableThinking,
          system_prompt: options.systemPrompt,
          mode: options.mode || 'ssh',
          context_window: options.contextWindow,
        }),
        signal: abortController.signal,
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        options.onError(err.detail || res.statusText)
        return
      }

      if (!res.body) {
        options.onError('Response body is empty')
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') {
                options.onDone()
                return
              }
              try {
                const parsed = JSON.parse(data)
                if (parsed.error) {
                  options.onError(parsed.error)
                  return
                }
                // H3 修复：回调异常单独捕获，避免被当作 JSON 解析错误吞掉
                try {
                  if (parsed.content) options.onContent(parsed.content)
                  if (parsed.reasoning) options.onReasoning(parsed.reasoning)
                } catch (e) {
                  console.error('[streamChat] 回调处理异常:', e)
                }
              } catch {
                // JSON.parse 失败：心跳或非 JSON 数据，忽略
              }
            }
          }
        }

        // stream ended without [DONE]
        options.onDone()
      } finally {
        // M5 修复：显式释放 reader，避免 abort 后底层流仍被占用导致资源泄漏
        try { reader.releaseLock() } catch { /* already released */ }
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== 'AbortError') {
        // H13 修复：用 toFriendlyMessage 转换网络错误，避免直接显示 "Failed to fetch"
        options.onError(toFriendlyMessage(e, 'Agent 运行失败，请稍后重试'))
      }
    }
  }

  run()
  return () => abortController.abort()
}

// ── Agent SSE 流式运行 ──

export interface AgentSSEEvent {
  type: 'state' | 'content' | 'reasoning' | 'tool_call_start' | 'tool_call_result' | 'warning' | 'error' | 'human_help_needed' | 'permission_needed' | 'intention' | 'done'
  state?: string
  iteration?: number
  content?: string
  reasoning?: string
  tool?: string
  args?: Record<string, unknown>
  result?: string
  success?: boolean
  message?: string
  iterations?: number
  state_history?: Array<{ from: string; event: string; to: string }>
  // human_help_needed 事件专用字段
  reason?: string
  // permission_needed 事件专用字段
  risk?: string
  reversible?: boolean
  // P7: done 事件携带的成本快照 + run_id
  cost?: AgentCostSnapshot
  run_id?: string
  // C3: done 事件携带的文件变更摘要
  file_changes?: AgentFileChangesSummary
  // H14: intention 事件携带的意图识别结果
  intention?: AgentIntentionInfo
}

export interface AgentCostSnapshot {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number
  tool_call_count: number
  llm_call_count: number
  duration_ms: number
  tool_duration_ms: number
  iterations: number
}

/** H14: 意图识别结果（后端 intention.py classify() 输出） */
export interface AgentIntentionInfo {
  level: 'trivial' | 'simple' | 'moderate' | 'complex' | 'debug'
  task_type: 'query' | 'implement' | 'modify' | 'refactor' | 'debug' | 'research' | 'conversation'
  reasoning_budget: number | null
  allow_planning_phase: boolean
  confidence: number
}

export interface AgentRunTrace {
  run: {
    id: string
    conversation_id: string
    task: string
    mode?: string
    model_used?: string
    status: string
    iteration: number
    max_iterations: number
    started_at: string
    completed_at?: string
  }
  cost: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    cost_usd: number
    duration_ms: number
    tool_call_count: number
    pricing: {
      input_per_1m: number
      output_per_1m: number
      model: string
    }
  }
  state_history: Array<{ from: string; event: string; to: string }>
  tool_calls: Array<{
    id: string
    iteration: number
    tool_name: string
    arguments?: string
    result?: string
    success: boolean
    duration_ms: number
    created_at: string
  }>
  // C3: 文件变更摘要 — done 事件 + trace API 均会返回
  file_changes?: AgentFileChangesSummary
}

// C3: 文件变更单条记录
export interface AgentFileChange {
  agent_run_id: string
  file_path: string
  abs_path: string
  operation: 'create' | 'modify' | 'delete' | string
  added_lines: number
  removed_lines: number
  before_size: number
  after_size: number
  created_at?: string
}

// C3: 文件变更汇总（由后端 get_file_changes_summary 返回）
export interface AgentFileChangesSummary {
  files_changed: number
  added_lines: number
  removed_lines: number
  changes: AgentFileChange[]
}

// C3: 单文件具体 diff（由后端 get_file_diff 返回）
export interface FileDiffResult {
  file_path: string
  abs_path: string
  before: string
  after: string
  unified_diff: string
  operation: 'create' | 'modify' | 'delete' | string
  added_lines: number
  removed_lines: number
}

/**
 * Agent 运行过程快照 — 用于固化到 ChatMessage.agentTrace 字段
 * 使历史会话切换后仍能显示该次运行的执行过程摘要
 */
export interface AgentTraceSnapshot {
  /** 最终状态：completed / failed */
  status: 'completed' | 'failed'
  /** 当前 Agent 状态机状态：thinking / acting / observing / completed / failed 等 */
  currentState: string
  /** 实际完成的迭代次数 */
  iterations: number
  /** 运行耗时（秒） */
  duration?: number
  /** 工具调用记录数组（结构与 agentStore.toolCalls 一致） */
  toolCalls: Array<{
    id: string
    tool: string
    args: Record<string, unknown>
    result?: string
    success?: boolean
    timestamp: number
    iteration?: number
  }>
  /** 思考过程文本（reasoning 累积） */
  reasoning: string
  /** 关联的 agent_run id（用于打开 TraceViewer） */
  lastRunId: string
  /** 文件变更摘要 */
  fileChanges?: AgentFileChangesSummary
  /** 成本快照 */
  costInfo?: AgentCostSnapshot
  /** 失败时的错误信息 */
  errorMessage?: string
}

export interface AgentCostStats {
  filter: { mode?: string | null; days: number }
  stats: {
    total_runs: number
    completed_runs: number
    failed_runs: number
    total_tokens: number
    prompt_tokens: number
    completion_tokens: number
    total_cost_usd: number
    total_duration_ms: number
    total_tool_calls: number
    avg_iterations: number
    avg_cost_usd: number
  }
}

export interface MemoryItem {
  id: string
  type: 'episodic' | 'semantic'
  mode: 'ssh'
  content: string
  source_conv_id: string | null
  agent_run_id: string | null
  importance: number
  created_at: string
  last_used_at: string
  use_count: number
}

/**
 * 工具元信息（含启用状态）
 */
export interface Tool {
  /** 工具名（如 read_file / shell_exec） */
  name: string
  /** 工具描述 */
  description: string
  /** 参数 JSON Schema */
  parameters: Record<string, unknown>
  /** 风险等级：safe / moderate / dangerous */
  risk_level: 'safe' | 'moderate' | 'dangerous'
  /** 适用模式列表 */
  mode_scope: string[]
  /** 是否启用（从 tools_config 表读取） */
  enabled: boolean
  /** 超时时间（秒） */
  timeout: number
  /** 结果可缓存 */
  cacheable: boolean
  /** 操作可逆 */
  reversible: boolean
}

export interface StreamAgentOptions {
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
  onEvent: (event: AgentSSEEvent) => void
  onError: (error: string) => void
  onDone: () => void
}

export function streamAgentRun(options: StreamAgentOptions): () => void {
  const abortController = new AbortController()

  const run = async () => {
    try {
      const url = `${getBaseUrl()}/agent/run`
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      const res = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          task: options.task,
          conversation_id: options.conversationId,
          model_id: options.modelId,
          mode: options.mode || 'ssh',
          system_prompt: options.systemPrompt,
          ssh_session_id: options.sshSessionId,
          enable_thinking: options.enableThinking ?? false,
          enable_tools: options.enableTools ?? true,
          max_iterations: options.maxIterations ?? 100,
          enable_dynamic_budget: options.enableDynamicBudget ?? true,
          skill_ids: options.skillIds || [],
        }),
        signal: abortController.signal,
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        options.onError(err.detail || res.statusText)
        return
      }

      if (!res.body) {
        options.onError('Response body is empty')
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') {
                options.onDone()
                return
              }
              try {
                const parsed = JSON.parse(data) as AgentSSEEvent
                // H3 修复：onEvent 异常单独捕获，避免被当作 heartbeat 解析错误吞掉
                try {
                  options.onEvent(parsed)
                } catch (e) {
                  console.error('[streamAgentRun] onEvent 处理异常:', e, 'event:', parsed)
                }
              } catch {
                // JSON.parse 失败：心跳或非 JSON 数据，忽略
              }
            }
          }
        }

        options.onDone()
      } finally {
        // M5 修复：显式释放 reader，避免 abort 后底层流仍被占用导致资源泄漏
        try { reader.releaseLock() } catch { /* already released */ }
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== 'AbortError') {
        // H13 修复：用 toFriendlyMessage 转换网络错误，避免直接显示 "Failed to fetch"
        options.onError(toFriendlyMessage(e, 'Agent 运行失败，请稍后重试'))
      }
    }
  }

  run()
  return () => abortController.abort()
}

// ── Agent API ──

export const agentApi = {
  listRuns: (conversationId?: string) =>
    request<{ runs: unknown[] }>(`/agent/runs${conversationId ? `?conversation_id=${conversationId}` : ''}`),

  getRunDetail: (runId: string) =>
    request<{ run: unknown; tool_calls: unknown[] }>(`/agent/runs/${runId}`),

  listTools: (mode: string = 'ssh') =>
    request<{ mode: string; tools: Tool[] }>(`/agent/tools?mode=${mode}`),

  updateToolEnabled: (toolName: string, enabled: boolean, mode: string = 'all') =>
    request<{ id: string; tool_name: string; mode: string; enabled: boolean }>(
      `/agent/tools/${encodeURIComponent(toolName)}/enabled?mode=${mode}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      }
    ),

  // P7: Trace Viewer + 成本统计
  getRunTrace: (runId: string) =>
    request<AgentRunTrace>(`/agent/runs/${runId}/trace`),

  /** 获取某次运行中某文件的具体 unified diff */
  getRunFileDiff: (runId: string, absPath: string) =>
    request<{ ok: boolean; diff?: FileDiffResult; message?: string }>(
      `/agent/runs/${runId}/diff?abs_path=${encodeURIComponent(absPath)}`
    ),

  /** 撤销某次 Agent 运行的所有文件变更 */
  undoRunChanges: (runId: string) =>
    request<{ undone: boolean; restored: string[]; deleted: string[]; failed: string[]; reason?: string }>(
      `/agent/runs/${runId}/undo`,
      { method: 'POST' }
    ),

  /** 保留某次 Agent 运行的所有文件变更 */
  keepRunChanges: (runId: string) =>
    request<{ kept: boolean; run_id: string }>(
      `/agent/runs/${runId}/keep`,
      { method: 'POST' }
    ),

  getCostStats: (mode?: string, days: number = 7) =>
    request<AgentCostStats>(`/agent/stats?${mode ? `mode=${mode}&` : ''}days=${days}`),

  // 记忆设置
  getMemorySettings: () =>
    request<{ enabled: boolean; strategy: string; max_items: number }>('/settings/memory'),

  updateMemorySettings: (data: { enabled: boolean; strategy: string; max_items: number }) =>
    request<{ enabled: boolean; strategy: string; max_items: number }>('/settings/memory', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  // 记忆管理
  listMemories: (mode?: string, memType?: string) =>
    request<{ memories: MemoryItem[]; count: number }>(
      `/agent/memories?${mode ? `mode=${mode}&` : ''}${memType ? `mem_type=${memType}` : ''}`
    ),

  deleteMemory: (memoryId: string) =>
    request<{ deleted: boolean; id: string }>(`/agent/memories/${memoryId}`, {
      method: 'DELETE',
    }),

  pruneMemories: (mode?: string, memType?: string, maxCount?: number, clearAll?: boolean) => {
    const params = new URLSearchParams()
    if (mode) params.set('mode', mode)
    if (memType) params.set('mem_type', memType)
    if (clearAll) {
      params.set('clear_all', 'true')
    } else {
      params.set('max_count', String(maxCount ?? 200))
    }
    return request<{ deleted: number }>(`/agent/memories/prune?${params}`, {
      method: 'POST',
    })
  },

  // 策略管理
  listStrategies: (mode?: string, strategyType?: string) =>
    request<{ strategies: unknown[]; count: number }>(
      `/agent/strategies?${mode ? `mode=${mode}&` : ''}${strategyType ? `strategy_type=${strategyType}` : ''}`
    ),

  triggerEvolution: (mode: string = 'ssh') =>
    request<{ evolved: boolean; config?: unknown; adjustments: string[] }>(
      `/agent/evolve?mode=${mode}`,
      { method: 'POST' }
    ),

  // 意识 tick
  triggerTick: () =>
    request<{ tick: unknown }>(`/agent/tick`, { method: 'POST' }),

  getTickStatus: () =>
    request<{ running: boolean; interval_seconds: number; last_tick: unknown }>(`/agent/tick/status`),

  // 权限管理
  grantPermission: (tool: string, scope: 'always' | 'session' = 'always') =>
    request<{ tool: string; scope: string; granted: boolean }>(`/agent/permission/grant`, {
      method: 'POST',
      body: JSON.stringify({ tool, scope }),
    }),

  revokePermission: (tool: string) =>
    request<{ tool: string; granted: boolean }>(`/agent/permission/revoke?tool=${encodeURIComponent(tool)}`, {
      method: 'POST',
    }),

  listPermissions: () =>
    request<{ tools: string[] }>(`/agent/permission/list`),
}
