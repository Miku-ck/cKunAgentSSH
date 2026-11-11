/**
 * `cdRemote` 的行为守卫。
 *
 * 远端落点的「目录」不是一条配置，而是那条交互式 shell 的 cwd —— 后端只认
 * ssh_session_id，命令最终写回同一个 pty。所以「选目录」在远端只能落成一句 cd，
 * 这里守住两件事：**路径不许被 shell 解释**（空格 / 单引号 / $ 都得原样过去），
 * 以及写完之后 chip 立刻读到新目录（不等 OSC 7 那 5 秒轮询）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSSHStore } from './ssh'

const g = globalThis as unknown as Record<string, unknown>

beforeEach(() => {
  setActivePinia(createPinia())
  g.window = { electronAPI: undefined }
})

describe('cdRemote', () => {
  it('把路径整段单引号包住写进 pty，内部单引号正确转义', () => {
    const write = vi.fn()
    g.window = { electronAPI: { sshWrite: write } }
    const ssh = useSSHStore()

    ssh.cdRemote('s1', "/home/a b/it's $HOME")
    expect(write).toHaveBeenCalledTimes(1)
    const [sessionId, data] = write.mock.calls[0] as [string, string]
    expect(sessionId).toBe('s1')
    // 单引号闭合 → \' → 重新开启：shell 收到的就是字面量，不会当命令执行
    expect(data).toBe("cd -- '/home/a b/it'\\''s $HOME'\r")
  })

  it('写完立刻更新 chip 读数（乐观），不等 OSC 7 轮询', () => {
    g.window = { electronAPI: { sshWrite: vi.fn() } }
    const ssh = useSSHStore()
    ssh.sessions = [{ id: 's1', name: 'prod', type: 'remote', shell: 'root@h' }]

    ssh.cdRemote('s1', '/var/www/app')
    expect(ssh.sessionCwd('s1')).toBe('/var/www/app')
  })

  it('没有 IPC 通道 / 空路径时静默不动（node 环境不该抛）', () => {
    g.window = { electronAPI: undefined }
    const ssh = useSSHStore()
    expect(() => ssh.cdRemote('s1', '/tmp')).not.toThrow()
    expect(ssh.sessionCwd('s1')).toBe('')

    g.window = { electronAPI: { sshWrite: vi.fn() } }
    ssh.cdRemote('s1', '   ')
    expect(ssh.sessionCwd('s1')).toBe('')
  })
})
