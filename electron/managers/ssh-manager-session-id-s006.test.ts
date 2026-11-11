/**
 * S0-06 回归：会话 id 必须带上「哪一次启动」这一段。
 *
 * `nextId` 每次主进程启动都从 1 重来，而 `agent_jobs.session_id` 跨重启持久 ——
 * 旧格式 `ssh-1` 在新进程里天然会撞上新的某个会话，而桥接那条腿原先没有任何
 * 所有权判定（只验非空字符串）。后果不是报错而是**拿别人的会话干活**：恢复的
 * 远程作业对用户正在用的终端下发命令，或把它 disconnect 掉。
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => '/tmp/ckun-agent-ssh-test',
    getAppPath: () => '/tmp/ckun-agent-ssh-test',
  },
}))
vi.mock('ssh2', () => ({ Client: class {} }))

import { SSHManager } from './ssh-manager'

describe('newSessionId 的跨进程不重叠（S0-06）', () => {
  it('两次启动各自发的 id 集合没有交集', () => {
    // 两个实例 = 主进程重启前后。旧形状在这里必然撞车（都是 ssh-1…ssh-20）
    const before = new SSHManager()
    const after = new SSHManager()
    const previous = new Set<string>()
    for (let i = 0; i < 20; i++) {
      previous.add(before.newSessionId('ssh'))
      previous.add(before.newSessionId('sftp'))
    }
    for (let i = 0; i < 20; i++) {
      for (const id of [after.newSessionId('ssh'), after.newSessionId('sftp')]) {
        expect(previous.has(id), `重启后的 id ${id} 与重启前的某个 id 撞车`).toBe(false)
      }
    }
  })

  it('上一进程持久化下来的裸 ssh-1 在这一进程恒不存在', () => {
    const mgr = new SSHManager()
    // 恢复的旧作业带的就是这一串（ops_job._ensure_restored 原样回填 session_id）
    expect(mgr.newSessionId('ssh')).not.toBe('ssh-1')
    expect(mgr.isBridgeSession('ssh-1')).toBe(false)
  })

  it('同一进程内仍然逐条唯一，且 ssh 与 sftp 共用一个计数器不复用号', () => {
    const mgr = new SSHManager()
    const ids = new Set([mgr.newSessionId('ssh'), mgr.newSessionId('ssh'), mgr.newSessionId('sftp')])
    expect(ids.size).toBe(3)
    expect([...ids].every((id) => /^(ssh|sftp)-[0-9a-f]{8}-\d+$/.test(id))).toBe(true)
  })
})
