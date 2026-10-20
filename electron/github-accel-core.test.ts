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
  buildGitUnsetSubsectionArgs,
  buildProbeUrl,
  decodeHostsBytes,
  defaultGithubAccelConfig,
  encodeHostsBytes,
  findGithubInsteadOf,
  githubHostsDomains,
  hasHostsBlock,
  hostsBlockState,
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
  HOSTS_BLOCK_END,
  hostsBlockAgeDays,
  hostsBlockGeneratedAt,
  isHostsBlockStale,
  isProxyProbeOk,
  parseSshG,
  PROBE_TARGET,
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

  it('「最新版本资产」链接与带 tag 的同归 release-asset', () => {
    const p = parseGithubUrl('https://github.com/o/r/releases/latest/download/app-win.zip')
    expect(p.type).toBe('release-asset')
    expect(p.assetPath).toBe('app-win.zip')
    expect(p.normalized).toBe('https://github.com/o/r/releases/latest/download/app-win.zip')
  })

  it('完整 clone 命令与 owner/repo 简写', () => {
    expect(parseGithubUrl('git clone https://github.com/o/r.git')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('git clone --depth 1 https://github.com/o/r.git mydir')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('gh repo clone o/r')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('“https://github.com/o/r”')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
    expect(parseGithubUrl('o/r')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r', normalized: 'https://github.com/o/r' })
    expect(parseGithubUrl('o/r.git')).toMatchObject({ type: 'repo', owner: 'o', repo: 'r' })
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
    // 子节变体：生产卸载的是任意生效子节（github-accel.ts gitEnable/gitDisable），
    // 前缀变体委托本函数 —— 两条路径都被测试钉住
    expect(buildGitUnsetSubsectionArgs('https://old.example/https://github.com/')).toEqual([
      'config', '--global', '--unset', 'url.https://old.example/https://github.com/.insteadOf',
    ])
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

describe('自检判据', () => {
  it('代理探测只认 2xx（403/404/429 都是没取到内容）', () => {
    expect(isProxyProbeOk(200)).toBe(true)
    expect(isProxyProbeOk(204)).toBe(true)
    expect(isProxyProbeOk(301)).toBe(false)
    expect(isProxyProbeOk(403)).toBe(false)
    expect(isProxyProbeOk(404)).toBe(false)
    expect(isProxyProbeOk(429)).toBe(false)
    expect(isProxyProbeOk(500)).toBe(false)
    expect(isProxyProbeOk(Number.NaN)).toBe(false)
  })

  it('ssh -G 输出解析：取 hostname/port，缺字段不猜默认值', () => {
    const out = [
      'host github.com',
      'user git',
      'hostname ssh.github.com',
      'port 443',
      'addressfamily any',
    ].join('\n')
    expect(parseSshG(out)).toEqual({ hostname: 'ssh.github.com', port: 443 })
    expect(parseSshG('host github.com\nhostname github.com\n')).toEqual({ hostname: 'github.com', port: null })
    expect(parseSshG('')).toEqual({ hostname: null, port: null })
    expect(parseSshG('port not-a-number\n')).toEqual({ hostname: null, port: null })
  })
})

describe('hosts 托管块（M3）', () => {
  it('非 UTF-8 的 hosts 内容按字节往返（GBK 注释不被 UTF-8 解码破坏）', () => {
    // "# 中文注释" 的 GBK 字节 + CRLF：Windows hosts 常见的 ANSI 编码
    const gbkComment = Buffer.from([0x23, 0x20, 0xd6, 0xd0, 0xce, 0xc4, 0xd7, 0xa2, 0xca, 0xcd, 0x0d, 0x0a])
    const localhost = Buffer.from('127.0.0.1 localhost\r\n', 'latin1')
    const original = Buffer.concat([gbkComment, localhost])

    const text = decodeHostsBytes(original)
    expect(text).not.toContain('\uFFFD')

    const applied = applyHostsBlock(text, [{ domain: 'github.com', ip: '140.82.112.4' }])
    const out = Buffer.from(encodeHostsBytes(applied))
    // 块外原始字节（GBK 注释 + CRLF）必须逐字节保留
    expect(out.subarray(0, original.length)).toEqual(original)
    expect(decodeHostsBytes(out)).not.toContain('\uFFFD')
  })

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

  it('SEC-001：domain 注入向量被整条丢弃（换行/空格/#/非法字符/超长）', () => {
    const block = renderHostsBlock([
      // 换行注入：把任意域名指到攻击者 IP，影响全系统解析
      { domain: 'evil.com\n1.2.3.4 attacker.example', ip: '140.82.112.4' },
      // 空格当别名
      { domain: 'a.com b.com', ip: '140.82.112.5' },
      // 篡改注释边界
      { domain: 'x.com # injected', ip: '140.82.112.6' },
      // 非法字符（下划线/斜杠/大写混入后未按白名单放行）
      { domain: 'bad_domain.example', ip: '140.82.112.7' },
      { domain: 'a/b.example', ip: '140.82.112.8' },
      // 超长域名（DNS 上限 253）
      { domain: `${'a'.repeat(250)}.example.com`, ip: '140.82.112.9' },
      // 合法项不受影响
      { domain: 'github.com', ip: '140.82.112.4' },
    ])
    expect(block).toContain('140.82.112.4\tgithub.com')
    expect(block).not.toContain('attacker.example')
    expect(block).not.toContain('a.com b.com')
    expect(block).not.toContain('injected')
    expect(block).not.toContain('bad_domain')
    expect(block).not.toContain('a/b')
    expect(block).not.toContain(`${'a'.repeat(250)}`)
    // 不产生空行（全被丢弃时只剩标记行）
    expect(block.split('\n').every(l => l.length > 0)).toBe(true)
  })

  it('apply → remove 精确往返（LF / CRLF / 无尾换行 / 空文件），且不碰块外内容', () => {
    const entries = githubHostsDomains().map((d, i) => ({ domain: d, ip: `140.82.112.${i + 1}` }))
    const originals = [
      '',
      '# my hosts\n127.0.0.1 localhost\n',
      '127.0.0.1 localhost\n\n# note\n',
      '127.0.0.1 localhost',
      '# note\r\n127.0.0.1 localhost\r\n',
      '127.0.0.1 localhost\r\n',
    ]
    for (const raw of originals) {
      const applied = applyHostsBlock(raw, entries)
      expect(hasHostsBlock(applied)).toBe(true)
      // 块外内容原样保留（空文件用例没有块外内容）
      if (raw.includes('127.0.0.1 localhost')) expect(applied).toContain('127.0.0.1 localhost')
      expect(removeHostsBlock(applied)).toBe(raw)
    }
  })

  it('重复 apply 原位替换后仍能精确还原', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    for (const raw of ['127.0.0.1 localhost\n', '127.0.0.1 localhost\r\n', '127.0.0.1 localhost']) {
      const twice = applyHostsBlock(applyHostsBlock(raw, entries), entries)
      expect(twice.split(HOSTS_BLOCK_BEGIN)).toHaveLength(2)
      expect(removeHostsBlock(twice)).toBe(raw)
    }
  })

  it('已有托管块时原位更新而非重复追加', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    const once = applyHostsBlock('', entries)
    const twice = applyHostsBlock(once, entries)
    expect(twice.split(HOSTS_BLOCK_BEGIN)).toHaveLength(2)
    expect(hasHostsBlock(twice)).toBe(true)
  })

  it('标记不配对时拒绝改写：BEGIN-only 不会被追加成双块，也不会连带删用户行', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    const beginOnly = `${HOSTS_BLOCK_BEGIN}\n127.0.0.1 myhost.local\n`
    expect(hostsBlockState(beginOnly)).toBe('broken')
    expect(() => applyHostsBlock(beginOnly, entries)).toThrow(/托管标记不完整/)
    expect(() => removeHostsBlock(beginOnly)).toThrow(/托管标记不完整/)
  })

  it('END-only / 标记顺序颠倒 / 重复块都判 broken', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    const endOnly = `127.0.0.1 localhost\n${HOSTS_BLOCK_END}\n`
    const reversed = `${HOSTS_BLOCK_END}\n# note\n${HOSTS_BLOCK_BEGIN}\n`
    const twoBlocks = `${applyHostsBlock('', entries)}\n${renderHostsBlock(entries)}\n`
    for (const raw of [endOnly, reversed, twoBlocks]) {
      expect(hostsBlockState(raw)).toBe('broken')
      expect(() => removeHostsBlock(raw)).toThrow(/托管标记不完整/)
    }
  })

  it('无标记 = none；成对且唯一 = ok', () => {
    const entries = [{ domain: 'github.com', ip: '140.82.112.4' }]
    expect(hostsBlockState('127.0.0.1 localhost\n')).toBe('none')
    expect(hostsBlockState(applyHostsBlock('127.0.0.1 localhost\n', entries))).toBe('ok')
  })

  it('域名清单覆盖 GitHub 关键面', () => {
    const d = githubHostsDomains()
    expect(d).toContain('github.com')
    expect(d).toContain('api.github.com')
    expect(d).toContain('raw.githubusercontent.com')
    expect(d).toContain('codeload.github.com')
  })
})

