/**
 * SSH 错误契约缝测试（S0-14 / S0-16）。
 *
 * 这一层是「主进程分类 → IPC transport → 渲染端文案」的唯一 Passage，
 * 历史上三处各自出错且互相掩盖：
 *   - S0-16：passphrase 分类正则抄的是 ssh2 从不产出的措辞 ⇒ 死分支
 *   - S0-14：toIpcTransport 只读 original.message ⇒ 生产方 detail 到不了渲染端
 *   - S0-14：DNS_FAIL 是唯一不判空的文案成员 ⇒ detail 缺失时产出「无法解析主机名""」
 * 断言用 ssh2 的**真实消息串**（取自 node_modules/ssh2/lib/protocol/keyParser.js
 * 与 client.js 的包装），改回抄来的措辞本文件就变红。
 */
import { describe, expect, it } from 'vitest'
import { SSHError, classifyRawError, toIpcTransport } from './ssh-error'
import { toFriendlyZh } from '../shared/ssh-error-core'

/** ssh2 client.js:261 对 keyParser 错误的包装（connect() 同步抛出） */
const SSH2_ENCRYPTED_KEY = 'Cannot parse privateKey: Encrypted private OpenSSH key detected, but no passphrase given'
/** keyParser.js:632 —— passphrase 给了但错 */
const SSH2_BAD_PASSPHRASE = 'OpenSSH key integrity check failed -- bad passphrase?'

describe('classifyRawError — 私钥 passphrase（S0-16）', () => {
  it('识别 ssh2 的「没给 passphrase」真实措辞', () => {
    expect(classifyRawError(new Error(SSH2_ENCRYPTED_KEY))).toBe('AUTH_PASSPHRASE')
  })

  it('识别 ssh2 的「passphrase 不对」真实措辞', () => {
    expect(classifyRawError(new Error(SSH2_BAD_PASSPHRASE))).toBe('AUTH_PASSPHRASE')
  })

  it('落到专门文案而不是泛化的 SSH 错误串', () => {
    const err = SSHError.from(new Error(SSH2_ENCRYPTED_KEY))
    expect(err.code).toBe('AUTH_PASSPHRASE')
    expect(err.message).toBe('私钥保护密码（passphrase）错误或缺失')
  })

  it('认证阶段失败仍按 authType 精确归类（不受新正则影响）', () => {
    const err = Object.assign(new Error('All configured authentication methods failed'), {
      level: 'client-authentication',
    })
    expect(classifyRawError(err, 'password')).toBe('AUTH_PASSWORD')
    expect(classifyRawError(err, 'privateKey')).toBe('AUTH_KEY')
  })
})

describe('toIpcTransport — detail 透传（S0-14）', () => {
  it('生产方 detail 跨进程可见', () => {
    const t = toIpcTransport(new SSHError('EXEC_TIMEOUT', { detail: '30秒' }))
    expect(t.detail).toBe('30秒')
    expect(t.message).toContain('30秒')
  })

  it('无 detail 时回退原始 Error 的技术消息', () => {
    const raw = new Error('getaddrinfo ENOTFOUND hoppo.example')
    const t = toIpcTransport(SSHError.from(raw))
    expect(t.detail).toBe(raw.message)
  })

  it('SESSION_NOT_FOUND 带上哪个会话（detail 不丢）', () => {
    const t = toIpcTransport(new SSHError('SESSION_NOT_FOUND', { detail: '会话 sftp-3 不存在' }))
    expect(t.detail).toBe('会话 sftp-3 不存在')
    expect(t.code).toBe('SESSION_NOT_FOUND')
  })
})

describe('toFriendlyZh — DNS_FAIL 判空（S0-14）', () => {
  it('detail 缺失时不再产出「无法解析主机名""」', () => {
    const text = toFriendlyZh('DNS_FAIL')
    expect(text).not.toContain('""')
    expect(text).toBe('主机名无法解析，请检查主机地址是否正确')
  })

  it('detail 为 Node 原始消息时只取主机名，不把英文技术串塞进中文 toast', () => {
    const text = toFriendlyZh('DNS_FAIL', 'getaddrinfo ENOTFOUND hoppo.example.com')
    expect(text).toContain('hoppo.example.com')
    expect(text).not.toContain('getaddrinfo')
    expect(text).not.toContain('ENOTFOUND')
  })

  it('detail 取不到主机名时走通用句式', () => {
    expect(toFriendlyZh('DNS_FAIL', 'dns resolution failed')).toBe('主机名无法解析，请检查主机地址是否正确')
  })

  it('EXEC_TIMEOUT 的 detail 仍然出现在文案里（超时秒数是用户下一步动作的依据）', () => {
    expect(toFriendlyZh('EXEC_TIMEOUT', '30秒')).toContain('（30秒）')
  })
})
