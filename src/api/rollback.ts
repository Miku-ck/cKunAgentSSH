/**
 * rollbackApi — run 级一键回滚（功能 B）
 *
 * 三个后端端点都在 backend/routes/rollbacks.py（/api 前缀由 main.py 统一挂）：
 * - GET  /rollback/runs/{id}/undo-actions — undo 明细（后端保证不含 snapshot_b64）
 * - POST /rollback/plan                   — 组装回滚任务文本（只组装不执行；
 *           执行走既有 agentStore.runAgent → POST /agent/run 交互式全链）
 * - POST /rollback/register               — 登记源 run → 回滚 run 关联
 */
import { request } from './base'

/** 逐条 undo 动作（与后端 run_undo_actions 行投影一致，永无 snapshot_b64） */
export interface UndoActionItem {
  seq: number
  kind: 'shell' | 'file' | 'noop' | string
  tool: string
  command: string
  reverse_command: string | null
  path: string | null
  existed: boolean | null
  /** snapshot（内容级还原）> derived（派生反向命令）> delete_created（反向是删除）> manual（不进回滚计划） */
  confidence: 'snapshot' | 'derived' | 'delete_created' | 'manual' | string
  /** 后端按形态派生的警示语；空串 = 无 */
  warning: string
  status: 'pending' | 'auto_rolled_back' | 'rolled_back' | string
  host: string
  environment: string
}

export interface UndoActionsResponse {
  run_id: string
  run_status: string
  terminal: boolean
  items: UndoActionItem[]
}

export interface RollbackPlanResponse {
  source_run_id: string
  /** 回滚消息落原会话（对用户可见）；前端 runAgent 的 conversationId 必须取它 */
  conversation_id: string
  task_text: string
  host: string
  environment: string
  /** 源 run 的 SSH 会话 id（临时号、大概率已失效）：仅提示用，不做自动重连 */
  ssh_session_hint: string
  items: UndoActionItem[]
}

export interface RollbackRegisterResponse {
  id: string
  source_run_id: string
  rollback_run_id: string
  status: string
  created_at?: string
}

export const rollbackApi = {
  /** 某 run 的 undo 明细；run 不存在或无 undo 记录时后端 404 */
  getUndoActions: (runId: string) =>
    request<UndoActionsResponse>(
      `/rollback/runs/${encodeURIComponent(runId)}/undo-actions`,
    ),

  /** 组装回滚任务文本（服务端只组装，不执行） */
  plan: (sourceRunId: string) =>
    request<RollbackPlanResponse>('/rollback/plan', {
      method: 'POST',
      body: JSON.stringify({ source_run_id: sourceRunId }),
    }),

  /** 登记源 run → 回滚 run 关联（幂等；undo_restore 归属校验的数据前提） */
  register: (sourceRunId: string, rollbackRunId: string) =>
    request<RollbackRegisterResponse>('/rollback/register', {
      method: 'POST',
      body: JSON.stringify({
        source_run_id: sourceRunId,
        rollback_run_id: rollbackRunId,
      }),
    }),
}
