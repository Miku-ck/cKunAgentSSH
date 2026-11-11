/**
 * 执行落点 store 的行为守卫。
 *
 * 核心契约：落点类型从真实会话反查（用户在终端页切会话时 chip 要跟着变），
 * 且「工作树」必须真的以该 worktree 路径为 cwd 开本地 PTY、同路径只开一次。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useExecTargetStore } from './exec-target'
import { useSSHStore } from './ssh'
import type { GitWorktree } from '../../shared/git-repos-core'

const g = globalThis as unknown as Record<string, unknown>

function worktree(path: string, branch: string | null = 'main'): GitWorktree {
  return { path, head: 'abc123', branch, detached: false, bare: false, locked: false, prunable: false, main: false }
}

beforeEach(() => {
  setActivePinia(createPinia())
  g.window = { electronAPI: undefined }
})

describe('落点判定 kind', () => {
  it('无会话为 null；本地 / 远程会话分别映射 local / ssh', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    expect(landing.kind).toBeNull()

    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    expect(landing.kind).toBe('local')

    ssh.sessions = [...ssh.sessions, { id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.kind).toBe('ssh')
  })

  it('会话被关掉后不臆造选中态', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    ssh.sessions = []
    expect(landing.kind).toBeNull()
  })
})

describe('activeShell（本地落点的 shell 跟读）', () => {
  it('本地会话读 shell 字段；远程会话与无会话返回空，不臆造', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    expect(landing.activeShell).toBe('')

    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    expect(landing.activeShell).toBe('PowerShell')

    ssh.sessions = [{ id: 'w1', name: 'WSL', type: 'local', shell: 'WSL' }]
    ssh.activeSessionId = 'w1'
    expect(landing.activeShell).toBe('WSL')

    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.activeShell).toBe('')
  })

  it('showBranch：只有工作树落点长第三枚分支 chip，本地与 SSH 都不长', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    expect(landing.showBranch).toBe(false)

    // 本地：就在当前文件夹，没有分支可言
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    expect(landing.showBranch).toBe(false)

    // SSH：另一台机器，分支读数属无中生有
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.showBranch).toBe(false)
  })

  it('activeDirLabel：第二枚 chip 三态都在，只换语义（文件夹 / 仓库 / 远程目录）', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    // 没有会话：没有目录可读，空串（界面把它写成「打开文件夹」，不编「主目录」）
    expect(landing.activeDirLabel).toBe('')

    // 本地读会话 cwd 的末级目录名
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell', cwd: 'D:\\work\\cKunMTIE' }]
    ssh.activeSessionId = 'l1'
    expect(landing.activeDirLabel).toBe('cKunMTIE')

    // 本地会话没有绑定目录（「不在项目中工作」/ 老会话）时留空，不印「主目录」
    ssh.sessions = [{ id: 'l2', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l2'
    expect(landing.activeDirLabel).toBe('')

    // 远程会话没有采集远程 cwd：落 ~ 而不是把本地路径张冠李戴
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.activeDirLabel).toBe('~')

    // 远程读到了真实目录：chip 只印末级名（`/home/ckun/www` 会把这枚 chip 挤爆），
    // 全路径走 activeDirPath 进 title。OSC 7 或一次性 pwd 兜底写进 cwdBySession 后
    // 这条读数就该跟着变（用户 2026-11-11 反馈：连上 SSH 目录 chip 不显示目录名）。
    ssh.setTerminalCwd('r1', '/home/ckun/www/app')
    expect(landing.activeDirLabel).toBe('app')
    expect(landing.activeDirPath).toBe('/home/ckun/www/app')

    // 根目录：末级名为空 → 退回原路径，不能印一个空 chip
    ssh.setTerminalCwd('r1', '/')
    expect(landing.activeDirLabel).toBe('/')
  })

  it('落在远程且 OSC 7 还没上报时，补一次 pwd 填进 chip', async () => {
    const exec = vi.fn(async () => ({ stdout: '/opt/app\n', stderr: '', exitCode: 0 }))
    g.window = { electronAPI: { sshExec: exec } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r2', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r2'

    // OSC 7 只在终端页可见时轮询上报；刚连上就回首页时这张表是空的 —— 不补探测
    // 目录 chip 就永远停在 `~`（用户反馈的「连接成功但不显示目录」）。
    await vi.waitFor(() => expect(exec).toHaveBeenCalledWith('r2', 'pwd', 5))
    await vi.waitFor(() => expect(landing.activeDirLabel).toBe('app'))
    expect(landing.activeDirPath).toBe('/opt/app')

    // 已有读数就不再往返：切走再切回来不该重发 pwd
    ssh.activeSessionId = ''
    ssh.activeSessionId = 'r2'
    await Promise.resolve()
    expect(exec).toHaveBeenCalledTimes(1)
  })

  it('主进程已跟到 OSC 7 时直接用跟踪值，不发 pwd', async () => {
    const exec = vi.fn(async () => ({ stdout: '/should-not-be-used\n', stderr: '', exitCode: 0 }))
    const getCwd = vi.fn(async () => '/srv/www')
    g.window = { electronAPI: { sshGetCwd: getCwd, sshExec: exec } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r3', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r3'

    await vi.waitFor(() => expect(getCwd).toHaveBeenCalledWith('r3'))
    await vi.waitFor(() => expect(landing.activeDirLabel).toBe('www'))
    expect(exec).not.toHaveBeenCalled()
  })

  it('会话已不在主进程时 chip 说「未连接」，不再把死目标画成 ~', async () => {
    // 真实事故复现：主进程早已移除这条会话（日志 [ssh] 会话 … 连接已关闭（已移除）），
    // 渲染层标签还留着 → OSC 7 永远没有值、探测回 SESSION_NOT_FOUND。
    // 那时 chip 仍印 `~`，看起来像「连上了只是没读到目录」，把人往错误方向带。
    const exec = vi.fn(async () => { throw new Error('SESSION_NOT_FOUND: SSH 会话不存在或已断开') })
    g.window = { electronAPI: { sshGetCwd: async () => '', sshExec: exec } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r4', name: 'prod', type: 'remote', shell: 'root@10.0.0.1' }]
    ssh.activeSessionId = 'r4'
    expect(landing.activeDirLabel).toBe('~')

    await vi.waitFor(() => expect(landing.activeDirLabel).toBe('未连接'))
    expect(landing.activeDirUnavailable).toBe(true)

    // 反证之后不再反复探测（否则每切一次落点就多一条 SESSION_NOT_FOUND 错误日志）
    ssh.activeSessionId = ''
    ssh.activeSessionId = 'r4'
    await Promise.resolve()
    expect(exec).toHaveBeenCalledTimes(1)

    // 重连成功（主进程推送）清掉失效标记，chip 回到可读状态并重新探测
    g.window = { electronAPI: { sshGetCwd: async () => '/var/log', sshExec: exec } }
    ssh.sessions[0].unavailable = false
    ssh.sessions[0].reconnectGaveUp = false
    await vi.waitFor(() => expect(landing.activeDirLabel).toBe('log'))
  })

  it('幽灵会话自动回拨一次：连上后 chip 直接读出新远端目录', async () => {
    // 用户口径「连接成功却不显示文件夹」的完整闭环：标签是活的，主进程池里没有它。
    // 只把 chip 改成「未连接」不够 —— 首页得把这条连接自己接回来。
    const exec = vi.fn(async (id: string) => {
      if (id === 'r6') throw new Error('SESSION_NOT_FOUND: SSH 会话不存在或已断开')
      return { stdout: '/home/ckun/www\n', stderr: '', exitCode: 0 }
    })
    const getCwd = vi.fn(async (id: string) => (id === 'r6' ? '' : '/home/ckun/www'))
    const connect = vi.fn(async () => ({ id: 'r6b', name: 'prod' }))
    const disconnect = vi.fn(async () => undefined)
    g.window = { electronAPI: { sshGetCwd: getCwd, sshExec: exec, sshConnect: connect, sshDisconnect: disconnect } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.connections = [{ id: 'c1', name: 'prod', host: '10.0.0.1', port: 22, username: 'root' } as never]
    ssh.sessions = [{ id: 'r6', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', connectionId: 'c1' }]
    ssh.activeSessionId = 'r6'

    await vi.waitFor(() => expect(connect).toHaveBeenCalledTimes(1))
    // 新连接顶在同一个标签位上，不是另开一个：会话数不变、active 跟着换 id
    await vi.waitFor(() => expect(ssh.activeSessionId).toBe('r6b'))
    expect(ssh.sessions.length).toBe(1)
    expect(ssh.sessions[0].unavailable).toBeFalsy()
    expect(disconnect).toHaveBeenCalledWith('r6')

    // 回拨成功后 chip 立刻有目录，不必等终端页 OSC 7
    await vi.waitFor(() => expect(landing.activeDirLabel).toBe('www'))
    expect(landing.activeDirUnavailable).toBe(false)

    // 同一轮不再重复回拨（失败会让用户自己点「重新连接…」，不自动刷）
    ssh.activeSessionId = ''
    ssh.activeSessionId = 'r6b'
    await Promise.resolve()
    expect(connect).toHaveBeenCalledTimes(1)
  })

  it('回拨失败时把原因写进落点 error，chip 仍说「未连接」', async () => {
    const exec = vi.fn(async () => { throw new Error('SESSION_NOT_FOUND: SSH 会话不存在或已断开') })
    const connect = vi.fn(async () => { throw new Error('认证失败') })
    g.window = { electronAPI: { sshGetCwd: async () => '', sshExec: exec, sshConnect: connect, sshDisconnect: async () => undefined } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.connections = [{ id: 'c2', name: 'prod', host: '10.0.0.2', port: 22, username: 'root' } as never]
    ssh.sessions = [{ id: 'r7', name: 'prod', type: 'remote', shell: 'root@10.0.0.2', connectionId: 'c2' }]
    ssh.activeSessionId = 'r7'

    await vi.waitFor(() => expect(connect).toHaveBeenCalled())
    await vi.waitFor(() => expect(landing.error).toContain('认证失败'))
    expect(landing.activeDirLabel).toBe('未连接')
    expect(ssh.sessions[0].id).toBe('r7')
  })

  it('重连已放弃的会话直接说「未连接」，不发探测', () => {
    const exec = vi.fn(async () => ({ stdout: '/x\n', stderr: '', exitCode: 0 }))
    g.window = { electronAPI: { sshGetCwd: async () => '', sshExec: exec } }
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'r5', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', reconnectGaveUp: true }]
    ssh.activeSessionId = 'r5'
    expect(landing.activeDirLabel).toBe('未连接')
    expect(exec).not.toHaveBeenCalled()
  })

  it('activeRemoteLabel：连接名优先，退到 username@host，非远程落点为空', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    expect(landing.activeRemoteLabel).toBe('')

    ssh.connections = [{
      id: 'c1', name: '生产库', host: '10.0.0.1', port: 22, username: 'root',
      authType: 'password', tags: [], createdAt: 0, updatedAt: 0,
    }]
    ssh.sessions = [{ id: 'r1', name: 'prod', type: 'remote', shell: 'root@10.0.0.1', connectionId: 'c1' }]
    ssh.activeSessionId = 'r1'
    expect(landing.activeRemoteLabel).toBe('生产库')

    // 没有对应连接记录（连接被删/手工开的会话）时退到会话自带的 username@host
    ssh.sessions = [{ id: 'r2', name: 'x', type: 'remote', shell: 'dev@10.0.0.2' }]
    ssh.activeSessionId = 'r2'
    expect(landing.activeRemoteLabel).toBe('dev@10.0.0.2')

    // 本地落点不进这个读数（本地 chip 只写「本地」）
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    expect(landing.activeRemoteLabel).toBe('')
  })
})

describe('工作树落点', () => {
  it('以 worktree 路径为 cwd 开本地 PTY，并把落点标成 worktree', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `wt-${cwd}`
      ssh.sessions = [...ssh.sessions, { id, name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
      ssh.activeSessionId = id
      return id
    })

    const w = worktree('/repo-wt-feature', 'feature/x')
    await landing.chooseWorktree(w)

    expect(create).toHaveBeenCalledWith('PowerShell', '/repo-wt-feature')
    expect(landing.kind).toBe('worktree')
    expect(landing.activeWorktree?.branch).toBe('feature/x')
    // 工作树态才长第三枚分支 chip
    expect(landing.showBranch).toBe(true)
  })

  it('chooseWorktreeDefault：不带参数就落到当前仓库的主工作树，不必先挑分支', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `wt-${cwd}`
      ssh.sessions = [...ssh.sessions, { id, name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
      ssh.activeSessionId = id
      return id
    })
    const main: GitWorktree = { ...worktree('/repo', 'main'), main: true }
    g.window = {
      electronAPI: {
        gitreposGetRegistry: async () => ({ version: 2, local: ['/repo'], remote: {}, sync: {} }),
        // 主工作树排在列表第二枚：默认选的是 main，不是「拿第一枚凑数」
        gitreposListWorktrees: async () => [worktree('/repo-wt-dev', 'dev'), main],
      },
    }
    await landing.loadWorktrees()
    await landing.chooseWorktreeDefault()

    expect(create).toHaveBeenCalledWith('PowerShell', '/repo')
    expect(landing.kind).toBe('worktree')
    expect(landing.activeWorktree?.branch).toBe('main')
    expect(landing.error).toBe('')
  })

  it('chooseWorktreeDefault：没有登记仓库时给可行动的提示，不静默也不改落点', async () => {
    const landing = useExecTargetStore()
    await landing.chooseWorktreeDefault()
    expect(landing.error).toContain('登记本机仓库')
    expect(landing.kind).toBeNull()
  })

  it('同一工作树复用已开会话，不重复堆标签', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `wt-${cwd}`
      ssh.sessions = [...ssh.sessions, { id, name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
      ssh.activeSessionId = id
      return id
    })

    const w = worktree('/repo-wt')
    await landing.chooseWorktree(w)
    ssh.activeSessionId = ''
    await landing.chooseWorktree(w)
    expect(create).toHaveBeenCalledTimes(1)
    expect(landing.kind).toBe('worktree')
  })
})

describe('工作树列表', () => {
  it('单仓探测失败只留该仓原因，其它仓库照常出列表', async () => {
    const landing = useExecTargetStore()
    g.window = {
      electronAPI: {
        gitreposGetRegistry: async () => ({ version: 2, local: ['/a', '/b'], remote: {}, sync: {} }),
        gitreposListWorktrees: async (p: string) => {
          if (p === '/b') throw new Error('不是 git 仓库')
          return [worktree('/a', 'main')]
        },
      },
    }
    await landing.loadWorktrees()
    expect(landing.groups).toHaveLength(2)
    expect(landing.groups[0].items).toHaveLength(1)
    expect(landing.groups[1].items).toEqual([])
    expect(landing.groups[1].error).toContain('不是 git 仓库')
  })

  it('没登记仓库时给出可行动的提示，不静默空列表', async () => {
    const landing = useExecTargetStore()
    g.window = {
      electronAPI: {
        gitreposGetRegistry: async () => ({ version: 2, local: [], remote: {}, sync: {} }),
        gitreposListWorktrees: async () => [],
      },
    }
    await landing.loadWorktrees()
    expect(landing.error).toContain('登记本机仓库')
  })
})

describe('顶栏仓库 / 分支 chip 的读数', () => {
  it('默认取第一个仓库的短名与主分支，切仓库后 activeGroup 跟着变', async () => {
    const landing = useExecTargetStore()
    g.window = {
      electronAPI: {
        gitreposGetRegistry: async () => ({
          version: 2,
          local: ['D:\\workspace\\cKunMTIE', 'D:\\workspace\\another'],
          remote: {}, sync: {},
        }),
        gitreposListWorktrees: async (p: string) => p.includes('cKunMTIE')
          ? [worktree('D:\\workspace\\cKunMTIE', 'main')]
          : [worktree('D:\\workspace\\wt-dev', 'dev')],
      },
    }
    await landing.loadWorktrees()

    expect(landing.activeRepoLabel).toBe('cKunMTIE')
    expect(landing.activeBranch).toBe('main')
    landing.selectRepo('D:\\workspace\\another')
    expect(landing.activeRepoLabel).toBe('another')
    expect(landing.activeBranch).toBe('dev')
  })

  it('没有仓库时读数不谎报：短名空、分支显示占位', () => {
    const landing = useExecTargetStore()
    expect(landing.activeRepoLabel).toBe('')
    expect(landing.activeBranch).toBe('—')
  })
})

describe('连接 WSL', () => {
  it('走 createLocalTerminal(WSL)，启动失败把原因留在 error', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = vi.spyOn(ssh, 'createLocalTerminal').mockResolvedValue('wsl-1')
    await landing.chooseWsl()
    expect(create).toHaveBeenCalledWith('WSL')
    expect(landing.error).toBe('')

    create.mockRejectedValueOnce(new Error('WSL 未安装发行版'))
    await landing.chooseWsl()
    expect(landing.error).toContain('WSL 未安装发行版')
  })
})

describe('目录记录（目录面的候选）', () => {
  /** createLocalTerminal 在 node 环境没有 electronAPI：直接给一条会话，模拟落点切换。 */
  function stubCreate(ssh: ReturnType<typeof useSSHStore>) {
    return vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `l-${cwd ?? 'home'}`
      ssh.sessions = [...ssh.sessions, { id, name: 'PowerShell', type: 'local', shell: 'PowerShell', cwd }]
      ssh.activeSessionId = id
      return id
    })
  }

  it('默认没有任何记录：不预读会话 / 工作树 / 仓库，目录面空着', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [
      { id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell', cwd: 'D:\\work\\a' },
      { id: 'l2', name: 'PowerShell', type: 'local', shell: 'PowerShell' },
    ]
    ssh.activeSessionId = 'l2'
    // 有会话不等于有记录：记录只由「用户打开过目录」写入
    expect(landing.recentDirs).toEqual([])
  })

  it('打开目录才记一条：最近在前、同一目录不重复', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = stubCreate(ssh)

    await landing.chooseLocalDir('D:\\work\\a')
    await landing.chooseLocalDir('D:\\work\\b')
    // 再选回 a：复用那个会话，不新开终端，也不落第二条记录
    await landing.chooseLocalDir('D:\\work\\a')

    expect(create).toHaveBeenCalledTimes(2)
    expect(landing.recentDirs).toEqual(['D:\\work\\a', 'D:\\work\\b'])
  })

  it('删掉一条记录：只摘候选，那个目录里正跑着的会话不动', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    stubCreate(ssh)
    await landing.chooseLocalDir('D:\\work\\a')
    await landing.chooseLocalDir('D:\\work\\b')

    landing.removeRecentDir('D:\\work\\a')

    expect(landing.recentDirs).toEqual(['D:\\work\\b'])
    expect(ssh.sessions.some(s => s.cwd === 'D:\\work\\a')).toBe(true)
    // 删光了就回到空列表（不是又冒出一批预读目录）
    landing.removeRecentDir('D:\\work\\b')
    expect(landing.recentDirs).toEqual([])
  })

  it('同一目录的不同写法算一条：斜杠方向 / 尾斜杠 / 盘符大小写都不该多出一行', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    const create = stubCreate(ssh)

    await landing.chooseLocalDir('D:\\work\\a')
    // 同目录、不同写法：不新建终端，也不新增记录（只把这条提到最前、留下最新写法）
    await landing.chooseLocalDir('d:/work/a/')
    expect(create).toHaveBeenCalledTimes(1)
    expect(landing.recentDirs).toHaveLength(1)

    // 删除同样按归一后的键匹配：不管当前写法是哪种都能删掉那条记录
    landing.removeRecentDir('D:/work/A')
    expect(landing.recentDirs).toEqual([])
  })

  it('activeBoundDir 只认绑定目录：不在项目中的会话上报主目录也不算', () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    ssh.sessions = [{ id: 'l1', name: 'PowerShell', type: 'local', shell: 'PowerShell' }]
    ssh.activeSessionId = 'l1'
    // 终端自己同步上来的 cwd 是主目录：它是终端在哪，不是用户选了哪个项目
    ssh.setTerminalCwd('l1', 'C:\\Users\\ckun')
    expect(landing.activeBoundDir).toBe('')
    expect(landing.activeDirLabel).toBe('')

    ssh.sessions = [{ id: 'l2', name: 'PowerShell', type: 'local', shell: 'PowerShell', cwd: 'D:\\work\\a' }]
    ssh.activeSessionId = 'l2'
    expect(landing.activeBoundDir).toBe('D:\\work\\a')
  })
})

