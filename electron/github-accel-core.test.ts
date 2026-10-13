/**
 * GitHub 加速纯逻辑核心（shared/github-accel-core.ts）：
 * URL 解析 / 加速链接生成 / insteadOf 参数与解析 / ssh 443 托管块。
 * 这些函数是渲染层与主进程共用的单一事实来源，规则错了两边一起错，必须钉死。
 */
import { describe, expect, it } from 'vitest'
import {
  applyHostsBlock,
  applySshBlock,
  buildAccelLinks,
  buildGitReadArgs,
  buildGitRestoreArgs,
  buildGitSetArgs,
  buildGitUnsetArgs,
  buildProbeUrl,
  defaultGithubAccelConfig,
  findGithubInsteadOf,
  githubHostsDomains,
  hasHostsBlock,
  hasSshBlock,
  isValidIPv4,
  parseGithubUrl,
  parseGitInsteadOf,
  removeHostsBlock,
  removeSshBlock,
  renderHostsBlock,
  renderSsh443Block,
  validateProxyPrefix,
  validateWebMirrorHost,
  GITHUB_BASE,
  HOSTS_BLOCK_BEGIN,
  SSH_BLOCK_BEGIN,
  SSH_BLOCK_END,
  type AccelProxy,
  type AccelWebMirror,
} from '../shared/github-accel-core'

const proxies = (over: Partial<AccelProxy> = {}): AccelProxy[] => [
  { id: 'p1', name: 'M1', prefix: 'https://m1.example/', builtin: false, enabled: true, ...over },
]
const webMirrors = (over: Partial<AccelWebMirror> = {}): AccelWebMirror[] => [
  { id: 'w1', name: 'W1', host: 'w1.example.com', builtin: false, enabled: true, ...over },
]

