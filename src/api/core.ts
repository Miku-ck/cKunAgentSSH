/** 会话/模型/设置域：REST 客户端 api 对象与各资源类型（ARCH-008 从 client.ts 拆出） */

import { request } from './base'
import { AgentTraceSnapshot } from './sse'
import { PermissionPolicy } from './agent'

// ── 会话 API ──

export interface Conversation {
  id: string
  title: string
  // S9-12：原声明含 'failed'，但 conversations.status 全仓只有两个写入点
  // （routes/agent.py 标 in_progress / completed，database.py 的 stale 复位也只写
  // completed），没有生产者。留着会让下一个读者写出永不成立的分支。
  status: 'in_progress' | 'completed'
  mode?: string
  created_at: string
  updated_at: string
  /** 后端 list_conversations 的相关子查询产物；渲染层当前无读者。 */
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
  reasoning_tokens?: number
  top_p?: number
  sort_order?: number
  created_at?: string
  updated_at?: string
}

/** 默认模型档位：与后端 routes/settings.py 的 DefaultMode 逐字对应 */
export type ModelDefaultMode = 'ssh' | 'global'

export interface ModelConfigPayload {
  name: string
  base_url: string
  api_key?: string
  model_id?: string
  provider?: string
  // supports_thinking / supports_image / supports_tool_calling 不在写入体里：
  // 三列运行时无消费者，面板也不再提供勾选（给了开关就等于承诺它会生效）。
  // 读侧仍回传（ModelConfig），卡片徽标按库里的历史值显示。
  temperature?: number
  top_p?: number
  max_tokens?: number
  max_output_tokens?: number
  reasoning_tokens?: number
  is_default_ssh?: boolean
}

/** GET /settings/http 的回读形状：proxy 只有脱敏形态，且反映的是"生效"策略（含 env 兜底） */
export interface HttpSettings {
  proxy_url_masked: string
  proxy_url_set: boolean
  proxy_source: 'db' | 'env' | ''
  timeout_seconds: number
  retry_count: number
}

export interface HttpSettingsPayload {
  /**
   * 三字段都是"传了才改"：代理明文只单向写入、读回永远脱敏，所以只改超时/重试时
   * 不能把代理字段一起带上 —— 带空串等于清除，带脱敏值会把口令抹掉。
   */
  proxy_url?: string
  /** 0 = 不覆盖，各调用点沿用自己的原默认超时 */
  timeout_seconds?: number
  retry_count?: number
}

export type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical'

export interface LogLevelSettings {
  level: LogLevel
  levels: LogLevel[]
}