describe('新建工作树与清理（Trae 式任务驱动）', () => {
  function apiMock(overrides: Record<string, unknown> = {}) {
    return {
      gitreposGetRegistry: async () => ({ version: 2, local: ['/repo'], remote: {}, sync: {} }),
      gitreposListWorktrees: async () => [worktree('/repo', 'main')],
      gitreposCreateWorktree: async (_p: string, branch: string) => worktree(`/repo-wt-${branch}`, branch),
      gitreposPruneWorktrees: async () => ({ code: 0, stdout: '', stderr: '' }),
      ...overrides,
    }
  }

  it('createWorktree：建完刷新列表并立刻把落点切到新分支', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'createLocalTerminal').mockImplementation(async (_shell, cwd) => {
      const id = `wt-${cwd}`
      ssh.sessions = [...ssh.sessions, { id, name: 'Term', type: 'local', shell: 'Term' }]
      ssh.activeSessionId = id
      return id
    })
    g.window = { electronAPI: apiMock({
      gitreposListWorktrees: async () => [worktree('/repo', 'main'), worktree('/repo-wt-feat-ai-1', 'feat/ai-1')],
    }) }
    await landing.loadWorktrees()

    const wt = await landing.createWorktree('/repo', 'feat/ai-1')
    expect(wt?.branch).toBe('feat/ai-1')
    expect(landing.kind).toBe('worktree')
    expect(landing.activeWorktree?.branch).toBe('feat/ai-1')
    expect(landing.activeRepoPath).toBe('/repo')
    expect(landing.error).toBe('')
  })

  it('createWorktree：失败（分支重名）落 error 且不切落点', async () => {
    const ssh = useSSHStore()
    const landing = useExecTargetStore()
    vi.spyOn(ssh, 'createLocalTerminal').mockResolvedValue('wt-x')
    g.window = { electronAPI: apiMock({
      gitreposCreateWorktree: async () => { throw new Error('分支已存在: feat/x') },
    }) }
    await landing.loadWorktrees()

    const wt = await landing.createWorktree('/repo', 'feat/x')
    expect(wt).toBeNull()
    expect(landing.error).toContain('分支已存在')
    expect(landing.kind).not.toBe('worktree')
  })

  it('pruneWorktrees：成功后刷新列表，失败落 error', async () => {
    const landing = useExecTargetStore()
    g.window = { electronAPI: apiMock() }
    await landing.loadWorktrees()
    expect(await landing.pruneWorktrees('/repo')).toBe(true)
    expect(landing.error).toBe('')

    g.window = { electronAPI: apiMock({
      gitreposPruneWorktrees: async () => { throw new Error('prune 失败') },
    }) }
    expect(await landing.pruneWorktrees('/repo')).toBe(false)
    expect(landing.error).toContain('prune 失败')
  })
})
