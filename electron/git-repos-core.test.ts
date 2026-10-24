/**
 * git-repos-core 解析与命令构造测试。
 * 解析 fixture 逐字取自 `git status --porcelain=v1 -b` / `git branch -vv` /
 * `git log --format=%H%x1f…` 的真实输出形态（含 \x1f 字段分隔与 \x1e 段分隔）。
 */
import { describe, expect, it } from 'vitest'
import {
  parseGitStatusPorcelain, parseBranchVv, parseGitLog, parseOriginUrl,
  repoNameOf, repoRowId, repoRowKind, sortRepoRows, toLocalRepoRow,
  isValidRemoteRepoPath, shSingleQuote, REMOTE_SEP,
  buildRemoteRepoStatusCmd, buildRemoteRepoDetailCmd, buildRemoteRepoVerifyCmd,
  buildRemoteGitActionCmd, splitBySep, toRemoteRepoRow, parseRepoDetailSections,
  interpretPull, firstLine, friendlyGitStderr,
  GRAPH_MAX_LIMIT, buildGraphLogArgs, buildRemoteGraphCmd,
  parseGraphLog, graphRefKind, graphHasMore,
  type RepoRow,
} from '../shared/git-repos-core'

describe('parseGitStatusPorcelain', () => {
  it('正常头行：分支 + 上游 + ahead/behind + 文件行', () => {
    const raw = [
      '## main...origin/main [ahead 1, behind 2]',
      ' M backend/app.py',
      'A  new-file.ts',
      '?? untracked.txt',
      'R  old-name.ts -> new-name.ts',
    ].join('\n')
    const s = parseGitStatusPorcelain(raw)
    expect(s.branch).toBe('main')
    expect(s.upstream).toBe('origin/main')
    expect(s.ahead).toBe(1)
    expect(s.behind).toBe(2)
    expect(s.files).toHaveLength(4)
    expect(s.files[0]).toEqual({ status: 'M', path: 'backend/app.py' })
    expect(s.files[1]).toEqual({ status: 'A', path: 'new-file.ts' })
    expect(s.files[2]).toEqual({ status: '??', path: 'untracked.txt' })
    // 重命名归一到新路径
    expect(s.files[3]).toEqual({ status: 'R', path: 'new-name.ts' })
  })

  it('仅落后（behind only）', () => {
    const s = parseGitStatusPorcelain('## main...origin/main [behind 3]')
    expect(s.ahead).toBe(0)
    expect(s.behind).toBe(3)
    expect(s.files).toHaveLength(0)
  })

  it('无上游：## main 不带点点', () => {
    const s = parseGitStatusPorcelain('## main')
    expect(s.branch).toBe('main')
    expect(s.upstream).toBeNull()
  })

  it('detached HEAD', () => {
    const s = parseGitStatusPorcelain('## HEAD (no branch)')
    expect(s.detached).toBe(true)
    expect(s.branch).toBe('HEAD')
  })

  it('core.quotePath=false：中文路径原样保留（不做八进制转义）', () => {
    const s = parseGitStatusPorcelain('## main\n?? 中文目录/说明.txt\nR  旧名.md -> 新名.md')
    expect(s.files.map(f => f.path)).toEqual(['中文目录/说明.txt', '新名.md'])
  })

  it('初始仓库（No commits yet）', () => {
    const s = parseGitStatusPorcelain('## No commits yet on main\n?? a.txt')
    expect(s.branch).toBe('main')
    expect(s.files).toHaveLength(1)
  })
})

describe('parseBranchVv', () => {
  it('当前分支带上游与 ahead/behind，非当前分支无上游', () => {
    const raw = [
      '* dev    a1b2c4d [origin/dev: ahead 1, behind 2] feat: x',
      '  main   d4e5f6a [origin/main] fix: y',
      '  wip    789abcd chore',
    ].join('\n')
    const bs = parseBranchVv(raw)
    expect(bs).toHaveLength(3)
    expect(bs[0]).toEqual({ name: 'dev', upstream: 'origin/dev', ahead: 1, behind: 2, current: true })
    expect(bs[1]).toEqual({ name: 'main', upstream: 'origin/main', ahead: 0, behind: 0, current: false })
    expect(bs[2]).toEqual({ name: 'wip', upstream: null, ahead: null, behind: null, current: false })
  })

  it('上游被删（gone）：upstream 归一，ahead/behind 保持 null', () => {
    const bs = parseBranchVv('* dead  a1b2c4d [origin/dead: gone] topic')
    expect(bs[0].upstream).toBe('origin/dead')
    expect(bs[0].ahead).toBeNull()
    expect(bs[0].behind).toBeNull()
  })
})

