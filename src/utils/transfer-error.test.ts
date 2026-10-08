/**
 * S0-15 / S0-23 回归：跨 IPC 的传输错误与 SSH 错误码回读。
 *
 * S0-15 的症状是「自动续传整支死」：渲染层只读 `err.code`/`err.retryable` 这两个
 * 自定义属性，而本仓对 IPC 的自述前提是自定义属性会丢（main.ts normalizeIpcError
 * 因此对 SSHError 双写 name + __sshError）。现在两条腿都读，且 retryable 由 code
 * 现推 —— 与主进程保留/删除 .part 用的是同一个 isTransferRetryable，两侧不可能
 * 对同一 code 有两种理解。
 */
import { describe, expect, it } from 'vitest'
import {
  encodeTransferErrorName,
  isTransferRetryable,
  parseTransferErrorCode,
} from '../../shared/transfer-error-core'
import { describeTransferError } from './transfer-error'
import { extractSSHError } from '@/ssh-error-types'

const envelope = (msg: string) => `Error invoking remote method 'sftp:upload': Error: ${msg}`

describe('transfer-error-core 编解码', () => {
  it('name 腿可往返，且只认契约内的码', () => {
    expect(encodeTransferErrorName('STALLED')).toBe('TransferError:STALLED')
    expect(parseTransferErrorCode('TransferError:NETWORK')).toBe('NETWORK')
    expect(parseTransferErrorCode('CANCELLED')).toBe('CANCELLED')
    expect(parseTransferErrorCode('Error')).toBeNull()
    expect(parseTransferErrorCode(undefined)).toBeNull()
  })

  it('retryable 与主进程 .part 取舍同判据', () => {
    expect(isTransferRetryable('NETWORK')).toBe(true)
    expect(isTransferRetryable('STALLED')).toBe(true)
    // 审查 P2：数据已完整落到 .part，只差 rename —— 重试可续传完成，必须保留
    expect(isTransferRetryable('RENAME_FAILED')).toBe(true)
    expect(isTransferRetryable('DISK_FULL')).toBe(false)
    expect(isTransferRetryable('PERMISSION')).toBe(false)
    expect(isTransferRetryable('CANCELLED')).toBe(false)
    expect(isTransferRetryable('UNKNOWN')).toBe(false)
  })
})

describe('describeTransferError — 两条腿（S0-15）', () => {
  it('自定义属性被 IPC 丢掉时，name 腿仍能恢复 code 并放行自动续传', () => {
    const e = new Error('网络中断，已保存进度可续传')
    e.name = 'TransferError:NETWORK'
    const info = describeTransferError(e)
    expect(info.code).toBe('NETWORK')
    expect(info.retryable).toBe(true)
  })

  it('属性腿存活时优先用它', () => {
    const e = Object.assign(new Error('传输停滞，网络可能不稳定'), { code: 'STALLED' })
    expect(describeTransferError(e)).toEqual({
      code: 'STALLED',
      retryable: true,
      message: '传输停滞，网络可能不稳定',
    })
  })

  it('两条腿都没有 ⇒ 不猜测、不自动重试', () => {
    const info = describeTransferError(new Error('boom'))
    expect(info.code).toBeUndefined()
    expect(info.retryable).toBe(false)
  })

  it('不相干的 code 字符串不当成合法码（S0-23 同族）', () => {
    const e = Object.assign(new Error('boom'), { code: 'EBUSY' })
    expect(describeTransferError(e).code).toBeUndefined()
    expect(describeTransferError(e).retryable).toBe(false)
  })

  it('message 剥掉 Electron 信封，内部通道名不进用户可见区（S0-24）', () => {
    const e = Object.assign(new Error(envelope('磁盘空间不足')), { code: 'DISK_FULL' })
    const info = describeTransferError(e)
    expect(info.message).toBe('磁盘空间不足')
    expect(info.message).not.toContain('sftp:upload')
    expect(info.retryable).toBe(false)
  })
})

describe('extractSSHError — 幽灵码不认账（S0-23）', () => {
  it('name 前缀里是不认识的码 ⇒ 不当结构化错误处理', () => {
    const e = new Error('任意消息') as Error & { name: string }
    e.name = 'SSHError:NOT_A_CODE'
    expect(extractSSHError(e)).toBeNull()
  })

  it('__sshError 里码不认账 ⇒ 降级 UNKNOWN 但保留 message', () => {
    const e = new Error('连接被拒绝。请检查主机地址和端口是否正确') as Error & { __sshError?: unknown }
    e.__sshError = {
      __sshError: true,
      code: 'SFTP_DELETED_CODE',
      message: '连接被拒绝。请检查主机地址和端口是否正确',
      retryable: true,
    }
    const t = extractSSHError(e)
    expect(t?.code).toBe('UNKNOWN')
    expect(t?.message).toBe('连接被拒绝。请检查主机地址和端口是否正确')
  })
})
