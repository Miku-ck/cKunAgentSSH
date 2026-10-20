/**
 * 渲染层人工通道的审计写入口 —— 批量下发 / Docker 变更动作。
 *
 * S4-07：这两条通道走 ssh:exec，Electron 主进程不写 audit_log，审计页上曾完全
 * 看不到。审计是事后证据，不是执行闸门：写失败只提示，不回滚已发生的远端动作。
 */

import { auditApi, type AuditAppendInput, type AuditConclusion } from '@/api/audit'
import { extractSSHError } from '@/ssh-error-types'

export const AUDIT_ACTION_BULK = 'bulk_ssh_exec'
export const AUDIT_ACTION_DOCKER = 'docker_action'

// 与后端 AuditAppendRequest 的字段上限逐字相等
export const AUDIT_COMMAND_MAX = 8192
export const AUDIT_RESULT_MAX = 2048
export const AUDIT_TARGET_MAX = 256

const TRUNCATION_MARKER = '…[truncated]'

/**
 * 按码点截断：中文与 emoji 不被劈开，口径与后端 Pydantic max_length（字符数）一致。
 * 触发截断时保留尾标记，让审计读者知道原文比记录长。
 */
export function clipAuditText(value: string, max: number): string {
  const chars = [...value]
  if (chars.length <= max) return value
  if (max <= TRUNCATION_MARKER.length) return chars.slice(0, max).join('')
  return chars.slice(0, max - TRUNCATION_MARKER.length).join('') + TRUNCATION_MARKER
}

export type RemoteAuditConclusion = Extract<AuditConclusion, 'executed' | 'failed'>

export interface RemoteAuditOutcome {
  conclusion: RemoteAuditConclusion
  result: string
}

function isExecTimeout(err: unknown): boolean {
  const transport = extractSSHError(err)
  if (transport) return transport.code === 'EXEC_TIMEOUT'
  // DockerView 抛的是 describeThrownDockerError 的降阶对象（只剩 code/message），
  // 原始 transport 已丢，超时只能从 code 认。
  return !!err && typeof err === 'object'
    && (err as { code?: unknown }).code === 'EXEC_TIMEOUT'
}

/**
 * 下发异常 → 审计结论。
 *
 * EXEC_TIMEOUT 记 executed 而不是 failed：命令可能已经在远端落地，只是回包没等到。
 * 记 failed 会诱导「再执行一次」，也等于让审计链对一件不确定的事下确定结论。
 */
export function auditOutcomeForDispatchError(
  err: unknown,
  friendlyMessage: string,
  timeoutSec?: number,
): RemoteAuditOutcome {
  if (isExecTimeout(err)) {
    return {
      conclusion: 'executed',
      result: timeoutSec
        ? `timeout=${timeoutSec}s remote outcome unknown`
        : 'timeout remote outcome unknown',
    }
  }
  const code = extractSSHError(err)?.code
  return {
    conclusion: 'failed',
    result: code ? `error[${code}]=${friendlyMessage}` : `error=${friendlyMessage}`,
  }
}

/** 已回包（含非零退出码）的结果摘要；exitCode 缺失如实写 null，不折成 0 或失败 */
export function auditResultForExit(exitCode: number | null, durationMs: number): string {
  return `exit=${exitCode ?? 'null'} durationMs=${durationMs}`
}

/** 只有连接记录明确标了 prod 才算生产环境；未知/缺失一律 test */
export function auditEnvironmentFor(
  session: { connectionId?: string } | null | undefined,
  connections: ReadonlyArray<{ id: string; environment?: 'test' | 'prod' }>,
): 'test' | 'prod' {
  const conn = session?.connectionId
    ? connections.find(c => c.id === session.connectionId)
    : undefined
  return conn?.environment === 'prod' ? 'prod' : 'test'
}

export interface RemoteAuditRecordOptions {
  action: string
  conclusion: RemoteAuditConclusion
  targetId: string
  environment: 'test' | 'prod'
  command: string
  result: string
}

export function buildRemoteAuditRecord(o: RemoteAuditRecordOptions): AuditAppendInput {
  return {
    action: o.action,
    conclusion: o.conclusion,
    target_type: 'ssh',
    target_id: clipAuditText(o.targetId, AUDIT_TARGET_MAX),
    environment: o.environment,
    command_raw: clipAuditText(o.command, AUDIT_COMMAND_MAX),
    result: clipAuditText(o.result, AUDIT_RESULT_MAX),
  }
}

/** 写审计但不阻塞用户动作：失败只返回 false，由调用方聚合后提示一次 */
export async function writeRemoteAudit(input: AuditAppendInput): Promise<boolean> {
  try {
    await auditApi.append(input)
    return true
  } catch (e) {
    console.warn('[audit] 通道审计写入失败:', input.action, e)
    return false
  }
}