describe('parseGitLog', () => {
  it('按 \\x1f 切字段，跳过残行', () => {
    const raw = ['e511be5\x1ffix: a\x1fckun\x1f2 hours ago', '9fabfa2\x1fdocs: b\x1fckun\x1f3 days ago', 'broken-line', ''].join('\n')
    const cs = parseGitLog(raw)
    expect(cs).toHaveLength(2)
    expect(cs[0]).toEqual({ hash: 'e511be5', subject: 'fix: a', author: 'ckun', relTime: '2 hours ago' })
  })
})

describe('parseOriginUrl', () => {
  it('https / scp / ssh 带端口三种写法', () => {
    expect(parseOriginUrl('https://github.com/ckun/repo.git')).toEqual({ host: 'github.com', slug: 'ckun/repo' })
    expect(parseOriginUrl('git@github.com:ckun/repo.git')).toEqual({ host: 'github.com', slug: 'ckun/repo' })
    expect(parseOriginUrl('ssh://git@gitlab.internal:2222/ops/app.git')).toEqual({ host: 'gitlab.internal', slug: 'ops/app' })
    expect(parseOriginUrl('https://gitee.com/team/work-report')).toEqual({ host: 'gitee.com', slug: 'team/work-report' })
  })
  it('识别不了的返回 null（不猜）', () => {
    expect(parseOriginUrl('/local/path')).toBeNull()
    expect(parseOriginUrl('')).toBeNull()
    expect(parseOriginUrl('not a url')).toBeNull()
  })
})

describe('行组装与排序', () => {
  it('repoNameOf 认两种分隔符', () => {
    expect(repoNameOf('D:\\workspace\\cKunAgentSSH')).toBe('cKunAgentSSH')
    expect(repoNameOf('/var/www/app/')).toBe('app')
  })
  it('repoRowId 区分 scope', () => {
    expect(repoRowId('local', undefined, 'D:\\x')).toBe('local:D:\\x')
    expect(repoRowId('remote', 's1', '/var/www/app')).toBe('remote:s1:/var/www/app')
  })
  it('repoRowKind 优先级：取数失败 > 脏区 > 分歧 > 落后 > 领先 > 无远端 > 干净', () => {
    expect(repoRowKind({ error: 'fatal: not a git repository', dirtyFiles: 2, ahead: 1, behind: 1, originUrl: 'x' })).toBe('error')
    expect(repoRowKind({ dirtyFiles: 2, ahead: 0, behind: 0, originUrl: 'x' })).toBe('dirty')
    expect(repoRowKind({ dirtyFiles: 0, ahead: 1, behind: 2, originUrl: 'x' })).toBe('diverge')
    expect(repoRowKind({ dirtyFiles: 0, ahead: 0, behind: 3, originUrl: 'x' })).toBe('behind')
    expect(repoRowKind({ dirtyFiles: 0, ahead: 1, behind: 0, originUrl: 'x' })).toBe('ahead')
    expect(repoRowKind({ dirtyFiles: 0, ahead: 0, behind: 0, originUrl: null })).toBe('none')
    expect(repoRowKind({ dirtyFiles: 0, ahead: 0, behind: 0, originUrl: 'x' })).toBe('clean')
  })
  it('sortRepoRows 异常优先，同档按名称', () => {
    const mk = (name: string, d: number, a: number, b: number): RepoRow => ({
      id: name, scope: 'local', name, path: name, branch: 'main', detached: false,
      upstream: null, ahead: a, behind: b, dirtyFiles: d, originUrl: 'u',
      originHost: 'h', originSlug: 's', lastCommit: null,
    })
    const broken: RepoRow = { ...mk('broken', 0, 0, 0), error: 'fatal: not a git repository' }
    const sorted = sortRepoRows([mk('zeta', 0, 0, 0), mk('beta', 1, 0, 0), mk('alpha', 0, 0, 2), mk('gamma', 0, 0, 0), broken])
    expect(sorted.map(r => r.name)).toEqual(['broken', 'beta', 'alpha', 'gamma', 'zeta'])
  })
  it('toLocalRepoRow 组装本地行', () => {
    const row = toLocalRepoRow({
      path: 'D:\\workspace\\demo',
      ok: true,
      status: parseGitStatusPorcelain('## main...origin/main [behind 2]\n M a.py'),
      lastCommit: { hash: 'h1', subject: 's', author: 'ckun', relTime: '1 hour ago' },
      originUrl: 'git@github.com:ckun/demo.git',
    })
    expect(row.name).toBe('demo')
    expect(row.scope).toBe('local')
    expect(row.behind).toBe(2)
    expect(row.dirtyFiles).toBe(1)
    expect(row.originHost).toBe('github.com')
    expect(row.originSlug).toBe('ckun/demo')
    expect(row.id).toBe('local:D:\\workspace\\demo')
  })
})

