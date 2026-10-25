/** Agent 域：agentApi / scheduleApi / agentAdminApi 及各自类型（原 client.ts 1067-1589 行，ARCH-008 拆分） */

import { request } from './base'
import type { StructureResponse } from '@/utils/task-structure-core'

// ── Agent API ──

/** /agent/safety/classify 单条结果 —— level 与后端 classify_command 同枚举 */
export interface SafetyClassifyResult {
  command: string
  level: 'hardline' | 'dangerous' | 'safe'
  reason: string
  normalized: string
  explain: string
}

export interface MemorySettings {
  enabled: boolean
  max_items: number
  inject_budget_tokens: number
  blocks_budget_tokens: number
  search_timeout_s: number
  reconcile_llm_enabled: boolean
}
export const agentApi = {
  // 记忆设置 —— 后端 MemorySettingsRequest 全字段 Optional：不传 = 不改。
  // 因此这里收 Partial：store 只把真正改过的字段发出去（settings.ts 的 createBackendSync）。
  // 曾经按整份对象提交，于是"启动 GET 没成功 + 用户拨一下开关"会把前端从未读到过的
  // 字段按本地默认写回库，静默覆盖后端真值。响应仍按整份读回，用于收敛本地。
  getMemorySettings: () =>
    request<MemorySettings>('/settings/memory'),

  updateMemorySettings: (data: Partial<MemorySettings>) =>
    request<MemorySettings>('/settings/memory', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  // 权限管理
  // 审查 M-3：默认 scope 'once'（仅放行下一次调用），原默认 'always' 一次点击即永久免审批；
  // command 用于 shell_exec 的命令级绑定（只批准被展示的那条命令）
  // runId/conversationId：once 授权按 run 键控、session 按会话键控（后端 PermissionGate 隔离）
  grantPermission: (tool: string, scope: 'once' | 'session' | 'always' = 'once', command?: string,
                    runId?: string, conversationId?: string) =>
    request<{ tool: string; scope: string; grant_key: string; granted: boolean }>(`/agent/permission/grant`, {
      method: 'POST',
      body: JSON.stringify({
        tool, scope,
        ...(command ? { command } : {}),
        ...(runId ? { run_id: runId } : {}),
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    }),

  /** 清理某次 run 的全部 once 授权（前端显式终止或超时时主动释放） */
  releaseRunPermissions: (runId: string) =>
    request<ReleaseRunResponse>(
      `/agent/permission/release-run/${encodeURIComponent(runId)}`,
      { method: 'POST' },
    ),

  /** 标记某 run 下某命令被用户拒绝（visible_terminal 拒绝后调用，后续不再弹卡片） */
  denyCommand: (tool: string, command: string, runId: string) =>
    request<{ tool: string; command: string; run_id: string; denied_key: string }>(
      '/agent/permission/deny-command',
      { method: 'POST', body: JSON.stringify({ tool, command, run_id: runId }) },
    ),

  /**
   * 命令风险分类（批量下发等人工通道的唯一判定源）。
   * 后端 classify_command 与 shell_exec 完全同口径；调用方失败时必须拒绝执行（fail-closed）。
   */
  classifyCommands: (commands: string[]) =>
    request<{ results: SafetyClassifyResult[] }>('/agent/safety/classify', {
      method: 'POST',
      body: JSON.stringify({ commands }),
    }),

  /**
   * 长任务结构化（spec 2026-10-24-task-structuring）：把一段较长的模糊运维需求
   * 交给后端纯推理拆成结构。这是发送前的**可选旁路**，非发送前置：调用失败由调用方
   * 降级、原文照发，绝不因为结构化挂了就把用户卡住发不出消息。
   *
   * active_target.host 是当前选定的落点主机，后端据此硬锁主机字段（模型不得臆测）；
   * allowed_hosts 传已连接主机白名单，出现集合外主机字面量会被标为需确认。
   */
  structureTask: (payload: {
    raw_text: string
    active_target: { host: string; session_type: 'local' | 'remote' }
    environment: 'test' | 'prod'
    posture?: 'goal' | 'plan' | null
    allowed_hosts?: string[]
    model_id?: string
  }) =>
    request<StructureResponse>('/agent/structure', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** Git 提交信息生成（纯推理，无工具）：仓库 / 分支 / 变更清单（带状态前缀）+ 可选 diff 摘要 → 单行提交信息 */
  generateCommitMessage: (data: { repo: string; branch: string; files: string[]; diff?: string }) =>
    request<{ message: string }>('/agent/commit-message', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // ── 澄清向导 ──
  /** 提交用户答案（前端澄清向导"完成"按钮调用） */
  submitClarification: (runId: string, answers: Record<string, { choice: string; custom_text?: string }>) =>
    request<{ run_id: string; status: string; steps: number }>(
      '/agent/clarification/answer',
      { method: 'POST', body: JSON.stringify({ run_id: runId, answers }) },
    ),

  /** 跳过所有剩余澄清步（前端"跳过"按钮调用） */
  skipClarification: (runId: string) =>
    request<{ run_id: string; status: string }>(
      '/agent/clarification/skip',
      { method: 'POST', body: JSON.stringify({ run_id: runId, answers: {} }) },
    ),
}

// ── 定时任务调度 API ──
export interface ScheduleRecord {
  id: string
  name: string
  task_prompt: string
  target_host: string
  schedule_type: 'interval' | 'daily' | 'weekly' | 'monthly'
  interval_minutes: number
  time_hour: number
  time_minute: number
  weekday: number
  day_of_month: number
  enabled: number
  next_run_at?: string
  last_run_at?: string
  last_status: string
  last_run_id: string
  created_at: string
  updated_at?: string
}

export interface ScheduleRun {
  id: string
  schedule_id: string
  run_id: string
  status: string
  summary: string
  error: string
  started_at: string
  finished_at?: string
  created_at: string
}

export interface ScheduleIn {
  name: string
  task_prompt?: string
  target_host?: string
  schedule_type?: 'interval' | 'daily' | 'weekly' | 'monthly'
  interval_minutes?: number
  time_hour?: number
  time_minute?: number
  weekday?: number
  day_of_month?: number
  enabled?: boolean
}

export const scheduleApi = {
  /** 任务列表 */
  list: () =>
    request<ScheduleRecord[]>('/schedules'),

  /** 创建定时巡检任务 */
  create: (payload: ScheduleIn) =>
    request<ScheduleRecord>('/schedules', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 更新任务（名称/提示词/目标/周期/启停） */
  update: (id: string, payload: Partial<Omit<ScheduleIn, 'name'>> & { name?: string }) =>
    request<ScheduleRecord>(`/schedules/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  /** 删除任务 */
  remove: (id: string) =>
    request<{ ok: boolean }>(`/schedules/${id}`, {
      method: 'DELETE',
    }),

  /**
   * 立即巡检。后端走 ScheduleEngine.trigger(manual=True)：不推进 next_run_at，
   * 手动执行不消耗本次定时；若执行期间恰好到期，会在结束后的下一个 tick（≤30s）
   * 补跑该次定时。
   */
  runNow: (id: string) =>
    request<{ ok: boolean; schedule_id: string }>(`/schedules/${id}/run`, {
      method: 'POST',
    }),

  /** 执行历史 */
  runs: (id: string, limit = 50) =>
    request<ScheduleRun[]>(`/schedules/${id}/runs?limit=${limit}`),
}

// ── Agent 管理端（对接 backend/routes/agent.py 的管理端点）──
// 说明：这些端点后端早已闭环、此前前端零引用；此处仅接线消费，不改后端行为。

/** GET /agent/tools 单工具元信息（含启用状态） */
export interface AgentToolInfo {
  name: string
  description: string
  risk_level: 'safe' | 'moderate' | 'dangerous'
  enabled: boolean
  reversible: boolean
  cacheable: boolean
  timeout: number
}
export interface AgentToolsResponse { mode: string; tools: AgentToolInfo[] }

/** GET /agent/permission/list 永久授权键集合（"tool" 或 "tool:cmdhash"） */
export interface PermissionListResponse {
  /** 落盘的原始条目串：主机限域条目形如 `@host|tool:cmdhash`，无前缀 = 全局。
   *  保留给只认裸键的旧消费方；新代码用 entries（主机段已拆开）。 */
  tools: string[]
  /** E3：逐条授予/到期时间（epoch 秒）。expires_at=null = 该条永不过期。
   *  host 为空串 = 全局条目；entry 是落盘的原始行。 */
  entries?: {
    key: string
    host: string
    entry: string
    granted_at: number | null
    expires_at: number | null
  }[]
  /** 当前生效的保留时长（小时）；0 = 永久 */
  ttl_hours?: number
}

/** GET/PUT /settings/permission-policy — 审批授权策略（E3 + 审批策略新字段） */
export interface PermissionPolicy {
  /** 永久授权保留时长（小时）；0 = 永不过期（默认，即引入 TTL 前的行为） */
  allowlist_ttl_hours: number
  /** 自动审批设置侧开关；env CKUN_AGENT_DISABLE_AUTO_APPROVE=1 优先级更高 */
  auto_approve_enabled: boolean
  /** 审批兜底等待秒数；0 = 永久等待（卡片不过期），默认 600 */
  approval_fallback_s: number
}

/** POST /agent/permission/release-run/{run_id} — 清理某次 run 的全部 once 授权 */
export interface ReleaseRunResponse { run_id: string; released: number }

/** v2 记忆行（embedding 后端不出，仅 has_embedding 标记；SELECT * 投影） */
export interface MemoryRow {
  id: string
  kind: 'semantic' | 'episodic' | 'procedural' | string
  scope: string
  title: string
  content: string
  source: string
  status: 'active' | 'pending_reconcile' | 'invalidated' | 'superseded' | 'rejected' | string
  importance: number | null
  pinned: number
  use_count: number
  valid_at?: string | null
  invalid_at?: string | null
  superseded_by?: string | null
  last_used_at?: string | null
  embedding_src?: string | null
  has_embedding: boolean
  source_conv_id?: string | null
  agent_run_id?: string | null
  created_at: string
  updated_at: string
}
export interface MemoriesResponse { memories: MemoryRow[]; count: number }

/** GET /agent/memories/stats：按 kind→status 计数 + job 队列水位 */
export interface MemoryStats {
  memories: Record<string, Record<string, number>>
  jobs: Record<string, number>
}

export interface MemoryJobRow {
  id: string
  kind: string
  status: string
  attempts: number
  last_error?: string | null
  priority: number
  created_at: string
}
export interface MemoryAuditRow {
  id: number
  memory_id: string
  action: string
  old_content?: string | null
  new_content?: string | null
  reason?: string | null
  job_id?: string | null
  created_at: string
}
export interface MemoryBlockRow {
  key: string
  value: string
  limit_chars: number
  version: number
  updated_by: string
  updated_at: string
}

/** 画像块历史版本（回滚即把其中的 value 再写一版，历史不删） */
export interface MemoryBlockHistoryRow {
  key: string
  version: number
  value: string
  updated_by: string
  created_at: string
}

/** POST /agent/memories —— 用户手动新增，后端直写 active 并置 pinned */
export interface MemoryCreatePayload {
  kind?: 'semantic' | 'episodic' | 'procedural'
  content: string
  title?: string
  scope?: string
  importance?: number
}

/** GET /agent/strategies 单条策略（reflection / evolved_config） */
export interface AgentStrategyItem {
  id: string
  mode: string
  strategy_type: string
  name: string
  content: string
  config?: string
  score: number
  enabled: number
  created_at: string
  updated_at: string
}
export interface StrategiesResponse { strategies: AgentStrategyItem[]; count: number }

/** GET /agent/tick/status 意识循环状态 */
export interface TickStatusResponse {
  running: boolean
  interval_seconds: number
  last_tick: Record<string, unknown> | null
}

/** GET /agent/runs/{id}/trace 完整轨迹（会话内「轨迹」按钮回放） */
export interface AgentRunTrace {
  run: {
    id: string | null
    conversation_id?: string
    task?: string
    mode?: string
    model_used?: string
    status?: string
    iteration?: number
    max_iterations?: number
    started_at?: string
    completed_at?: string
  }
  cost: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    cost_usd: number
    duration_ms: number
    tool_call_count: number
    pricing: { input_per_1m: number; output_per_1m: number; model: string }
  }
  state_history: Array<{ from?: string; to?: string; event?: string; at?: string }>
  tool_calls: Array<{
    id: string
    iteration: number
    tool_name: string
    arguments: Record<string, unknown> | string
    result: string | null
    /** null = 调用发起后结果从未回写（崩溃/中止），前端按「未回写」渲染 */
    success: boolean | null
    duration_ms: number | null
    created_at: string
  }>
}

/** GET /agent/runs 的列表行。
 *
 * 列集合就是后端那份投影（`database._RUN_LIST_COLUMNS`）：**没有**目标主机与审批档
 * —— `agent_runs` 表里没存这两样，界面凭空画一格就等于借"当前设置里的选择"顶上去，
 * 那是 run 归因那轮已经踩过的串台（运行中改设置会改写历史外观）。
 * 整段 JSON 的 `state_history` / `context_summary` 也不在列表里，详情走 getRunTrace。 */
export interface AgentRunRow {
  id: string
  conversation_id: string
  task: string | null
  status: string
  mode: string | null
  model_used: string | null
  iteration: number
  max_iterations: number
  total_tokens: number
  cost_usd: number
  duration_ms: number
  tool_call_count: number
  started_at: string
  completed_at: string | null
}

export interface AgentRunsResponse { items: AgentRunRow[]; total: number }

export const agentAdminApi = {
  /** 工具清单（含启用状态、风险档） */
  getTools: () =>
    request<AgentToolsResponse>('/agent/tools'),

  /** 启用/禁用工具 */
  setToolEnabled: (toolName: string, enabled: boolean) =>
    request<unknown>(
      `/agent/tools/${encodeURIComponent(toolName)}/enabled`,
      { method: 'PATCH', body: JSON.stringify({ enabled }) },
    ),

  /** 永久授权清单 */
  listPermissions: () => request<PermissionListResponse>('/agent/permission/list'),

  /** 手动登记永久授权（与审批弹窗「始终允许」同源：落 allowlist 文件 + 审计）。
   *  host：限定到该主机（落成 `@host|键`）；省略/空 = 全局条目（对所有主机生效）。 */
  allowPermission: (tool: string, command?: string, host?: string) =>
    request<{ tool: string; grant_key: string; host: string; granted: boolean }>(
      '/agent/permission/allow',
      {
        method: 'POST',
        body: JSON.stringify({
          tool,
          ...(command ? { command } : {}),
          ...(host ? { host } : {}),
        }),
      },
    ),

  /** 撤销授权：省略 command = 撤该工具全部授权；带 command = 只撤单条命令绑定。
   *  host 三态：不传 = 全局与所有主机全撤；'' = 只撤全局条目；'h' = 只撤该主机。 */
  revokePermission: (tool: string, command?: string, host?: string) =>
    request<{ tool: string; granted: boolean; host: string | null }>(
      `/agent/permission/revoke?tool=${encodeURIComponent(tool)}` +
      (command ? `&command=${encodeURIComponent(command)}` : '') +
      (host !== undefined ? `&host=${encodeURIComponent(host)}` : ''),
      { method: 'POST' },
    ),

  /** v2 记忆列表：kind/scope/status 精确过滤 + 内容/标题模糊搜 */
  listMemories: (opts: {
    kind?: string; scope?: string; status?: string; q?: string; limit?: number; offset?: number
  } = {}) => {
    const q = new URLSearchParams()
    if (opts.kind) q.set('kind', opts.kind)
    if (opts.scope) q.set('scope', opts.scope)
    if (opts.status) q.set('status', opts.status)
    if (opts.q) q.set('q', opts.q)
    q.set('limit', String(opts.limit ?? 100))
    if (opts.offset) q.set('offset', String(opts.offset))
    return request<MemoriesResponse>(`/agent/memories?${q.toString()}`)
  },

  memoryStats: () => request<MemoryStats>('/agent/memories/stats'),

  /**
   * 手动新增：直写 active + pinned（不参与池容量修剪），source 固定 user_ui。
   * 不带 embedding —— 关键词检索照常命中，列表里 has_embedding 显示 false。
   */
  addMemory: (payload: MemoryCreatePayload) =>
    request<{ created: boolean; id: string; memory: MemoryRow }>('/agent/memories', {
      method: 'POST', body: JSON.stringify(payload),
    }),

  /** 用户编辑：改内容（旧向量作废）与/或 pin */
  editMemory: (id: string, patch: { content?: string; title?: string; pinned?: boolean }) =>
    request<{ updated: boolean; memory: MemoryRow }>(
      `/agent/memories/${encodeURIComponent(id)}`,
      { method: 'PATCH', body: JSON.stringify(patch) },
    ),

  /** 删除 = invalidate（失效不物理删，审计链保留） */
  deleteMemory: (id: string) =>
    request<unknown>(`/agent/memories/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 清空：全部非 pinned 记忆 invalidate（pinned 永不被批量操作波及） */
  clearMemories: () =>
    request<{ invalidated: number }>('/agent/memories/clear', { method: 'POST' }),

  /** 单条记忆的审计链（可空 = 全局最近） */
  listMemoryAudit: (memoryId?: string, limit = 50) => {
    const q = new URLSearchParams({ limit: String(limit) })
    if (memoryId) q.set('memory_id', memoryId)
    return request<{ audit: MemoryAuditRow[] }>(`/agent/memories/audit?${q.toString()}`)
  },

  listMemoryJobs: (status?: string, limit = 50) => {
    const q = new URLSearchParams({ limit: String(limit) })
    if (status) q.set('status', status)
    return request<{ jobs: MemoryJobRow[] }>(`/agent/memories/jobs?${q.toString()}`)
  },

  retryMemoryJob: (jobId: string) =>
    request<unknown>(`/agent/memories/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' }),

  listMemoryBlocks: () => request<{ blocks: MemoryBlockRow[] }>('/agent/blocks'),

  /** 块历史（倒序，最新在前）——「回滚到此版」用其 value 调 putMemoryBlock */
  listMemoryBlockHistory: (key: string, limit = 20) =>
    request<{ history: MemoryBlockHistoryRow[] }>(
      `/agent/blocks/${encodeURIComponent(key)}/history?limit=${limit}`,
    ),

  /** UI 整块写入画像块（超限后端 400） */
  putMemoryBlock: (key: string, value: string) =>
    request<unknown>(`/agent/blocks/${encodeURIComponent(key)}`, {
      method: 'PUT', body: JSON.stringify({ value }),
    }),

  /** 策略列表（反思 + 演化） */
  listStrategies: (opts: { mode?: string; limit?: number } = {}) => {
    const q = new URLSearchParams()
    if (opts.mode) q.set('mode', opts.mode)
    q.set('limit', String(opts.limit ?? 50))
    return request<StrategiesResponse>(`/agent/strategies?${q.toString()}`)
  },

  setStrategyEnabled: (id: string, enabled: boolean) =>
    request<unknown>(
      `/agent/strategies/${encodeURIComponent(id)}/enabled?enabled=${enabled ? 1 : 0}`,
      { method: 'PATCH' },
    ),

  deleteStrategy: (id: string) =>
    request<unknown>(`/agent/strategies/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 意识 tick 状态（running / 间隔 / 上次结果） */
  getTickStatus: () => request<TickStatusResponse>('/agent/tick/status'),

  /**
   * 立即整理：手动跑一轮 tick（对账 + 池修剪 + 衰减），不等定时周期。
   * 后台调度不受影响；返回本轮 summary 与各步计数。
   */
  runMemoryTickNow: () => request<Record<string, unknown>>('/agent/tick/run', {
    method: 'POST',
  }),

  /** 按时间倒序列出 run（历史回看的列表）。
   *  runId 是 trace 号的**精确匹配**（后端 `id = ?`）—— 号是 12 位 hex，粘半个就是零条。 */
  listRuns: (params: {
    status?: string; runId?: string; conversationId?: string; limit?: number; offset?: number
  } = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.runId) qs.set('run_id', params.runId)
    if (params.conversationId) qs.set('conversation_id', params.conversationId)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    return request<AgentRunsResponse>(`/agent/runs?${qs.toString()}`)
  },

  /** 某次 run 的完整轨迹（DB 权威，含每调用耗时与成本） */
  getRunTrace: (runId: string) =>
    request<AgentRunTrace>(`/agent/runs/${encodeURIComponent(runId)}/trace`),
}

// ── Goal（实现目标）API 已移除 ──
// 旧 goalApi/goalsApi（目标 CRUD + goal_progress SSE）随 runbook 重构移除，
// 由 core.ts 的 workflowApi（Spec/Plan/Goal 只读，经 client.ts barrel 导出）取代。
