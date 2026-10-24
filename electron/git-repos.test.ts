/**
 * GitReposService 集成测试：注册表读改写 / 登记校验 / 真实 git 仓库取数。
 * git 命令在本机真实执行（spawn 一个临时仓库），覆盖「添加仓库」全链路 ——
 * 渲染层报过的登记错误在这里按同样路径复现。
 */
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

let userDataDir = ''
vi.mock('electron', () => ({
  app: { getPath: () => userDataDir },
}))

// 服务要在 electron mock 之后导入（其 getStorePath 读 mock 的 userData）
const { GitReposService } = await import('./git-repos')
const { parseGraphLog } = await import('../shared/git-repos-core')

let svc: InstanceType<typeof GitReposService>
let workDir = ''
let repoA = ''
let repoB = ''

/** 造一个真实 git 仓库（含一次提交），失败说明环境缺 git —— 测试本身就该红 */
function makeRepo(path: string, file: string): void {
  mkdirSync(path, { recursive: true })
  const git = (args: string[]) => execFileSync('git', ['-C', path, ...args], { stdio: 'pipe' })
  git(['init', '-b', 'main'])
  git(['config', 'user.email', 't@t'])
  git(['config', 'user.name', 't'])
  writeFileSync(join(path, file), 'hello\n')
  git(['add', file])
  git(['commit', '-m', 'init commit'])
}

beforeAll(() => {
  userDataDir = mkdtempSync(join(tmpdir(), 'gitrepos-userdata-'))
  workDir = mkdtempSync(join(tmpdir(), 'gitrepos-work-'))
  repoA = join(workDir, 'repo-a')
  repoB = join(workDir, 'nested', 'repo-b')
  makeRepo(repoA, 'a.txt')
  makeRepo(repoB, 'b.txt')
  svc = new GitReposService()
})

afterAll(() => {
  rmSync(userDataDir, { recursive: true, force: true })
  rmSync(workDir, { recursive: true, force: true })
})

describe('GitReposService 登记链路（添加仓库）', () => {
  it('空注册表出厂', async () => {
    const reg = await svc.getRegistry()
    expect(reg).toEqual({ version: 1, local: [], remote: {} })
  })

  it('addLocal：合法仓库登记并持久化', async () => {
    const reg = await svc.addLocal(repoA)
    expect(reg.local).toEqual([repoA])
    // 持久化校验：新实例从盘上读回同一份
    const again = await new GitReposService().getRegistry()
    expect(again.local).toEqual([repoA])
  })

  it('addLocal：重复登记幂等', async () => {
    const reg = await svc.addLocal(repoA)
    expect(reg.local).toEqual([repoA])
  })

  it('addLocal：相对路径与无 .git 目录被拒', async () => {
    await expect(svc.addLocal('relative/path')).rejects.toThrow('绝对路径')
    await expect(svc.addLocal(workDir)).rejects.toThrow('不是 git 仓库')
  })

  it('removeLocal 仅从注册表移除', async () => {
    await svc.addLocal(repoA)
    const after = await svc.removeLocal(repoA)
    expect(after.local).toEqual([])
  })

  it('setRemotePaths：按 connectionId 记忆与清空', async () => {
    await svc.setRemotePaths('conn-1', ['/var/www/app'])
    const withEntry = await svc.getRegistry()
    expect(withEntry.remote['conn-1']).toEqual(['/var/www/app'])
    await svc.setRemotePaths('conn-1', [])
    const cleared = await svc.getRegistry()
    expect(cleared.remote['conn-1']).toBeUndefined()
  })
})

describe('GitReposService 真实取数（statusAll / detail / fetch）', () => {
  it('statusAll：真实仓库解析出分支/提交/干净状态', async () => {
    const results = await svc.statusAll([repoB])
    expect(results).toHaveLength(1)
    const r = results[0]
    expect(r.ok).toBe(true)
    expect(r.error).toBeUndefined()
    expect(r.status?.branch).toBe('main')
    expect(r.status?.files).toHaveLength(0)
    expect(r.lastCommit?.subject).toBe('init commit')
    expect(r.originUrl).toBeNull() // 未配 remote
  })

  it('statusAll：目录被删的仓库按行上浮错误，不拖垮整批', async () => {
    const ghost = join(workDir, 'ghost')
    makeRepo(ghost, 'g.txt')
    await svc.addLocal(ghost)
    rmSync(ghost, { recursive: true, force: true })
    const results = await svc.statusAll([ghost, repoB])
    expect(results).toHaveLength(2)
    expect(results[0].ok).toBe(false)
    expect(results[0].error).toBeTruthy()
    expect(results[1].ok).toBe(true)
    await svc.removeLocal(ghost)
  })

  it('detail：三段原始输出可被 parseRepoDetailSections 解析', async () => {
    const d = await svc.detail(repoB)
    expect(d.ok).toBe(true)
    const { parseRepoDetailSections } = await import('../shared/git-repos-core')
    const parsed = parseRepoDetailSections(d.statusRaw ?? '', d.branchRaw ?? '', d.logRaw ?? '')
    expect(parsed.files).toHaveLength(0)
    expect(parsed.branches[0].name).toBe('main')
    expect(parsed.branches[0].current).toBe(true)
    expect(parsed.commits[0].subject).toBe('init commit')
  })

  it('graphLog：真实仓库的分叉合并拓扑与引用装饰', async () => {
    // 造拓扑：feat 分支一提交 + 回 main 合并（--no-ff 保留合叉结构）
    const g = (args: string[]) => execFileSync('git', ['-C', repoA, ...args], { stdio: 'pipe' })
    g(['checkout', '-b', 'feat-x'])
    writeFileSync(join(repoA, 'f2.txt'), 'x\n')
    g(['add', 'f2.txt'])
    g(['commit', '-m', 'feat: x'])
    g(['checkout', 'main'])
    g(['merge', '--no-ff', 'feat-x', '-m', 'merge: feat-x'])
    const r = await svc.graphLog(repoA, 30)
    expect(r.code).toBe(0)
    const rows = parseGraphLog(r.stdout)
    expect(rows.length).toBeGreaterThan(2)
    // 连接线行（合叉）与提交行并存，且带 HEAD -> main 装饰
    expect(rows.some(row => row.commit === null)).toBe(true)
    expect(rows.some(row => row.commit?.subject === 'merge: feat-x')).toBe(true)
    expect(rows.some(row => row.refs.some(x => x.includes('main')))).toBe(true)
    g(['branch', '-d', 'feat-x'])
  })

  it('fetchOne：无 remote 的仓库静默成功（git 对 --all 无远端不报错）', async () => {
    const r = await svc.fetchOne(repoB)
    expect(r.code).toBe(0)
  })
})
