/**
 * 首页标题里的目标主机（spec 2026-09-29-home-command-panel · 用户故事 2/3，接缝 S2）
 *
 * 这条派生是首页唯一的目标选择入口：AI 的 shell_exec 落在 ssh store 的
 * activeSessionId 上，所以「换目标」的正确含义就是改那一个字段 —— 既不去
 * 远程管理页配置，也不能顺手把首页切成终端（用户正在输入，跳走等于吞掉现场）。
 * 会话字段用的是 TerminalSession.shell（远程会话建时即写入 `username@host`，
 * 是这条连接当时的真实身份；连接记录事后被改名/改主机都不该影响已跑的会话）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSSHStore } from '@/stores/ssh'

const { useHomeTarget } = await import('@/composables/useHomeTarget')

function mount() {
  const ssh = useSSHStore()
  const t = useHomeTarget()
  return { ssh, t }
}

describe('useHomeTarget', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('没有活动会话时回落到「服务器」', () => {
    const { t } = mount()
    expect(t.targetLabel.value).toBe('服务器')
  })

  it('远程活动会话显示 username@host', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' })
    ssh.activeSessionId = 's1'
    expect(t.targetLabel.value).toBe('root@10.20.1.7')
  })

  it('本地活动会话显示 shell 名，不假装是主机', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'PowerShell', type: 'local', shell: 'PowerShell' })
    ssh.activeSessionId = 's1'
    expect(t.targetLabel.value).toBe('PowerShell')
  })

  it('切会话时标题跟着变', () => {
    const { ssh, t } = mount()
    ssh.sessions.push(
      { id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' },
      { id: 's2', name: 'pg', type: 'remote', shell: 'ops@10.20.2.11', connectionId: 'c2' },
    )
    ssh.activeSessionId = 's1'
    expect(t.targetLabel.value).toBe('root@10.20.1.7')
    ssh.activeSessionId = 's2'
    expect(t.targetLabel.value).toBe('ops@10.20.2.11')
  })

  it('会话缺 shell（旧数据）时退回会话名，不显示空白', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: '', connectionId: 'c1' })
    ssh.activeSessionId = 's1'
    expect(t.targetLabel.value).toBe('web-03')
  })

  it('候选项是已打开的会话，且只有当前目标被标为选中', () => {
    const { ssh, t } = mount()
    ssh.sessions.push(
      { id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' },
      { id: 's2', name: 'pwsh', type: 'local', shell: 'PowerShell' },
    )
    ssh.activeSessionId = 's2'
    expect(t.options.value.map(o => o.sessionId)).toEqual(['s1', 's2'])
    expect(t.options.value.map(o => o.active)).toEqual([false, true])
    expect(t.options.value[0].label).toBe('root@10.20.1.7')
    // 副标题给连接名，用户认得的是名字不是 IP
    expect(t.options.value[0].hint).toBe('web-03')
    expect(t.options.value[1].hint).toBe('本地终端')
  })

  it('选目标只改活动会话，首页状态不在这里被触碰', () => {
    const { ssh, t } = mount()
    ssh.sessions.push(
      { id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' },
      { id: 's2', name: 'pg', type: 'remote', shell: 'ops@10.20.2.11', connectionId: 'c2' },
    )
    ssh.activeSessionId = 's1'
    t.pick('s2')
    expect(ssh.activeSessionId).toBe('s2')
    expect(t.targetLabel.value).toBe('ops@10.20.2.11')
  })

  it('选到已关闭的会话不改状态', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' })
    ssh.activeSessionId = 's1'
    t.pick('gone')
    expect(ssh.activeSessionId).toBe('s1')
  })

  it('两台以上远程会话才谈「全部 N 台」', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' })
    expect(t.remoteCount.value).toBe(1)
    expect(t.canBulkDispatch.value).toBe(false)
    ssh.sessions.push({ id: 's2', name: 'pg', type: 'remote', shell: 'ops@10.20.2.11', connectionId: 'c2' })
    ssh.sessions.push({ id: 's3', name: 'pwsh', type: 'local', shell: 'PowerShell' })
    expect(t.remoteCount.value).toBe(2)
    expect(t.canBulkDispatch.value).toBe(true)
  })
})
