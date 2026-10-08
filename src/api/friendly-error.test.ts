/**
 * 渲染端错误文案缝测试（S0-13 / S0-14）。
 *
 * 症状：主进程任何非 SSHError 的 reject 到 UI 都是一句「服务暂时不可用，请稍后重试」，
 * 因为 Electron 的投递信封 `Error invoking remote method 'x': Error: <msg>` 恒含
 * `Error: `，而技术信息判定用 `/Traceback|Exception|Error:\s/i` 在**未剥信封**的
 * 消息上跑 —— 于是那条模式必然命中，具体根因永不可见（S4-06 把 write() 改成抛错后，
 * 这条路径正好把新加的 SESSION_NOT_FOUND 文案又吞掉，是两个缺陷的交汇点）。
 *
 * 反向断言也要成立：真正的技术串（Traceback / sqlite / SQL）剥完信封后仍须被拦下，
 * 否则就是把后端内部信息泄露给用户。
 */
import { describe, expect, it } from 'vitest'
import { toFriendlyMessage, unwrapIpcErrorEnvelope } from './client'

const env = (msg: string) => `Error invoking remote method 'ssh:write': Error: ${msg}`

describe('unwrapIpcErrorEnvelope（S0-13）', () => {
  it('剥掉 Electron 的 invoke 信封与前导 Error:', () => {
    expect(unwrapIpcErrorEnvelope(env('SSH 会话不存在或已断开'))).toBe('SSH 会话不存在或已断开')
  })

  it('非信封消息原样返回', () => {
    expect(unwrapIpcErrorEnvelope('后端未就绪')).toBe('后端未就绪')
  })

  it('信封里没有根因时不把用户推到空字符串', () => {
    expect(unwrapIpcErrorEnvelope("Error invoking remote method 'x': Error: ")).toBe(
      "Error invoking remote method 'x': Error: "
    )
  })
})

describe('toFriendlyMessage — 主进程业务错误的根因可见（S0-13）', () => {
  it('信封包裹的中文业务文案原样呈现，不再塌成通用句', () => {
    expect(toFriendlyMessage(new Error(env('目标目录不合法，已拒绝')))).toBe('目标目录不合法，已拒绝')
  })

  it('信封包裹的会话缺失文案可见（S4-06 write() 抛错的用户侧收益）', () => {
    expect(toFriendlyMessage(new Error(env('SSH 会话不存在或已断开')))).toBe('SSH 会话不存在或已断开')
  })

  it('剥完信封仍要拦住真实技术串，不泄露内部细节', () => {
    expect(toFriendlyMessage(new Error(env('sqlite3.OperationalError: table x has no column y')))).toBe(
      '服务暂时不可用，请稍后重试'
    )
    expect(toFriendlyMessage(new Error(env('Traceback (most recent call last): ...')))).toBe(
      '服务暂时不可用，请稍后重试'
    )
  })

  it('既有的专用映射不受剥信封影响', () => {
    expect(toFriendlyMessage(new Error(env('Backend not ready')))).toBe('后端服务未就绪，请稍后重试')
    expect(toFriendlyMessage(new Error('fetch failed'))).toBe('网络连接失败，请检查服务是否运行')
  })
})

describe('toFriendlyMessage — 采信 transport.message（S0-14）', () => {
  /** 复刻 electron/main.ts normalizeIpcError 的产物形态 */
  const transport = (code: string, message: string, detail?: string) => {
    const e = new Error(message) as Error & { name: string; __sshError?: unknown }
    e.name = `SSHError:${code}`
    e.__sshError = { __sshError: true, code, message, retryable: false, detail }
    return e
  }

  it('主进程渲染好的 detail 不被重渲染抹掉', () => {
    const msg = toFriendlyMessage(transport('EXEC_TIMEOUT', '命令执行超时（30秒），可增大命令超时时间', '30秒'))
    expect(msg).toBe('命令执行超时（30秒），可增大命令超时时间')
  })

  it('message 缺失时才按 code+detail 兜底重渲染', () => {
    expect(toFriendlyMessage(transport('EXEC_TIMEOUT', '', '30秒'))).toContain('（30秒）')
  })

  it('只有 name 前缀（__sshError 序列化保真失败）时仍能出文案', () => {
    const e = new Error('') as Error & { name: string }
    e.name = 'SSHError:SESSION_LIMIT'
    expect(toFriendlyMessage(e)).toBe('会话数已达上限，请关闭闲置连接后重试')
  })
})