describe('远程路径校验与命令构造', () => {
  it('isValidRemoteRepoPath：绝对路径可过，引号/控制字符/相对路径拒绝', () => {
    expect(isValidRemoteRepoPath('/var/www/app')).toBe(true)
    expect(isValidRemoteRepoPath('/home/deploy/my app')).toBe(true)
    expect(isValidRemoteRepoPath('var/www/app')).toBe(false)
    expect(isValidRemoteRepoPath("/var/x'; rm -rf /")).toBe(false)
    expect(isValidRemoteRepoPath('/var/x\nrm')).toBe(false)
    expect(isValidRemoteRepoPath(`/${'a'.repeat(600)}`)).toBe(false)
  })
  it('shSingleQuote：含单引号的路径不出引用域', () => {
    expect(shSingleQuote('/plain/path')).toBe("'/plain/path'")
    // x'y → 'x'\''y'：三段拼接后仍是字面量，无法闭引号注入
    expect(shSingleQuote("x'$(rm -rf /)y")).toBe("'x'\\''$(rm -rf /)y'")
  })
  it('status 命令三段以 printf 分隔符衔接，并关闭 quotePath', () => {
    const cmd = buildRemoteRepoStatusCmd('/var/www/app')
    expect(cmd).toContain("git -C '/var/www/app' -c core.quotePath=false status --porcelain=v1 -b --ignore-submodules=dirty")
    expect(cmd).toContain(`printf '\\x1e'`)
    expect(cmd).toContain("git -C '/var/www/app' log -1 --format=")
    expect(cmd).toContain("git -C '/var/www/app' config --get remote.origin.url")
    expect(cmd.split(' ; ')).toHaveLength(5)
  })
  it('detail / verify / fetch / pull 命令', () => {
    expect(buildRemoteRepoDetailCmd('/srv/api')).toContain("-c core.quotePath=false status")
    expect(buildRemoteRepoDetailCmd('/srv/api')).toContain('branch -vv')
    expect(buildRemoteRepoDetailCmd('/srv/api')).toContain('log -20')
    expect(buildRemoteRepoVerifyCmd('/srv/api')).toBe("git -C '/srv/api' rev-parse --is-inside-work-tree")
    expect(buildRemoteGitActionCmd('/srv/api', 'fetch')).toBe("git -C '/srv/api' fetch --all --prune")
    expect(buildRemoteGitActionCmd('/srv/api', 'pull')).toBe("git -C '/srv/api' pull --ff-only")
  })
})

describe('远程回包解析', () => {
  const sep = REMOTE_SEP
  it('三段齐全 → 完整行', () => {
    const stdout = [
      '## main...origin/main [ahead 1, behind 2]',
      `d3c4b5a\x1ffeats: 指标导出\x1flin\x1fyesterday`,
      'git@github.com:team/data-api.git',
    ].join(sep)
    const row = toRemoteRepoRow('s1', '/srv/data-api', stdout, '')
    expect(row.scope).toBe('remote')
    expect(row.sessionId).toBe('s1')
    expect(row.behind).toBe(2)
    expect(row.originHost).toBe('github.com')
    expect(row.lastCommit?.subject).toBe('feats: 指标导出')
    expect(row.error).toBeUndefined()
  })
  it('回包分段缺失（命令整体失败）→ 行级 error', () => {
    const row = toRemoteRepoRow('s1', '/no/such/repo', 'fatal: not a git repository', "fatal: not a git repository (or any of the parent directories): .git")
    expect(row.error).toContain('not a git repository')
  })
  it('空仓库：status 段有头行无文件，log 段为空 → lastCommit 为 null 而非报错', () => {
    const stdout = ['## No commits yet on main', '', ''].join(sep)
    const row = toRemoteRepoRow('s1', '/opt/empty', stdout, '')
    expect(row.error).toBeUndefined()
    expect(row.branch).toBe('main')
    expect(row.lastCommit).toBeNull()
  })
  it('splitBySep 与 parseRepoDetailSections', () => {
    expect(splitBySep(`a${sep}b${sep}c`)).toEqual(['a', 'b', 'c'])
    const d = parseRepoDetailSections(
      ' M x.py\n?? y.txt',
      '* dev  a1b2c4d [origin/dev: ahead 1] s',
      `h1\x1fmsg\x1fckun\x1f2 hours ago`,
    )
    expect(d.files).toHaveLength(2)
    expect(d.branches[0].name).toBe('dev')
    expect(d.commits[0].hash).toBe('h1')
  })
})

