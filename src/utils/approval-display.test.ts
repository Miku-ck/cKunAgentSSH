/**
 * 审批展示派生 —— 纯函数，无 pinia（与 approval-scopes.test.ts 同风格）
 *
 * 锁住三条退路：有连接记录用连接名、连接被删退到会话名、连会话都没有退到 id 片段。
 * 第三条最容易漏：连接删掉后队列不能显示空白，否则用户不知道这张卡要动哪台机。
 * 另锁写入卡：卡片显示的是被授权的路径与内容，不是后端的分类占位模板。
 */
import { describe, expect, it } from 'vitest'
import {
  approvalCommand,
  approvalRiskClass,
  approvalRiskLabel,
  approvalTargetLabel,
  approvalWritePreview,
} from '@/utils/approval-display'

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

  it('审批卡传 emptyLabel：本机执行时报「本机」，而不是整行消失', () => {
    // 目标行凭空消失读起来像"卡片漏了字段"，不像"这就是台本机"
    expect(approvalTargetLabel({}, SESSIONS, CONNS, '本机')).toBe('本机')
  })
})

describe('approvalRiskLabel', () => {
  it('三级风险映射为人读文案', () => {
    expect(approvalRiskLabel('dangerous')).toBe('高危')
    expect(approvalRiskLabel('moderate')).toBe('中风险')
    expect(approvalRiskLabel('safe')).toBe('低风险')
  })

  it('认不出的风险值报「未知（按高危处理）」，不原样透出', () => {
    // 后端缺字段/脏值时 approval-scopes 已按 dangerous 收口（fail-closed）。
    // 卡片把 'catastrophic' 或空串原样画出来 = 用户读到一句看不懂的英文就点了批准。
    for (const junk of ['catastrophic', '', 'HIGH', undefined as unknown as string]) {
      expect(approvalRiskLabel(junk)).toBe('未知（按高危处理）')
    }
  })
})

describe('approvalRiskClass', () => {
  it('已知档位用各自配色类', () => {
    expect(approvalRiskClass('dangerous')).toBe('risk-dangerous')
    expect(approvalRiskClass('moderate')).toBe('risk-moderate')
    expect(approvalRiskClass('safe')).toBe('risk-safe')
  })

  it('未知档位落到 risk-unknown（底色等同高危，不能画成没皮的空徽章）', () => {
    expect(approvalRiskClass('')).toBe('risk-unknown')
    expect(approvalRiskClass('catastrophic')).toBe('risk-unknown')
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

describe('approvalWritePreview', () => {
  // file_write/file_edit 报给后端的「命令」是一条固定占位模板：授权键落在路径上，
  // 内容变了不重新审批。卡片把模板原样显示成「授权命令」= 用户批准的不是他看到的东西。
  const WRITE_CMD = "printf '%s' 'BASE64_CONTENT_PAYLOAD' | base64 -d > '/etc/hosts'"

  it('写入卡给出目标路径与内容，而不是模板命令', () => {
    expect(approvalWritePreview({
      tool: 'file_write',
      command: WRITE_CMD,
      args: { path: '/etc/hosts', content: '127.0.0.1 localhost\n' },
    })).toEqual({ path: '/etc/hosts', content: '127.0.0.1 localhost\n' })
  })

  it('编辑卡给出替换前后两段', () => {
    expect(approvalWritePreview({
      tool: 'file_edit',
      command: WRITE_CMD,
      args: { path: '/etc/nginx/nginx.conf', old_string: 'worker_processes 1', new_string: 'worker_processes auto' },
    })).toEqual({
      path: '/etc/nginx/nginx.conf',
      oldText: 'worker_processes 1',
      newText: 'worker_processes auto',
    })
  })

  it('归一化形态里带占位符同样命中', () => {
    expect(approvalWritePreview({
      tool: 'file_write',
      command: '无关原文',
      normalizedCommand: WRITE_CMD,
      args: { path: '/tmp/a', content: 'x' },
    })?.path).toBe('/tmp/a')
  })

  it('超长内容截断并标注', () => {
    const preview = approvalWritePreview({
      tool: 'file_write',
      command: WRITE_CMD,
      args: { path: '/tmp/big', content: 'a'.repeat(5000) },
    })
    expect(preview?.content).toHaveLength(2000)
    expect(preview?.truncated).toBe(true)
  })

  it('非写入卡返回 null（调用方照旧显示授权命令行）', () => {
    expect(approvalWritePreview({ tool: 'shell_exec', command: 'rm -rf /tmp/x', args: {} })).toBeNull()
  })

  it('命中占位符但取不到路径时返回 null，不画空路径', () => {
    expect(approvalWritePreview({ tool: 'file_write', command: WRITE_CMD, args: { content: 'x' } })).toBeNull()
  })
})
