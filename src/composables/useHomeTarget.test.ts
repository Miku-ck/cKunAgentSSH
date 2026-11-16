/**
 * 首页底栏的目标读数（spec 2026-09-29-home-command-panel · 用户故事 2/3，接缝 S2）
 *
 * 这里只测展示派生：本地非默认 shell 的差量标记（targetChip / targetHint）与
 * 批量下发门槛（remoteCount / canBulkDispatch）。AI 的远程落点选择已迁到
 * exec-target（aiSessionId，与用户终端隔离），不在本 composable 里。
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

  it('目标标记只给 chip 上没有的差量：远程不重复印地址，本地只有非默认 shell 才亮', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' })
    ssh.activeSessionId = 's1'
    // 第一枚 chip 已经写着这台机器：右边再印一遍同一串地址就是重复（用户截图口径）
    expect(t.targetChip.value).toBe('')
    // 地址没丢，在第一枚 chip 的 title 里
    expect(t.targetHint.value).toBe('root@10.20.1.7')
    // 本地默认落点：底栏已经有「本地 + 文件夹」，再印 PowerShell 只是重复默认值
    ssh.sessions.push({ id: 's2', name: 'PowerShell', type: 'local', shell: 'PowerShell' })
    ssh.activeSessionId = 's2'
    expect(t.targetChip.value).toBe('')
    // 非默认 shell（WSL / Git Bash / CMD）命令语法换了一套，必须亮出来
    ssh.sessions.push({ id: 's3', name: 'WSL', type: 'local', shell: 'WSL' })
    ssh.activeSessionId = 's3'
    expect(t.targetChip.value).toBe('WSL')
    ssh.sessions.push({ id: 's4', name: 'Git Bash', type: 'local', shell: 'Git Bash' })
    ssh.activeSessionId = 's4'
    expect(t.targetChip.value).toBe('Git Bash')
  })

  it('远程地址拆不出来时宁可不显示，不把会话名冒充 IP', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: '', connectionId: 'c1' })
    ssh.activeSessionId = 's1'
    expect(t.targetChip.value).toBe('')
  })

  it('悬停带出连接名，认得的是名字不是地址', () => {
    const { ssh, t } = mount()
    ssh.sessions.push({ id: 's1', name: 'web-03', type: 'remote', shell: 'root@10.20.1.7', connectionId: 'c1' })
    ssh.connections.push({ id: 'c1', name: '电商-web-03', host: '10.20.1.7', username: 'root' } as never)
    ssh.activeSessionId = 's1'
    expect(t.targetHint.value).toBe('电商-web-03 · root@10.20.1.7')
    // 连接记录被删掉：退回身份串，不留空白
    ssh.connections.length = 0
    expect(t.targetHint.value).toBe('root@10.20.1.7')
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