describe('interpretPull（--ff-only 安全边界）', () => {
  it('exit 0 + Already up to date → up-to-date', () => {
    expect(interpretPull('Already up to date.', '', 0).kind).toBe('up-to-date')
  })
  it('exit 0 + Fast-forward → fast-forwarded', () => {
    expect(interpretPull('Updating a..b\nFast-forward\n 1 file changed', '', 0).kind).toBe('fast-forwarded')
  })
  it('分叉 → diverged（提示手动处理，不自动合并）', () => {
    const out = interpretPull('', 'fatal: Not possible to fast-forward, aborting.', 128)
    expect(out.kind).toBe('diverged')
    expect(out.message).toContain('--ff-only')
  })
  it('其他失败 → failed，取 stderr 首行', () => {
    const out = interpretPull('', 'fatal: Authentication failed\nsecond line', 128)
    expect(out.kind).toBe('failed')
    expect(out.message).toBe('fatal: Authentication failed')
  })
  it('git-not-found 哨兵 → 友好文案而不是黑话', () => {
    const out = interpretPull('', 'git-not-found', -1)
    expect(out.kind).toBe('failed')
    expect(out.message).toBe('未检测到 git（请安装 git 或加入 PATH 后重试）')
    expect(friendlyGitStderr('fatal: x')).toBe('fatal: x')
  })
  it('firstLine 取首个非空行', () => {
    expect(firstLine('  \n\nfatal: x\nnext')).toBe('fatal: x')
    expect(firstLine('')).toBe('')
  })
})

describe('提交图谱（--graph --all 解析与命令构造）', () => {
  /** 逐字对照 `git log --graph --all --format=%x01%H%x1f…` 的真实输出形态 */
  const raw = [
    `* \x01e511be5aaa\x1ffix: a\x1fckun\x1f2 hours ago\x1fHEAD -> main, origin/main, tag: v1.0`,
    `|\\`,
    `| * \x01a1b2c3ddd\x1ffeat: b\x1flin\x1fyesterday\x1forigin/dev`,
    `* | \x019fabfa2eee\x1fdocs: c\x1fckun\x1f3 days ago\x1f`,
    `|/`,
    `* \x01d4e5f6accc\x1finit\x1fckun\x1f1 month ago\x1f`,
  ].join('\n')

  it('parseGraphLog：提交行切出图谱列/字段/引用，连接线行保留拓扑', () => {
    const rows = parseGraphLog(raw)
    expect(rows).toHaveLength(6)
    expect(rows[0]).toEqual({
      graph: '* ',
      commit: { hash: 'e511be5aaa', subject: 'fix: a', author: 'ckun', relTime: '2 hours ago' },
      refs: ['HEAD -> main', 'origin/main', 'tag: v1.0'],
    })
    // 连接线行（合叉的竖线/斜线段）：commit 为 null 但图谱列保留
    expect(rows[1]).toEqual({ graph: '|\\', commit: null, refs: [] })
    expect(rows[2].graph).toBe('| * ')
    expect(rows[2].refs).toEqual(['origin/dev'])
    expect(rows[3].graph).toBe('* | ')
    expect(rows[3].refs).toEqual([])
    expect(rows[4].commit).toBeNull()
    expect(rows[5].commit?.subject).toBe('init')
  })

  it('graphRefKind：HEAD / 本地 / 远端 / 标签四档', () => {
    expect(graphRefKind('HEAD -> main')).toBe('head')
    expect(graphRefKind('main')).toBe('local')
    expect(graphRefKind('origin/main')).toBe('remote')
    expect(graphRefKind('tag: v1.0')).toBe('tag')
  })

  it('graphHasMore：打满 limit 且未触顶才有更多', () => {
    const rows = parseGraphLog(raw) // 4 条提交
    expect(graphHasMore(rows, 4)).toBe(true)
    expect(graphHasMore(rows, 10)).toBe(false)
    // limit 触到 500 上限：即便打满也不再提供加载更多
    expect(graphHasMore(rows, 500)).toBe(false)
  })

  it('buildGraphLogArgs：参数形态与 limit 收口（默认 30，上限 500）', () => {
    expect(buildGraphLogArgs(30)).toEqual(['log', '-n', '30', '--graph', '--all', '--format=%x01%H%x1f%s%x1f%an%x1f%cr%x1f%D'])
    expect(buildGraphLogArgs(0)[2]).toBe('30')
    expect(buildGraphLogArgs(9999)[2]).toBe(String(GRAPH_MAX_LIMIT))
  })

  it('buildRemoteGraphCmd：路径单引号包裹 + 同一套参数', () => {
    const cmd = buildRemoteGraphCmd('/srv/api', 50)
    expect(cmd).toBe("git -C '/srv/api' log -n 50 --graph --all --format='%x01%H%x1f%s%x1f%an%x1f%cr%x1f%D'")
  })
})