describe('parseGithubUrl', () => {
  it('仓库主页（带/不带 .git、带尾斜杠）', () => {
    for (const raw of ['https://github.com/o/r', 'https://github.com/o/r.git', 'https://github.com/o/r/', 'github.com/o/r']) {
      const p = parseGithubUrl(raw)
      expect(p.type).toBe('repo')
      expect(p.owner).toBe('o')
      expect(p.repo).toBe('r')
      expect(p.normalized).toBe('https://github.com/o/r')
    }
  })

  it('git@ 与 ssh:// 形式折算为 https 仓库地址', () => {
    expect(parseGithubUrl('git@github.com:o/r.git')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r', normalized: 'https://github.com/o/r' })
    expect(parseGithubUrl('ssh://git@github.com/o/r')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
  })

  it('Release 附件链接', () => {
    const p = parseGithubUrl('https://github.com/o/r/releases/download/v1.2.3/app-win.zip')
    expect(p.type).toBe('release-asset')
    expect(p.assetPath).toBe('v1.2.3/app-win.zip')
    expect(p.normalized).toBe('https://github.com/o/r/releases/download/v1.2.3/app-win.zip')
  })

  it('releases 页面（非 download）按页面处理', () => {
    expect(parseGithubUrl('https://github.com/o/r/releases').type).toBe('page')
  })

  it('archive 与 codeload 链接', () => {
    expect(parseGithubUrl('https://github.com/o/r/archive/refs/heads/main.tar.gz')).toMatchObject({ type: 'archive', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('https://codeload.github.com/o/r/zip/refs/heads/main')).toMatchObject({ type: 'archive', owner: 'o', repo: 'r' })
  })

  it('raw 链接', () => {
    const p = parseGithubUrl('https://raw.githubusercontent.com/o/r/main/a/b.txt')
    expect(p.type).toBe('raw')
    expect(p.owner).toBe('o')
    expect(p.repo).toBe('r')
  })

  it('仓库内页面（tree/blob/issues…）', () => {
    expect(parseGithubUrl('https://github.com/o/r/tree/main/src')).toMatchObject({ type: 'page', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('https://github.com/o/r/issues/12').type).toBe('page')
  })

  it('无法识别的输入', () => {
    expect(parseGithubUrl('').type).toBe('unknown')
    expect(parseGithubUrl('https://example.com/o/r').type).toBe('unknown')
    expect(parseGithubUrl('https://github.com/onlyowner').type).toBe('unknown')
    expect(parseGithubUrl('not a url at all ::').type).toBe('unknown')
  })
})

describe('buildAccelLinks', () => {
  it('repo：每个启用代理一条 clone + 每个启用镜像站一条 web', () => {
    const p = parseGithubUrl('https://github.com/o/r')
    const { links, canonical } = buildAccelLinks(p, proxies(), webMirrors())
    expect(canonical.cloneUrl).toBe('https://github.com/o/r.git')
    expect(links.filter(l => l.kind === 'clone')).toEqual([
      { kind: 'clone', label: 'git clone（M1）', url: 'https://m1.example/https://github.com/o/r.git', mirrorId: 'p1', mirrorName: 'M1' },
    ])
    expect(links.filter(l => l.kind === 'web')[0].url).toBe('https://w1.example.com/o/r')
  })

  it('page：web 镜像保留子路径，clone 指向仓库根', () => {
    const p = parseGithubUrl('https://github.com/o/r/tree/main/src')
    const { links, canonical } = buildAccelLinks(p, proxies(), webMirrors())
    expect(canonical.cloneUrl).toBe('https://github.com/o/r.git')
    expect(links.find(l => l.kind === 'web')!.url).toBe('https://w1.example.com/o/r/tree/main/src')
  })

  it('release-asset：只产出 download 链接', () => {
    const p = parseGithubUrl('https://github.com/o/r/releases/download/v1/a.zip')
    const { links, canonical } = buildAccelLinks(p, proxies(), webMirrors())
    expect(canonical.downloadUrl).toBe('https://github.com/o/r/releases/download/v1/a.zip')
    expect(links).toHaveLength(1)
    expect(links[0]).toMatchObject({ kind: 'download', url: 'https://m1.example/https://github.com/o/r/releases/download/v1/a.zip' })
  })

  it('停用的条目不产出链接；unknown 产出空', () => {
    const p = parseGithubUrl('https://github.com/o/r')
    expect(buildAccelLinks(p, proxies({ enabled: false }), webMirrors({ enabled: false })).links).toHaveLength(0)
    expect(buildAccelLinks(parseGithubUrl(''), proxies(), webMirrors()).links).toHaveLength(0)
  })
})

describe('配置校验', () => {
  it('代理前缀：必须 https 且以 / 结尾、不带路径', () => {
    expect(validateProxyPrefix('https://m.example/')).toBeNull()
    expect(validateProxyPrefix('http://m.example/')).toContain('https')
    expect(validateProxyPrefix('https://m.example')).toContain('/ 结尾')
    expect(validateProxyPrefix('https://m.example/sub/')).toContain('路径')
    expect(validateProxyPrefix('')).toContain('不能为空')
  })

  it('镜像站域名', () => {
    expect(validateWebMirrorHost('kkgithub.com')).toBeNull()
    expect(validateWebMirrorHost('https://kkgithub.com')).not.toBeNull()
    expect(validateWebMirrorHost('bad host!')).not.toBeNull()
  })
})

describe('git insteadOf', () => {
  const prefix = 'https://m1.example/'

  it('set/unset/read 参数组装', () => {
    expect(buildGitSetArgs(prefix)).toEqual(['config', '--global', `url.${prefix}${GITHUB_BASE}.insteadOf`, GITHUB_BASE])
    expect(buildGitUnsetArgs(prefix)).toEqual(['config', '--global', '--unset', `url.${prefix}${GITHUB_BASE}.insteadOf`])
    expect(buildGitRestoreArgs('https://old.example/https://github.com/')).toEqual([
      'config', '--global', 'url.https://old.example/https://github.com/.insteadOf', GITHUB_BASE,
    ])
    expect(buildGitReadArgs()[3]).toBe('^url\\..*\\.insteadof$')
  })

  it('解析 git 输出（含无关行与空行）', () => {
    const stdout = [
      'url.https://m1.example/https://github.com/.insteadof https://github.com/',
      'url.https://gitee.com/.insteadof https://github.com:',
      '',
      'garbage-line-without-space',
    ].join('\n')
    const entries = parseGitInsteadOf(stdout)
    expect(entries).toHaveLength(2)
    expect(entries[0]).toMatchObject({ subsection: 'https://m1.example/https://github.com/', value: 'https://github.com/' })
  })

  it('findGithubInsteadOf 只认 value 恰为 github.com 基的条目', () => {
    const hit = parseGitInsteadOf(`url.${prefix}${GITHUB_BASE}.insteadof ${GITHUB_BASE}\n`)
    expect(findGithubInsteadOf(hit)?.subsection).toBe(`${prefix}${GITHUB_BASE}`)
    const miss = parseGitInsteadOf('url.https://x/.insteadof https://gitlab.com/\n')
    expect(findGithubInsteadOf(miss)).toBeNull()
    expect(findGithubInsteadOf([])).toBeNull()
  })
})

describe('ssh 443 托管块', () => {
  it('块内容含官方 443 接入参数', () => {
    const block = renderSsh443Block()
    expect(block).toContain('Host github.com')
    expect(block).toContain('HostName ssh.github.com')
    expect(block).toContain('Port 443')
    expect(block.startsWith(SSH_BLOCK_BEGIN)).toBe(true)
    expect(block.endsWith(SSH_BLOCK_END)).toBe(true)
  })

  it('空文件追加；已有内容追加时补空行', () => {
    expect(applySshBlock('')).toBe(renderSsh443Block() + '\n')
    const withContent = applySshBlock('Host example\n  HostName example.com\n')
    expect(withContent).toContain('Host example')
    expect(withContent.indexOf(SSH_BLOCK_BEGIN)).toBeGreaterThan(0)
    expect(hasSshBlock(withContent)).toBe(true)
  })

  it('已有托管块时原位替换而非重复追加', () => {
    const once = applySshBlock('')
    const twice = applySshBlock(once)
    expect(twice.split(SSH_BLOCK_BEGIN)).toHaveLength(2)
  })

  it('remove 精确移除托管块，其余内容原样保留', () => {
    const head = 'Host example\n  HostName example.com\n'
    const combined = applySshBlock(head)
    const removed = removeSshBlock(combined)
    expect(hasSshBlock(removed)).toBe(false)
    expect(removed.trim()).toBe(head.trim())
  })

  it('remove 无块时原样返回', () => {
    const raw = 'Host example\n'
    expect(removeSshBlock(raw)).toBe(raw)
  })

  it('apply → remove 往返恒等', () => {
    for (const raw of ['', 'Host a\n', 'Host a\n  HostName b\n\nHost c\n']) {
      expect(removeSshBlock(applySshBlock(raw)).trim()).toBe(raw.trim())
    }
  })
})

describe('hosts 托管块（M3）', () => {
  it('块内容只含合法 IPv4，非法 IP 被丢弃', () => {
    const block = renderHostsBlock([
      { domain: 'github.com', ip: '20.205.243.166' },
      { domain: 'bad.example', ip: '999.1.1.1' },
      { domain: 'evil.example', ip: '1.2.3.4; rm -rf /' },
    ])
    expect(block).toContain('20.205.243.166\tgithub.com')
    expect(block).not.toContain('bad.example')
    expect(block).not.toContain('evil')
  })

  it('isValidIPv4 严格校验', () => {
    expect(isValidIPv4('1.2.3.4')).toBe(true)
    expect(isValidIPv4('0.0.0.0')).toBe(true)
    expect(isValidIPv4('1.2.3.256')).toBe(false)
    expect(isValidIPv4('1.2.3')).toBe(false)
    expect(isValidIPv4('1.2.3.4.5')).toBe(false)
    expect(isValidIPv4('a.b.c.d')).toBe(false)
  })

  it('apply → remove 往返恒等，且不碰块外内容', () => {
    const entries = githubHostsDomains().map((d, i) => ({ domain: d, ip: `140.82.112.${i + 1}` }))
    for (const raw of ['', '# my hosts\n127.0.0.1 localhost\n', '127.0.0.1 localhost\n\n# note\n']) {
      const applied = applyHostsBlock(raw, entries)
      expect(hasHostsBlock(applied)).toBe(true)
      // 块外内容原样保留（空文件用例没有块外内容）
      if (raw.includes('127.0.0.1 localhost')) expect(applied).toContain('127.0.0.1 localhost')
      expect(removeHostsBlock(applied).trim()).toBe(raw.trim())
    }
  })

  it('已有托管块时原位更新而非重复追加', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    const once = applyHostsBlock('', entries)
    const twice = applyHostsBlock(once, entries)
    expect(twice.split(HOSTS_BLOCK_BEGIN)).toHaveLength(2)
    expect(hasHostsBlock(twice)).toBe(true)
  })

  it('域名清单覆盖 GitHub 关键面', () => {
    const d = githubHostsDomains()
    expect(d).toContain('github.com')
    expect(d).toContain('api.github.com')
    expect(d).toContain('raw.githubusercontent.com')
    expect(d).toContain('codeload.github.com')
  })
})

describe('出厂配置', () => {
  it('内置代理与镜像站齐备，前缀/域名均过校验', () => {
    const c = defaultGithubAccelConfig()
    expect(c.proxies.length).toBeGreaterThanOrEqual(2)
    expect(c.webMirrors.length).toBeGreaterThanOrEqual(1)
    for (const p of c.proxies) expect(validateProxyPrefix(p.prefix)).toBeNull()
    for (const w of c.webMirrors) expect(validateWebMirrorHost(w.host)).toBeNull()
    expect(c.gitBackup).toBeNull()
  })

  it('探测地址 = 前缀 + github 首页', () => {
    expect(buildProbeUrl('https://m1.example/')).toBe('https://m1.example/https://github.com/')
  })
})