/** GET /settings/diagnostics —— 只读快照，密钥/代理已在后端脱敏 */
export interface DiagnosticsSnapshot {
  backend: { version: string; python: string; platform: string; pid: number }
  database: { path: string; size_bytes: number | null; counts: Record<string, number> }
  logging: LogLevelSettings
  http: {
    proxy: string
    proxy_set: boolean
    timeout_seconds: number
    retry_count: number
  }
  tick: { running: boolean; last_result: Record<string, unknown> | null }
  bridge: { connected: boolean; present: boolean }
  permissions: { ttl_hours: number; permanent_entries: number | null }
  app_settings: Record<string, string>
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

// ── 会话工作流（Spec/Plan/Goal）──
// 三阶段工作流：spec（规格）→ plan（计划）→ goal（成功标准/执行锚），产物为
// 会话目录下 spec.md / plan.md / goal.md，内容只由 agent 用 spec_write/plan_write/
// goal_write 工具维护。前端唯一的写操作是删除（「目标」chip 的 ×）：一个只能
// 开启、无法关闭的开关等于没有开关。
export interface WorkflowSnapshot {
  /** 当前阶段：''(无) / spec / plan / goal */
  phase: string
  spec: string
  plan: string
  goal: string
}

/** Spec/Plan/Goal 工作流 API —— 对接 backend/routes/workflow.py */
export const workflowApi = {
  /** 读取某会话工作流阶段与三份工件；无会话/无工件返回空字符串字段（不是 404） */
  read: (conversationId: string) => {
    const q = new URLSearchParams()
    if (conversationId) q.set('conversation_id', conversationId)
    return request<WorkflowSnapshot>(`/workflow?${q.toString()}`)
  },

  /**
   * 删除某个工件（幂等：本就不存在也返回成功）。
   * 返回删除后的 phase —— 阶段取已存在的最高阶，删掉 goal 会回落到 plan，
   * 调用方据此提示用户连带后果，别让一次点击看起来"什么都没发生却被拦截了"。
   */
  clearArtifact: (conversationId: string, kind: 'spec' | 'plan' | 'goal') => {
    const q = new URLSearchParams({ conversation_id: conversationId, kind })
    return request<{ deleted: boolean; phase: string }>(`/workflow/artifact?${q.toString()}`, {
      method: 'DELETE',
    })
  },
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

  // 消息
  getMessages: (conversationId: string, limit = 50, offset = 0) =>
    request<Message[]>(`/chat/conversations/${conversationId}/messages?limit=${limit}&offset=${offset}`),

  // 对话附件上传（文本类文件，返回内容供注入 LLM 上下文）
  uploadChatFile: (conversationId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('conversation_id', conversationId || '')
    return request<ChatAttachment>('/chat/upload', {
      method: 'POST',
      body: fd,
    })
  },

  // 模型配置管理（聊天下拉与设置面板共用同一数据源 GET /settings/models）
  getModelConfigs: () =>
    request<ModelConfig[]>('/settings/models'),

  getDefaultModel: () =>
    request<{ id: string; name?: string; model_id?: string }>('/settings/models/default'),

  // model_id 在 query（不是 path）—— 顺序敏感：FastAPI 里 default 那条必须
  // 注册在 /settings/models/{config_id} 之前，别改成 path 参数
  // mode：'ssh' 写 is_default_ssh（对话/终端/定时任务档），'global' 写 is_default
  // （SSH 档未设时后处理与 terminal 兜底链读的档）。不传 = 'ssh'，与 A1 控件同口径。
  setDefaultModel: (modelId: string, mode: ModelDefaultMode = 'ssh') => {
    const q = new URLSearchParams({ model_id: modelId, mode })
    return request<{ default: string; mode: ModelDefaultMode }>(`/settings/models/default?${q.toString()}`, {
      method: 'PATCH',
    })
  },

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

  // 与 /settings/memory 同款：后端 PUT 是 exclude_none 的部分更新，只发改过的字段
  updatePostProcessSettings: (payload: Partial<{
    enabled: boolean
    generate_title: boolean
    extract_memory: boolean
    generate_skill: boolean
  }>) =>
    request<{ enabled: boolean; generate_title: boolean; extract_memory: boolean; generate_skill: boolean }>(
      '/settings/post-process',
      {
        method: 'PUT',
        body: JSON.stringify(payload),
      },
    ),

  // 会话保留清理（用户数据，默认关闭）— 开启后按 keep_count 清理最旧的已结束会话
  getConvRetention: () =>
    request<{ enabled: boolean; keep_count: number }>('/settings/conv-retention'),

  // 收 Partial：后端两字段都是 Optional（「不传 = 不改」），整包提交会在读失败时
  // 把库里用户调过的另一项按本地默认抹掉（这条设置是会真删会话的）
  updateConvRetention: (payload: Partial<{ enabled: boolean; keep_count: number }>) =>
    request<{ enabled: boolean; keep_count: number }>(
      '/settings/conv-retention',
      {
        method: 'PUT',
        body: JSON.stringify(payload),
      },
    ),

  // Web 搜索工具密钥（Tavily）— 后端掩码回显，明文只在写入时单向传输
  getWebToolsSettings: () =>
    request<{
      tavily_api_key_set: boolean
      tavily_api_key_masked: string
      tavily_api_key_source: 'env' | 'db' | ''
    }>('/settings/web-tools'),

  updateWebToolsSettings: (payload: { tavily_api_key: string }) =>
    request<{
      tavily_api_key_set: boolean
      tavily_api_key_masked: string
      tavily_api_key_source: 'env' | 'db' | ''
    }>('/settings/web-tools', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  // E3：审批授权策略（写入后端即时对运行时生效，无需重启）。
  // payload 字段全可选 =「不传 = 不改」，与后端 PermissionPolicyRequest 同契约。
  getPermissionPolicy: () =>
    request<PermissionPolicy>('/settings/permission-policy'),

  updatePermissionPolicy: (payload: Partial<PermissionPolicy>) =>
    request<PermissionPolicy>('/settings/permission-policy', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  // 运维策略：出站代理 / 超时 / 重试 / 日志级别 / 诊断快照

  getHttpSettings: () => request<HttpSettings>('/settings/http'),

  /**
   * PUT /settings/http — 代理只在写入时单向传明文，读回永远脱敏。
   * 后端返回的 clients_rebuilt 是这次被丢弃重建的 LLM 客户端数（代理改完仍直连
   * 是因为连接池在建 client 时固化，不重建要等重启）。
   */
  updateHttpSettings: (payload: HttpSettingsPayload) =>
    request<HttpSettings & { clients_rebuilt: number }>('/settings/http', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  getLogLevel: () => request<LogLevelSettings>('/settings/log-level'),

  updateLogLevel: (level: LogLevel) =>
    request<LogLevelSettings>('/settings/log-level', {
      method: 'PUT',
      body: JSON.stringify({ level }),
    }),

  getDiagnostics: () => request<DiagnosticsSnapshot>('/settings/diagnostics'),

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

  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) =>
    request<{ id: string; enabled: boolean }>(`/skills/${id}/enabled${mode ? `?mode=${mode}` : ''}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),

  deleteSkill: (id: string) =>
    request<{ deleted: boolean }>(`/skills/${id}`, {
      method: 'DELETE',
    }),

  /** 触发包内内置技能同步（backend/skills_builtin，升级后刷新技能内容） */
  syncPackagedSkills: () =>
    request<{ upserted: number; slugs: string[] }>('/skills/sync-packaged', {
      method: 'POST',
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

export interface ChatAttachment {
  file_id: string
  filename: string
  size?: number
  content: string
  truncated?: boolean
}

