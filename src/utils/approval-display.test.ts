/**
 * 审批展示派生 —— 纯函数，无 pinia（与 approval-scopes.test.ts 同风格）
 *
 * 锁住三条退路：有连接记录用连接名、连接被删退到会话名、连会话都没有退到 id 片段。
 * 第三条最容易漏：连接删掉后队列不能显示空白，否则用户不知道这张卡要动哪台机。
 */
import { describe, expect, it } from 'vitest'
import { approvalCommand, approvalRiskLabel, approvalTargetLabel } from '@/utils/approval-display'

const SESSIONS = [
  { id: 's-1', name: 'web-03', connectionId: 'c-1' },
  { id: 's-2', name: 'pwsh' },
]
const CONNS = [{ id: 'c-1', name: '生产 web', host: '10.20.1.7', username: 'root' }]

describe('approvalTargetLabel', () => {
  it('有连接记录时显示「连接名（用户@主机）」', () => {
    expect(approvalTargetLabel({ sshSessionId: 's-1' }, SESSIONS, CONNS))
      .toBe('生产 web（root@10.20.1.7）')
  })

  it('连接记录已删除时退到会话名', () => {
    expect(approvalTargetLabel({ sshSessionId: 's-1' }, SESSIONS, []))
      .toBe('web-03')
  })

  it('本地终端会话（无 connectionId）用会话名', () => {
    expect(approvalTargetLabel({ sshSessionId: 's-2' }, SESSIONS, CONNS))
      .toBe('pwsh')
  })

  it('会话也不在了时退到 id 片段，不留空白', () => {
    expect(approvalTargetLabel({ sshSessionId: 's-gone-123456' }, SESSIONS, CONNS))
      .toBe('会话 s-gone-1')
  })

  it('没有 sshSessionId 返回空串（调用方据此不渲染该行）', () => {
    expect(approvalTargetLabel({}, SESSIONS, CONNS)).toBe('')
  })
})

describe('approvalRiskLabel', () => {
  it('三级风险映射为人读文案', () => {
    expect(approvalRiskLabel('dangerous')).toBe('高危')
    expect(approvalRiskLabel('moderate')).toBe('中风险')
    expect(approvalRiskLabel('safe')).toBe('低风险')
  })

  it('未知风险原样透出，不静默降级成「低风险」', () => {
    expect(approvalRiskLabel('catastrophic')).toBe('catastrophic')
  })
})

describe('approvalCommand', () => {
  it('优先归一化命令（剥混淆后的形态）', () => {
    expect(approvalCommand({ normalizedCommand: 'nginx -s reload', command: 'nginx   -s    reload' }))
      .toBe('nginx -s reload')
  })

  it('无归一化时退回 payload 原文', () => {
    expect(approvalCommand({ command: 'systemctl restart nginx' })).toBe('systemctl restart nginx')
  })
})
