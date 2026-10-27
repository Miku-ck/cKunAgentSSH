import { afterEach, describe, expect, it, vi } from 'vitest'
import { auditApi } from '@/api/audit'
import {
  AUDIT_ACTION_BULK,
  AUDIT_ACTION_DOCKER,
  AUDIT_COMMAND_MAX,
  AUDIT_RESULT_MAX,
  AUDIT_TARGET_MAX,
  auditEnvironmentFor,
  auditOutcomeForDispatchError,
  auditResultForExit,
  buildRemoteAuditRecord,
  clipAuditText,
  writeRemoteAudit,
} from './remote-audit'

/** 生产形状：IPC 回来的 Error 上挂 __sshError transport（不是裸 transport） */
function ipcError(transport: Record<string, unknown>): Error {
  const e = new Error(String(transport.message)) as Error & { __sshError?: unknown }
  e.__sshError = { __sshError: true, ...transport }
  return e
}

const timeoutErr = ipcError({
  code: 'EXEC_TIMEOUT',
  message: '命令执行超时（30秒）',
  retryable: true,
  detail: '30秒',
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('clipAuditText', () => {
  it('短文本原样保留', () => {
    expect(clipAuditText('docker rm -f web', 100)).toBe('docker rm -f web')
  })

  it('超长文本截断后不超过上限并带标记', () => {
    const out = clipAuditText('x'.repeat(AUDIT_RESULT_MAX + 50), AUDIT_RESULT_MAX)
    expect(out.length).toBeLessThanOrEqual(AUDIT_RESULT_MAX)
    expect(out.endsWith('[truncated]')).toBe(true)
  })

  it('按码点截断，不劈开代理对', () => {
    const out = clipAuditText('😀'.repeat(40), 10)
    expect([...out].length).toBeLessThanOrEqual(10)
    expect(out).not.toMatch(/[\uD800-\uDBFF]$/)
  })
})

describe('auditOutcomeForDispatchError', () => {
  it('EXEC_TIMEOUT 记 executed：命令可能已在远端落地，结局未知不等于失败', () => {
    const out = auditOutcomeForDispatchError(timeoutErr, '命令执行超时（30秒）', 30)
    expect(out).toEqual({
      conclusion: 'executed',
      result: 'timeout=30s remote outcome unknown',
    })
  })

  it('DockerError 形状的 EXEC_TIMEOUT 同样认（describeThrownDockerError 只留 code）', () => {
    const out = auditOutcomeForDispatchError(
      { code: 'EXEC_TIMEOUT', message: '命令执行超时（60秒）', retryable: false },
      '命令执行超时（60秒）',
      60,
    )
    expect(out.conclusion).toBe('executed')
    expect(out.result).toContain('60s')
  })

  it('name 形式的 SSHError 也认', () => {
    const err = Object.assign(new Error('命令执行超时（30秒）'), { name: 'SSHError:EXEC_TIMEOUT' })
    expect(auditOutcomeForDispatchError(err, '超时', 30).conclusion).toBe('executed')
  })

  it('普通错误记 failed，并保留已渲染好的用户文案', () => {
    expect(auditOutcomeForDispatchError(new Error('connect ECONNREFUSED'), '连接失败')).toEqual({
      conclusion: 'failed',
      result: 'error=连接失败',
    })
  })

  it('有 SSH 错误码时带码，便于事后按故障类型检索', () => {
    const out = auditOutcomeForDispatchError(
      ipcError({ code: 'EXEC_FAIL', message: '连接已断开', retryable: true }),
      '连接已断开',
    )
    expect(out.conclusion).toBe('failed')
    expect(out.result).toBe('error[EXEC_FAIL]=连接已断开')
  })

  it('未知 code 不冒充超时', () => {
    const out = auditOutcomeForDispatchError(
      ipcError({ code: 'NOT_A_REAL_CODE', message: 'x', retryable: false }),
      'x',
    )
    expect(out.conclusion).toBe('failed')
    expect(out.result).toContain('UNKNOWN')
  })
})

describe('auditResultForExit', () => {
  it('exit 0 与非零都只说明「已回包」，结果里如实带退出码', () => {
    expect(auditResultForExit(0, 12)).toBe('exit=0 durationMs=12')
    expect(auditResultForExit(127, 9)).toBe('exit=127 durationMs=9')
  })

  it('没有退出码时写 null，不折成 0 也不折成失败', () => {
    expect(auditResultForExit(null, 5)).toBe('exit=null durationMs=5')
  })
})

describe('auditEnvironmentFor', () => {
  it('只有连接明确标了 prod 才算生产环境', () => {
    expect(auditEnvironmentFor({ connectionId: 'c1' }, [{ id: 'c1', environment: 'prod' }])).toBe('prod')
    expect(auditEnvironmentFor({ connectionId: 'c1' }, [{ id: 'c1', environment: 'test' }])).toBe('test')
    expect(auditEnvironmentFor({ connectionId: 'c1' }, [{ id: 'c1' }])).toBe('test')
    expect(auditEnvironmentFor({}, [])).toBe('test')
    expect(auditEnvironmentFor(null, [])).toBe('test')
  })
})

describe('buildRemoteAuditRecord', () => {
  it('目标类型固定 ssh，环境与命令原样进记录', () => {
    expect(buildRemoteAuditRecord({
      action: AUDIT_ACTION_BULK,
      conclusion: 'executed',
      targetId: 'web-1 (root@10.0.0.1)',
      environment: 'prod',
      command: 'systemctl restart nginx',
      result: 'exit=0 durationMs=12',
    })).toEqual({
      action: 'bulk_ssh_exec',
      conclusion: 'executed',
      target_type: 'ssh',
      target_id: 'web-1 (root@10.0.0.1)',
      environment: 'prod',
      command_raw: 'systemctl restart nginx',
      result: 'exit=0 durationMs=12',
    })
  })

  it('命令、结果、目标按服务端上限裁剪', () => {
    const rec = buildRemoteAuditRecord({
      action: AUDIT_ACTION_DOCKER,
      conclusion: 'failed',
      targetId: 't'.repeat(AUDIT_TARGET_MAX + 10),
      environment: 'test',
      command: 'c'.repeat(AUDIT_COMMAND_MAX + 10),
      result: 'r'.repeat(AUDIT_RESULT_MAX + 10),
    })
    expect(rec.target_id!.length).toBe(AUDIT_TARGET_MAX)
    expect(rec.command_raw!.length).toBe(AUDIT_COMMAND_MAX)
    expect(rec.result!.length).toBe(AUDIT_RESULT_MAX)
  })
})

describe('writeRemoteAudit', () => {
  it('写入成功返回 true', async () => {
    const spy = vi.spyOn(auditApi, 'append').mockResolvedValue({ ok: true, id: 1, hash: 'h' })
    await expect(writeRemoteAudit({ action: 'x', conclusion: 'executed' })).resolves.toBe(true)
    expect(spy).toHaveBeenCalledWith({ action: 'x', conclusion: 'executed' })
  })

  it('写入失败只返回 false，不把异常抛回用户动作链', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(auditApi, 'append').mockRejectedValue(new Error('审计写入失败，请稍后重试'))
    await expect(writeRemoteAudit({ action: 'x', conclusion: 'failed' })).resolves.toBe(false)
  })
})
