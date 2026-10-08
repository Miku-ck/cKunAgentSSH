/**
 * S5-19 回归：exit-status 缺失（exitCode === null）不是失败。
 *
 * ssh2 只在收到 exit-status 时填这个数，`close` 事件不带它 —— 精简 sshd 不回、
 * 命令被 OOM kill 之后，读数都是 null。原实现按 `!== 0` 判失败并 **unlink 归档**，
 * 于是「其实已经打包好」的那次把用户要的产物删掉了，还附一句「远程打包失败」。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: { getPath: () => '/tmp/ckun-agent-ssh-test', getAppPath: () => '/tmp/ckun-agent-ssh-test' },
}))
vi.mock('ssh2', () => ({ Client: class {} }))

import { SSHManager, classifyExit } from './ssh-manager'

type ExecResult = { stdout: string; stderr: string; exitCode: number | null }

describe('classifyExit（S5-19 的三态判据）', () => {
  it('0 是成功，null 是不知道，其余才是失败', () => {
    expect(classifyExit(0)).toBe('ok')
    expect(classifyExit(null)).toBe('unknown')
    expect(classifyExit(1)).toBe('failed')
    expect(classifyExit(127)).toBe('failed')
  })
})

describe('sftpZipRemote 不再销毁结果未知的产物', () => {
  let mgr: SSHManager
  let deleted: string[]
  let zipExit: number | null
  let archiveExists: boolean

  function stub(execReturn: ExecResult) {
    mgr.execCommandWithExitCode = vi.fn(async (_sid: string, cmd: string) => {
      // zip 可用性探测固定回「有 zip」，只有真正的打包命令用被测的读数
      if (cmd.startsWith('command -v zip')) return { stdout: 'yes\n', stderr: '', exitCode: 0 }
      return execReturn
    }) as never
  }

  beforeEach(() => {
    mgr = new SSHManager()
    deleted = []
    zipExit = null
    archiveExists = true
    mgr.sftpDelete = vi.fn(async (_sid: string, p: string) => { deleted.push(p) }) as never
    mgr.sftpExists = vi.fn(async () => archiveExists) as never
  })

  it('exit-status 缺失且归档在：保留产物，并把不确定性说清楚', async () => {
    stub({ stdout: '', stderr: '', exitCode: zipExit })
    await expect(mgr.sftpZipRemote('ssh-1', ['a.txt'], '/remote/x.zip', '/remote'))
      .rejects.toThrow(/结果未知/)
    expect(deleted).toEqual([])
  })

  it('exit-status 缺失且归档不在：这才算失败，且不说成「结果未知」', async () => {
    archiveExists = false
    stub({ stdout: '', stderr: 'killed', exitCode: null })
    await expect(mgr.sftpZipRemote('ssh-1', ['a.txt'], '/remote/x.zip', '/remote'))
      .rejects.toThrow(/未生成归档/)
  })

  it('确实失败（exit≠0）时仍按 Bug 3 的口径清理半成品', async () => {
    stub({ stdout: '', stderr: 'disk full', exitCode: 9 })
    await expect(mgr.sftpZipRemote('ssh-1', ['a.txt'], '/remote/x.zip', '/remote'))
      .rejects.toThrow(/远程打包失败: disk full/)
    expect(deleted).toEqual(['/remote/x.zip'])
  })

  it('成功路径不删不清理', async () => {
    stub({ stdout: '', stderr: '', exitCode: 0 })
    const r = await mgr.sftpZipRemote('ssh-1', ['a.txt'], '/remote/x.zip', '/remote')
    expect(r.exitCode).toBe(0)
    expect(deleted).toEqual([])
  })
})
