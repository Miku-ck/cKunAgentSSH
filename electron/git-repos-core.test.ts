/**
 * git-repos-core 解析与命令构造测试。
 * 解析 fixture 逐字取自 `git status --porcelain=v1 -b` / `git branch -vv` /
 * `git log --format=%H%x1f…` 的真实输出形态（含 \x1f 字段分隔与 \x1e 段分隔）。
 */
import { describe, expect, it } from 'vitest'
import {
  parseGitStatusPorcelain, parseBranchVv, parseGitLog, parseOriginUrl,
  parseRemoteConfig, pickRemote,
  isValidBranchName, isValidCommitMsg, isValidCommitFiles,
  buildRemoteSwitchCmd, buildRemoteCommitCmd, buildRemotePushCmd,
  interpretCommit, interpretPush,
  repoNameOf, repoRowId, repoRowKind, sortRepoRows, toLocalRepoRow,
  isValidRemoteRepoPath, shSingleQuote, REMOTE_SEP,
  buildRemoteRepoStatusCmd, buildRemoteRepoDetailCmd, buildRemoteRepoVerifyCmd,
  buildRemoteGitActionCmd, splitBySep, toRemoteRepoRow, parseRepoDetailSections,
  interpretPull, firstLine, friendlyGitStderr,
  GRAPH_MAX_LIMIT, buildGraphLogArgs, buildRemoteGraphCmd,
  parseGraphLog, graphRefKind, graphHasMore, layoutGraphLanes,
  draftCommitMessage, looksLikeNoiseFile,
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

describe('parseRemoteConfig / pickRemoteUrl（远端不再写死 origin）', () => {
  it('parseRemoteConfig：remote.名字.url 行 → 名字映射，同名多值取第一条', () => {
    const remotes = parseRemoteConfig([
      'remote.gitee.url https://gitee.com/t/a.git',
      'remote.origin.url git@github.com:ckun/a.git',
      'remote.origin.url https://mirror.example.com/a.git',
      'branch.master.remote gitee',
      'garbage line without sep',
    ].join('\n'))
    expect(remotes).toEqual({
      gitee: 'https://gitee.com/t/a.git',
      origin: 'git@github.com:ckun/a.git',
    })
  })
  it('pickRemote：upstream 指向的远端（最长名匹配）> origin > 字典序，返回名字与 URL', () => {
    // upstream gitee/master → 认 gitee，哪怕 origin 也在
    expect(pickRemote({ gitee: 'u1', origin: 'u2' }, 'gitee/master')).toEqual({ name: 'gitee', url: 'u1' })
    // remote 名含 / 的极端情况：最长名优先，不误配到 a
    expect(pickRemote({ a: 'u1', 'a/b': 'u2' }, 'a/b/main')).toEqual({ name: 'a/b', url: 'u2' })
    // upstream 指向已删除的远端 → 回落 origin
    expect(pickRemote({ origin: 'u2', zeta: 'u3' }, 'gone/master')).toEqual({ name: 'origin', url: 'u2' })
    // 无 upstream、无 origin → 字典序第一个（稳定）
    expect(pickRemote({ zeta: 'u3', alpha: 'u4' }, null)).toEqual({ name: 'alpha', url: 'u4' })
    expect(pickRemote({}, null)).toBeNull()
  })
  it('toRemoteRepoRow：非 origin 名字的远端同样落到 host 徽章', () => {
    const stdout = [
      '## main...gitee/main',
      `d3c4b5a\x1ffix: a\x1fckun\x1f2 hours ago`,
      'remote.gitee.url https://gitee.com/team/data-api.git',
    ].join(REMOTE_SEP)
    const row = toRemoteRepoRow('s1', '/srv/data-api', stdout, '')
    expect(row.originUrl).toBe('https://gitee.com/team/data-api.git')
    expect(row.remoteName).toBe('gitee')
    expect(row.originHost).toBe('gitee.com')
    expect(row.originSlug).toBe('team/data-api')
  })
})

describe('V2 写操作：校验 / 命令构造 / 结果解释', () => {
  it('isValidBranchName：常规名可过，选项注入 / 空白 / 引号 / 非法字符 / .. / .lock 拒绝', () => {
    expect(isValidBranchName('main')).toBe(true)
    expect(isValidBranchName('feat/oauth-login')).toBe(true)
    expect(isValidBranchName('release-1.2.3')).toBe(true)
    expect(isValidBranchName('')).toBe(false)
    expect(isValidBranchName('-oProxyCommand=calc')).toBe(false)
    expect(isValidBranchName('a b')).toBe(false)
    expect(isValidBranchName("a'b")).toBe(false)
    expect(isValidBranchName('a~b')).toBe(false)
    expect(isValidBranchName('a..b')).toBe(false)
    expect(isValidBranchName('a@{b}')).toBe(false)
    expect(isValidBranchName('v1.lock')).toBe(false)
    expect(isValidBranchName('feat/')).toBe(false)
  })

  it('isValidCommitMsg / isValidCommitFiles：空 / 超长 / 控制字符 / .. 段拒绝', () => {
    expect(isValidCommitMsg('fix: 登录超时')).toBe(true)
    expect(isValidCommitMsg('   ')).toBe(false)
    expect(isValidCommitMsg('a'.repeat(201))).toBe(false)
    expect(isValidCommitMsg('bad\x00msg')).toBe(false)
    expect(isValidCommitFiles(['src/a.py', '新建 目录/b.txt'])).toBe(true)
    expect(isValidCommitFiles([])).toBe(false)
    expect(isValidCommitFiles(['../escape'])).toBe(false)
    expect(isValidCommitFiles(['/abs/path'])).toBe(false)
    expect(isValidCommitFiles(['C:\\win\\path'])).toBe(false)
    expect(isValidCommitFiles(['ok.txt', ''])).toBe(false)
  })

  it('远程 switch / commit / push 命令：路径、分支名、提交信息、文件路径全部单引号包裹', () => {
    expect(buildRemoteSwitchCmd('/srv/app', 'release/1.2')).toBe("git -C '/srv/app' switch 'release/1.2'")
    expect(buildRemoteCommitCmd('/srv/app', ['a.py', '新 目录/b.txt'], "fix: it's ok")).toBe(
      "git -C '/srv/app' add -- 'a.py' '新 目录/b.txt' ; git -C '/srv/app' commit -m 'fix: it'\\''s ok' -- 'a.py' '新 目录/b.txt'",
    )
    expect(buildRemotePushCmd('/srv/app', { remoteName: 'gitee', branch: 'main' })).toBe(
      "git -C '/srv/app' push -u 'gitee' 'main'",
    )
  })

  it('interpretCommit：成功提取分支与短哈希；失败取 stderr 首行', () => {
    expect(interpretCommit('[main 8991dab] feat: x\n 1 file changed', '', 0)).toEqual({
      kind: 'committed', message: '已提交到 main 8991dab', hash: '8991dab',
    })
    // 首次提交形态：[main (root-commit) abc1234]
    expect(interpretCommit('[main (root-commit) abc12345] init\n 1 file changed', '', 0).message).toBe('已提交到 main abc1234')
    expect(interpretCommit('', 'On branch main\nnothing to commit, working tree clean', 1).kind).toBe('failed')
    expect(interpretCommit('', 'fatal: unable to auto-detect email address', 128).message).toBe('fatal: unable to auto-detect email address')
  })

  it('interpretPush：成功 / 非快进拒绝（提示先拉取）/ 其他失败', () => {
    expect(interpretPush('To gitee.com:t/a.git\n   main -> main', '', 0).kind).toBe('pushed')
    expect(interpretPush('branch \'main\' set up to track \'gitee/main\'.', '', 0).message).toContain('上游')
    expect(interpretPush('', "! [rejected]        main -> main (fetch first)", 1).kind).toBe('rejected')
    expect(interpretPush('', 'fatal: 没有配置远程仓库', 128).kind).toBe('failed')
    // git-not-found 哨兵转友好文案
    expect(interpretPush('', 'git-not-found', -1).message).toBe('未检测到 git（请安装 git 或加入 PATH 后重试）')
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
    expect(cmd).toContain("git -C '/var/www/app' config --get-regexp '^remote\\..*\\.url$'")
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
      'remote.origin.url git@github.com:team/data-api.git',
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

describe('提交信息规则草稿与临时产物识别', () => {
  it('looksLikeNoiseFile：日志/临时/备份/系统文件命中，普通源码不命中', () => {
    expect(looksLikeNoiseFile('deploy.log')).toBe(true)
    expect(looksLikeNoiseFile('src/app/tmp.tmp')).toBe(true)
    expect(looksLikeNoiseFile('a/b/config.cfg.bak')).toBe(true)
    expect(looksLikeNoiseFile('.DS_Store')).toBe(true)
    expect(looksLikeNoiseFile('C:\\Users\\x\\Thumbs.db')).toBe(true)
    expect(looksLikeNoiseFile('src/components/ssh/git/GitReposPanel.vue')).toBe(false)
    expect(looksLikeNoiseFile('logger.ts')).toBe(false) // 名字含 log 但不是 .log 后缀
  })

  it('draftCommitMessage：按构成选 type，scope 取最深公共目录', () => {
    expect(draftCommitMessage(['src/git/GitPanel.vue'])).toBe('feat(git): 更新 GitPanel.vue')
    expect(draftCommitMessage(['tests/api/a.test.ts', 'tests/api/b.test.ts'])).toBe('test(api): 调整 2 个文件')
    expect(draftCommitMessage(['README.md', 'docs/spec.md'])).toBe('docs: 调整 2 个文件')
    expect(draftCommitMessage(['package.json', 'tsconfig.json'])).toBe('chore: 调整 2 个文件')
    expect(draftCommitMessage(['src/utils/timeout-fix.ts', 'src/utils/x.ts'])).toBe('fix(utils): 调整 2 个文件')
    // 多个顶层目录：不带 scope
    expect(draftCommitMessage(['src/a.ts', 'docs/b.md'])).toBe('feat: 调整 2 个文件')
    // 单文件且无目录：不带 scope
    expect(draftCommitMessage(['hotfix.patch'])).toBe('fix: 更新 hotfix.patch')
    expect(draftCommitMessage([])).toBe('')
  })
})

describe('提交图谱（--graph --all 解析、命令构造与车道布局）', () => {
  /** 逐字对照 `git log --graph --all --format=%x01%H%x1f…` 的真实输出形态（含 %P 父哈希；
   *  连接线行（|\\、|/）在解析时丢弃——拓扑由 parents 承载） */
  const raw = [
    `* \x01e511be5aaa\x1ffix: a\x1fckun\x1f2 hours ago\x1fHEAD -> main, origin/main, tag: v1.0\x1f9fabfa2eee a1b2c3ddd`,
    `|\\`,
    `| * \x01a1b2c3ddd\x1ffeat: b\x1flin\x1fyesterday\x1forigin/dev\x1fd4e5f6accc`,
    `* | \x019fabfa2eee\x1fdocs: c\x1fckun\x1f3 days ago\x1f\x1fd4e5f6accc`,
    `|/`,
    `* \x01d4e5f6accc\x1finit\x1fckun\x1f1 month ago\x1f\x1f`,
  ].join('\n')

  it('parseGraphLog：提交行切出字段/引用/父哈希，图谱列与连接线行丢弃', () => {
    const rows = parseGraphLog(raw)
    expect(rows).toHaveLength(4)
    expect(rows[0]).toEqual({
      commit: { hash: 'e511be5aaa', subject: 'fix: a', author: 'ckun', relTime: '2 hours ago' },
      refs: ['HEAD -> main', 'origin/main', 'tag: v1.0'],
      parents: ['9fabfa2eee', 'a1b2c3ddd'],
    })
    expect(rows[1].refs).toEqual(['origin/dev'])
    expect(rows[1].parents).toEqual(['d4e5f6accc'])
    expect(rows[2].refs).toEqual([])
    expect(rows[3].commit.subject).toBe('init')
    expect(rows[3].parents).toEqual([])
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
    expect(buildGraphLogArgs(30)).toEqual(['log', '-n', '30', '--graph', '--all', '--format=%x01%H%x1f%s%x1f%an%x1f%cr%x1f%D%x1f%P'])
    expect(buildGraphLogArgs(0)[2]).toBe('30')
    expect(buildGraphLogArgs(9999)[2]).toBe(String(GRAPH_MAX_LIMIT))
  })

  it('buildRemoteGraphCmd：路径单引号包裹 + 同一套参数', () => {
    const cmd = buildRemoteGraphCmd('/srv/api', 50)
    expect(cmd).toBe("git -C '/srv/api' log -n 50 --graph --all --format='%x01%H%x1f%s%x1f%an%x1f%cr%x1f%D%x1f%P'")
  })

  it('layoutGraphLanes：首父继承车道、次父开新车道、根提交释放车道', () => {
    const { rows: lanes, laneCount } = layoutGraphLanes(parseGraphLog(raw))
    expect(laneCount).toBe(2)
    // e511 在车道 0：首父 9fab（提交序 2）同车道，次父 a1b2（提交序 1）开车道 1
    expect(lanes[0].lane).toBe(0)
    expect(lanes[0].edges).toEqual([{ toRow: 2, toLane: 0 }, { toRow: 1, toLane: 1 }])
    // a1b2 在车道 1，父 d4e5（提交序 3）同车道
    expect(lanes[1].lane).toBe(1)
    expect(lanes[1].edges).toEqual([{ toRow: 3, toLane: 1 }])
    // 9fab 回到车道 0，汇入 d4e5
    expect(lanes[2].lane).toBe(0)
    expect(lanes[2].edges).toEqual([{ toRow: 3, toLane: 0 }])
    // d4e5 是根提交：无连线，车道释放
    expect(lanes[3].lane).toBe(0)
    expect(lanes[3].edges).toEqual([])
  })

  it('layoutGraphLanes：父提交超出加载范围时跳过该边（车道就地释放）', () => {
    const rows = parseGraphLog(raw).filter(r => r.commit.hash !== 'd4e5f6accc')
    const { rows: lanes, laneCount } = layoutGraphLanes(rows)
    // 截掉根提交：e511 的两个父（9fab/a1b2）仍在，车道布局不变；
    // a1b2 与 9fab 的父 d4e5 不可见 → 无边、车道释放
    expect(laneCount).toBe(2)
    expect(lanes[0].edges).toEqual([{ toRow: 2, toLane: 0 }, { toRow: 1, toLane: 1 }])
    expect(lanes[1].lane).toBe(1)
    expect(lanes[1].edges).toEqual([])
    expect(lanes[2].lane).toBe(0)
    expect(lanes[2].edges).toEqual([])
  })
})
