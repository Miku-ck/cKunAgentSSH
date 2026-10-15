/** 审计域：auditApi 与审计记录类型（原 client.ts 645-731 行，ARCH-008 拆分） */

import { backendErrorDetail, ensureApiToken, getBaseUrl, request, withApiToken } from './base'

// ── 审计 API（等保2.0 安全审计）──
export interface AuditRecord {
  id: number
  ts: string
  operator: string
  target_type: string
  target_id: string
  environment: string
  action: string
  command_hash: string
  command_raw: string
  result: string
  conclusion: string // executed | auto_allowed | approved | blocked | denied
  run_id: string
  iteration: number
  prev_hash: string
  hash: string
  created_at: string
}

export interface AuditVerifyResult {
  intact: boolean
  total: number
  checked: number
  broken: number[]
  // 原先还有个 error?: string —— 后端把读失败折成 {intact:false, error} 时才发它，
  // 而审计页从不渲染这一字段（只显示「链被篡改!」）。现在读失败直接 500，
  // 后端不再发这个键，留着就是一条幽灵字段（S1-06 同族）。
}

export const auditApi = {
  /** 按条件查询审计记录 */
  query: (params: {
    environment?: string
    target_id?: string
    action?: string
    conclusion?: string
    run_id?: string
    limit?: number
    offset?: number
  } = {}) => {
    const qs = new URLSearchParams()
    if (params.environment) qs.set('environment', params.environment)
    if (params.target_id) qs.set('target_id', params.target_id)
    if (params.action) qs.set('action', params.action)
    if (params.conclusion) qs.set('conclusion', params.conclusion)
    if (params.run_id) qs.set('run_id', params.run_id)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset !== undefined) qs.set('offset', String(params.offset))
    return request<{ items: AuditRecord[]; total: number }>(`/audit/records?${qs.toString()}`)
  },

  /** 全链完整性校验 */
  verify: () =>
    request<AuditVerifyResult>('/audit/verify'),

  /** 归档式清理：全量 CSV 归档落盘后清空，写入封缄记录开新链（非裸删除） */
  clearArchived: () =>
    request<{ archived: number; file: string; last_hash: string; seal_hash: string }>(
      '/audit/clear', { method: 'POST' },
    ),

  /** 导出全部审计为 CSV */
  exportCsv: async () => {
    await ensureApiToken()
    const url = `${getBaseUrl()}/audit/export`
    const res = await fetch(url, {
      headers: withApiToken({}),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(backendErrorDetail(err) || res.statusText)
    }
    const blob = await res.blob()
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = 'audit_log.csv'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    // 延后到宏任务再释放：click() 只是发起下载，同步 revoke 会让浏览器来不及
    // 认领该 blob URL → 导出 0 字节或无文件（Chromium 明确告警的用法）。
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  },
}

