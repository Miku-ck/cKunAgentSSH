/**
 * 运维剧本（Runbook）域 API — 六方法，形状镜像 scheduleApi（src/api/agent.ts:169）。
 *
 * 不经 client.ts 桶导出（sharedWiring W4 由集成者定夺），消费方直接 import 本模块。
 * 无 SSE：剧本执行是无头 run，无流可看，前端走 runNow 后 3s 短轮询 + 激活期
 * 30s 静默轮询（SchedulesView 同节奏）。
 */
import { request } from '@/api/base'

export interface RunbookStep {
  command: string
  assertCommand?: string
  note?: string
}

export interface RunbookRecord {
  id: string
  name: string
  description: string
  target_host: string
  steps: RunbookStep[]
  enabled: number
  created_at: string
  updated_at: string
  /** 最近一次执行的状态（runbook_runs 联查；无历史时为 null） */
  last_status?: string | null
  last_run_at?: string | null
  last_run_id?: string | null
}

export interface RunbookStepState {
  seq: number
  status: 'success' | 'failed' | 'blocked' | 'skipped' | 'pending'
  command: string
  assertCommand: string
  verdict: string
  run_id: string
  error: string
}

export interface RunbookRunRecord {
  id: string
  runbook_id: string
  /** 最后一个无头 run 的 run_id（audit_log 按 run_id 贯通查询） */
  run_id: string
  status: string
  current_step: number
  steps_state: RunbookStepState[]
  /** 失败后自动诊断 run 的结论 */
  diagnosis: string
  error: string
  started_at?: string
  finished_at?: string
  created_at: string
}

export interface RunbookIn {
  name: string
  description?: string
  target_host?: string
  steps: RunbookStep[]
  enabled?: boolean
}

/** 历史条数夹取：与后端 GET /runbooks/{id}/runs 的 max(1, min(limit, 200)) 同口径 */
export function clampRunsLimit(limit?: number): number {
  const n = typeof limit === 'number' && Number.isFinite(limit) ? Math.floor(limit) : 50
  return Math.max(1, Math.min(n, 200))
}

export const runbookApi = {
  /** 剧本列表（含 last_run 摘要） */
  list: () =>
    request<RunbookRecord[]>('/runbooks'),

  /** 新建剧本（步骤数 ≤20、command 必填 ≤2000 字由后端校验） */
  create: (payload: RunbookIn) =>
    request<RunbookRecord>('/runbooks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 更新剧本（PATCH 局部语义：不发 = 不改；steps 传数组即整体替换） */
  update: (id: string, payload: Partial<Omit<RunbookIn, 'name'>> & { name?: string }) =>
    request<RunbookRecord>(`/runbooks/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  /** 删除剧本（执行中 409；历史由 FK 级联清理） */
  remove: (id: string) =>
    request<{ ok: boolean }>(`/runbooks/${id}`, {
      method: 'DELETE',
    }),

  /** 一键执行。触发即返回 runbook_run_id；桥不可达 503 / 执行中 409 */
  runNow: (id: string) =>
    request<{ ok: boolean; runbook_run_id: string }>(`/runbooks/${id}/run`, {
      method: 'POST',
    }),

  /** 执行历史（steps_state 全量返回；limit 夹取到 1-200） */
  runs: (id: string, limit?: number) =>
    request<RunbookRunRecord[]>(`/runbooks/${id}/runs?limit=${clampRunsLimit(limit)}`),

  /** 清空执行历史（保留剧本本身）；执行中有行则后端 409 拒 */
  clearRuns: (id: string) =>
    request<{ ok: boolean; deleted: number }>(`/runbooks/${id}/runs`, {
      method: 'DELETE',
    }),
}