describe('hosts 托管块的时效（优选 IP 不会自动更新）', () => {
  const entries = [{ domain: 'github.com', ip: '1.2.3.4' }]
  const DAY = 24 * 60 * 60 * 1000
  const now = Date.UTC(2026, 9, 14, 12, 0, 0)

  it('渲染块自带生成时间戳 —— 系统文件必须自己说明自己是哪天测的', () => {
    const block = renderHostsBlock(entries, new Date(now))
    expect(block).toContain(`${HOSTS_BLOCK_BEGIN}\n# generated: ${new Date(now).toISOString()}\n`)
    expect(block).toContain('1.2.3.4\tgithub.com')
    expect(block.trimEnd().endsWith(HOSTS_BLOCK_END)).toBe(true)
  })

  it('按时间戳算出已过去多少天', () => {
    const content = renderHostsBlock(entries, new Date(now - 8 * DAY))
    expect(hostsBlockAgeDays(content, new Date(now))).toBe(8)
  })

  it('原始生成时间也可读出（UI 要能说"哪天测的"，而不是只给天数）', () => {
    const at = new Date(now - 8 * DAY)
    expect(hostsBlockGeneratedAt(renderHostsBlock(entries, at))).toBe(at.toISOString())
    expect(hostsBlockGeneratedAt(`${HOSTS_BLOCK_BEGIN}\n1.2.3.4\tgithub.com\n${HOSTS_BLOCK_END}`)).toBeNull()
  })

  it('6 天前算新鲜，7 天及以上算该重选', () => {
    const fresh = renderHostsBlock(entries, new Date(now - 6 * DAY))
    const old = renderHostsBlock(entries, new Date(now - 7 * DAY))
    expect(isHostsBlockStale(fresh, new Date(now))).toBe(false)
    expect(isHostsBlockStale(old, new Date(now))).toBe(true)
  })

  it('块里没有可比对的时间戳 → 时效未知：ageDays 为 null，且不得谎报新鲜', () => {
    // 手写/旧版本写入的块没有 # generated: 行。把它当"新鲜"等于替用户担保一个
    // 无从考证的 IP；当"过期"只是多提示一次重选，代价不对称。
    const legacy = `${HOSTS_BLOCK_BEGIN}\n1.2.3.4\tgithub.com\n${HOSTS_BLOCK_END}\n`
    expect(hostsBlockAgeDays(legacy, new Date(now))).toBeNull()
    expect(isHostsBlockStale(legacy, new Date(now))).toBe(true)
  })

  it('时间戳被写成不可解析的值时同样按未知处理，不给 NaN 天', () => {
    const broken = `${HOSTS_BLOCK_BEGIN}\n# generated: 昨天\n1.2.3.4\tgithub.com\n${HOSTS_BLOCK_END}\n`
    expect(hostsBlockAgeDays(broken, new Date(now))).toBeNull()
    expect(isHostsBlockStale(broken, new Date(now))).toBe(true)
  })

  it('块内多行里只认我们自己那条 generated: 标记（不被别处的注释劫持时效判定）', () => {
    const tricky = [
      HOSTS_BLOCK_BEGIN,
      '# generated: 2000-01-01T00:00:00.000Z',
      '# 用户自己的注释里也有 # generated: 2030-01-01T00:00:00.000Z',
      '1.2.3.4\tgithub.com',
      HOSTS_BLOCK_END,
    ].join('\n')
    expect(hostsBlockAgeDays(tricky, new Date(now))).toBeGreaterThan(9000)
  })

  it('applyHostsBlock 透传时间戳：替换旧块时不会把时间戳弄丢', () => {
    const oldBlock = renderHostsBlock(entries, new Date(now - 30 * DAY))
    const next = applyHostsBlock(`127.0.0.1 localhost\n\n${oldBlock}\n`, entries, new Date(now))
    expect(hostsBlockAgeDays(next, new Date(now))).toBe(0)
    expect(isHostsBlockStale(next, new Date(now))).toBe(false)
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

  it('探测地址 = 前缀 + raw 探测目标（ghproxy 类不代理首页，打首页只会 403）', () => {
    expect(PROBE_TARGET).toContain('raw.githubusercontent.com')
    expect(buildProbeUrl('https://m1.example/')).toBe(`https://m1.example/${PROBE_TARGET}`)
  })
})
