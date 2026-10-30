/**
 * GitHub 加速纯逻辑核心（shared/github-accel-core.ts）：
 * 配置校验 / insteadOf 参数与解析 / ssh 443 托管块。
 * 这些函数是渲染层与主进程共用的单一事实来源，规则错了两边一起错，必须钉死。
 */
import { describe, expect, it } from 'vitest'
import {
  applyHostsBlock,
  applySshBlock,
  buildGitReadArgs,
  buildGitPushKeepArgs,
  buildGitPushKeepUnsetArgs,
  buildGitRestoreArgs,
  buildGitSetArgs,
  buildGitUnsetArgs,
  buildGitUnsetSubsectionArgs,
  buildGitPushUnsetSubsectionArgs,
  buildProbeUrl,
  buildProbeUrlForKind,
  buildTokenAuthUrl,
  decodeHostsBytes,
  defaultGithubAccelConfig,
  encodeHostsBytes,
  filterEmptyTokens,
  findGithubInsteadOf,
  findGithubPushInsteadOf,
  githubHostsDomains,
  hasHostsBlock,
  hostsBlockState,
  hasSshBlock,
  isArchiveProbeOk,
  isManagedPushSubsection,
  isValidIPv4,
  maskToken,
  mergeHostCandidates,
  parseCommunityHosts,
  parseGitInsteadOf,
  parseMirrorAggregator,
  pushSubsectionOf,
  removeHostsBlock,
  removeSshBlock,
  renderHostsBlock,
  renderSsh443Block,
  resolveInsteadOf,
  sanitizeToken,
  validateProxyPrefix,
  compareProbe,
  DISCOVER_LIMIT,
  GIT_SSH_PUSH_SUBSECTION,
  GITHUB_BASE,
  HOSTS_BLOCK_BEGIN,
  HOSTS_BLOCK_END,
  hostsBlockAgeDays,
  hostsBlockGeneratedAt,
  isHostsBlockStale,
  isProxyProbeOk,
  isProxyProbeOkWithContentType,
  parseSshG,
  PROBE_TARGET,
  PROBE_TARGET_ARCHIVE,
  SSH_BLOCK_BEGIN,
  SSH_BLOCK_END,
  nextFailStreak,
  pickAutoAdopt,
  pickBestProbe,
  shouldRetire,
  AUTO_ADOPT_LIMIT,
  PROBE_RETIRE_STREAK,
} from '../shared/github-accel-core'

describe('配置校验', () => {
  it('代理前缀：必须 https 且以 / 结尾，允许一级路径（如 gh-proxy 的子路径部署）', () => {
    expect(validateProxyPrefix('https://m.example/')).toBeNull()
    expect(validateProxyPrefix('https://m.example/gh/')).toBeNull()
    expect(validateProxyPrefix('http://m.example/')).toContain('https')
    expect(validateProxyPrefix('https://m.example')).toContain('/ 结尾')
    // 多级路径会在拼接 raw/archive 目标时错位，仍拒
    expect(validateProxyPrefix('https://m.example/a/b/')).toContain('路径')
    expect(validateProxyPrefix('https://m.example/sub')).toContain('/ 结尾')
    expect(validateProxyPrefix('')).toContain('不能为空')
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
    expect(buildGitReadArgs()[3]).toBe('^url\\..*\\.(push)?insteadof$')
    expect(buildGitPushKeepArgs()).toEqual(['config', '--global', `url.${GITHUB_BASE}.pushInsteadOf`, GITHUB_BASE])
    expect(buildGitPushKeepUnsetArgs()).toEqual(['config', '--global', '--unset', `url.${GITHUB_BASE}.pushInsteadOf`])
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

  it('解析 pushinsteadof 条目（push 标记与子节提取），与 insteadof 混排互不误判', () => {
    const entries = parseGitInsteadOf([
      `url.${prefix}${GITHUB_BASE}.insteadof ${GITHUB_BASE}`,
      `url.${GITHUB_BASE}.pushinsteadof ${GITHUB_BASE}`,
      'url.ssh://git@github.com/.pushinsteadof https://github.com/',
    ].join('\n'))
    expect(entries).toHaveLength(3)
    expect(entries[0].push).toBeUndefined()
    expect(entries[1]).toMatchObject({ subsection: GITHUB_BASE, value: GITHUB_BASE, push: true })
    expect(entries[2]).toMatchObject({ subsection: 'ssh://git@github.com/', value: GITHUB_BASE, push: true })
  })

  it('findGithubInsteadOf 忽略 push 条目；findGithubPushInsteadOf 只认 push 条目', () => {
    const mixed = parseGitInsteadOf(
      `url.${prefix}${GITHUB_BASE}.insteadof ${GITHUB_BASE}\nurl.${GITHUB_BASE}.pushinsteadof ${GITHUB_BASE}\n`,
    )
    expect(findGithubInsteadOf(mixed)?.subsection).toBe(`${prefix}${GITHUB_BASE}`)
    expect(findGithubPushInsteadOf(mixed)?.subsection).toBe(GITHUB_BASE)
    // 用户自建的 push 重定向（如到 ssh）也算已有 push 规则：启用时不得叠加自指规则
    const userPush = parseGitInsteadOf('url.ssh://git@github.com/.pushinsteadof https://github.com/\n')
    expect(findGithubPushInsteadOf(userPush)?.subsection).toBe('ssh://git@github.com/')
    expect(findGithubInsteadOf(userPush)).toBeNull()
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

  it('代理探测拒绝 2xx 的 HTML 响应（死而不僵镜像的广告/错误页）', () => {
    expect(isProxyProbeOkWithContentType(200, 'text/plain; charset=utf-8')).toBe(true)
    expect(isProxyProbeOkWithContentType(200, 'application/octet-stream')).toBe(true)
    expect(isProxyProbeOkWithContentType(200, undefined)).toBe(true)
    expect(isProxyProbeOkWithContentType(200, 'text/html; charset=utf-8')).toBe(false)
    expect(isProxyProbeOkWithContentType(200, 'TEXT/HTML')).toBe(false)
    // 非 2xx 仍按不可用处理，HTML 判据只额外收紧
    expect(isProxyProbeOkWithContentType(403, 'text/plain')).toBe(false)
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
  it('内置代理齐备，前缀均过校验', () => {
    const c = defaultGithubAccelConfig()
    expect(c.proxies.length).toBeGreaterThanOrEqual(2)
    for (const p of c.proxies) expect(validateProxyPrefix(p.prefix)).toBeNull()
    // 网页镜像站已下线：配置里不再有该类清单
    expect('webMirrors' in c).toBe(false)
    expect(c.gitBackup).toBeNull()
    // 私人令牌出厂模板必须存在且 token 为空（源码里不落真实凭据）
    expect(Array.isArray(c.privateTokens)).toBe(true)
    expect(c.privateTokens!.length).toBeGreaterThanOrEqual(1)
    for (const t of c.privateTokens!) expect(t.token).toBe('')
  })

  it('探测地址 = 前缀 + raw 探测目标（ghproxy 类不代理首页，打首页只会 403）', () => {
    expect(PROBE_TARGET).toContain('raw.githubusercontent.com')
    expect(buildProbeUrl('https://m1.example/')).toBe(`https://m1.example/${PROBE_TARGET}`)
  })
})

describe('私人令牌（PAT）', () => {
  const SAMPLE = 'ByytV7oivReuuWcsPJKF-oqX' // 4+20 → 24 chars long token
  const masked = `Byy${'*'.repeat(SAMPLE.length - 7)}-oqX`

  it('maskToken 保留前 3 后 4、短 token 整段打码，空串返回空', () => {
    expect(maskToken(SAMPLE)).toBe(masked)
    expect(maskToken('ghp_abcd1234')).toBe('ghp*****1234')
    // 不足 8 字符宁可全掩，不暴露结构
    expect(maskToken('abc')).toBe('****')
    expect(maskToken('xxxxxxx')).toBe('****')
    expect(maskToken('')).toBe('')
    expect(maskToken('   ')).toBe('')
  })

  it('sanitizeToken 清洗合法条目并 trim token', () => {
    const { token, errors } = sanitizeToken({ id: 'x', provider: 'gitcode', host: 'WRONG', label: 'My', token: ' ByytV7oivReuuWcsPJKF-oqX \t' }, 'fb')
    expect(errors).toEqual([])
    expect(token).not.toBeNull()
    expect(token!.id).toBe('x')
    expect(token!.provider).toBe('gitcode')
    // 预设平台 host 取 PROVIDER_HOSTS，忽略入参，并用 trim 后的 token
    expect(token!.host).toBe('gitcode.com')
    expect(token!.token).toBe('ByytV7oivReuuWcsPJKF-oqX')
    expect(token!.updatedAt).toBeGreaterThan(0)
  })

  it('sanitizeToken 自定义平台：host 需合法、空白字符算非法', () => {
    const bad = sanitizeToken({ provider: 'custom', host: '', token: 'abc' }, 'fb')
    expect(bad.token).toBeNull()
    expect(bad.errors.join(' ')).toMatch(/域名|自定义/)
    const bad2 = sanitizeToken({ provider: 'custom', host: 'git.example.com', token: 'a b' }, 'fb')
    expect(bad2.errors.join(' ')).toMatch(/空白/)
    const ok = sanitizeToken({ provider: 'custom', host: 'git.example.com', token: 'secret' }, 'fb')
    expect(ok.token!.host).toBe('git.example.com')
  })

  it('sanitizeToken 非法 provider / 空格 token 拒绝且不猜默认 id', () => {
    const r = sanitizeToken({ provider: 'nope' as any, host: 'github.com', token: 'x' }, 'fb99')
    expect(r.token).toBeNull()
    expect(r.errors.join(' ')).toMatch(/平台类型/)
    // 入参无 id 时由 fallbackId 兜底
    const r2 = sanitizeToken({ provider: 'github', token: 'x' }, 'fb99')
    expect(r2.token!.id).toBe('fb99')
  })

  it('buildTokenAuthUrl 注入鉴权并产掩码版（token 不进 urlMasked 原文）', () => {
    const { url, urlMasked } = buildTokenAuthUrl('gitcode.com', 'owner/repo.git', 'ByytV7oivReuuWcsPJKF-oqX')
    expect(url).toBe('https://ByytV7oivReuuWcsPJKF-oqX@gitcode.com/owner/repo.git')
    expect(urlMasked).toContain(`${masked}@`)
    expect(urlMasked).not.toContain(SAMPLE)
    // 特殊字符（@ / 空格）在 token 中应被 encode，避免破坏 URL 结构
    const special = buildTokenAuthUrl('gitcode.com', 'o/r', 'abc@123')
    expect(special.url).toContain(encodeURIComponent('abc@123'))
  })

  it('buildTokenAuthUrl 空 token / 非法域名抛错', () => {
    expect(() => buildTokenAuthUrl('gitcode.com', 'o/r', '')).toThrow(/令牌为空/)
    expect(() => buildTokenAuthUrl('!!!', 'o/r', 'x')).toThrow(/域名/)
  })

  it('filterEmptyTokens 过滤空模板、保留有效条目并收集 emptyIds', () => {
    const SAMPLE = 'ByytV7oivReuuWcsPJKF-oqX'
    const list = [
      { id: 'tok-gitcode', provider: 'gitcode', token: SAMPLE, host: 'gitcode.com', label: 'GitCode', updatedAt: 1 },
      { id: 'tok-github', provider: 'github', token: '', host: 'github.com', label: 'GitHub', updatedAt: 1 },
      { id: 'tok-gitee', provider: 'gitee', token: '  ', host: 'gitee.com', label: 'Gitee', updatedAt: 1 },
      { id: 'tok-custom', provider: 'custom', token: 'abc', host: 'git.example.com', label: 'X', updatedAt: 1 },
      null,
    ]
    const { persist, emptyIds } = filterEmptyTokens(list as any)
    // 全表提交：tok-github / tok-gitee 为空模板，应被过滤掉，用户真实填的保留
    expect(persist.map(t => t.id)).toEqual(['tok-gitcode', 'tok-custom'])
    expect(emptyIds).toEqual(['tok-github', 'tok-gitee'])
  })
})

describe('镜像清单动态发现（聚合接口只给候选）', () => {
  it('裸域名 / 字符串 / 对象三种录入统一补 https 与尾斜杠', () => {
    const out = parseMirrorAggregator([
      'ghproxy.net',
      { url: 'https://a.example/gh/', name: 'A 站' },
      { site: 'https://b.example', title: 'B' },
    ])
    expect(out.map(c => c.prefix)).toEqual(['https://ghproxy.net/', 'https://a.example/gh/', 'https://b.example/'])
    expect(out[0].name).toBe('ghproxy.net')
    expect(out[1].name).toBe('A 站')
    expect(out[2].name).toBe('B')
  })

  it('明文 http / 多级路径 / 非法域名一律丢弃（不静默升级、不改写）', () => {
    const out = parseMirrorAggregator(['http://plain.example/', 'https://m.example/a/b/', 'https://ok.example/'])
    expect(out.map(c => c.prefix)).toEqual(['https://ok.example/'])
  })

  it('支持 { sites } / { data } 包裹，未知形状降级为空数组', () => {
    expect(parseMirrorAggregator({ sites: ['x.example'] })[0].prefix).toBe('https://x.example/')
    expect(parseMirrorAggregator({ data: [{ url: 'y.example' }] })[0].prefix).toBe('https://y.example/')
    expect(parseMirrorAggregator(null)).toEqual([])
    expect(parseMirrorAggregator({ weird: 'x' })).toEqual([])
    expect(parseMirrorAggregator('https://x.example/')).toEqual([])
  })

  it('去重 + 按上限截断（DISCOVER_LIMIT 兜住 UI 一屏）', () => {
    expect(parseMirrorAggregator(['a.example', 'https://a.example/', 'b.example']).map(c => c.prefix)).toEqual([
      'https://a.example/',
      'https://b.example/',
    ])
    expect(parseMirrorAggregator(['a.example', 'b.example', 'c.example'], 2)).toHaveLength(2)
    expect(DISCOVER_LIMIT).toBeGreaterThan(0)
  })
})

describe('hosts 社区源解析与双源合并', () => {
  it('只保留托管域名，非法 IP / 未知域名丢弃，重复行去重', () => {
    const text = [
      '# GitHub520 同步',
      '140.82.112.4\tgithub.com',
      '140.82.112.5 api.github.com raw.githubusercontent.com  # 尾注释',
      '140.82.112.6 evil.example',
      'not-an-ip github.com',
      '140.82.112.4 github.com',
      '',
    ].join('\n')
    expect(parseCommunityHosts(text)).toEqual([
      { domain: 'github.com', ip: '140.82.112.4' },
      { domain: 'api.github.com', ip: '140.82.112.5' },
      { domain: 'raw.githubusercontent.com', ip: '140.82.112.5' },
    ])
  })

  it('域名大小写无关，且只认传入白名单（第三方文本不能把任意域名写进系统 hosts）', () => {
    expect(parseCommunityHosts('1.2.3.4 GITHUB.com', ['github.com'])).toEqual([{ domain: 'github.com', ip: '1.2.3.4' }])
    expect(parseCommunityHosts('1.2.3.4 other.example', ['github.com'])).toEqual([])
  })

  it('合并候选：本地优先、去重、剔非法、按上限截断', () => {
    expect(mergeHostCandidates(['1.1.1.1', '2.2.2.2'], ['2.2.2.2', '3.3.3.3', 'bad'])).toEqual([
      '1.1.1.1',
      '2.2.2.2',
      '3.3.3.3',
    ])
    expect(mergeHostCandidates(['1.1.1.1', '2.2.2.2', '3.3.3.3'], ['4.4.4.4'], 2)).toEqual(['1.1.1.1', '2.2.2.2'])
    // 社区源挂了就是空数组，不能抛错
    expect(mergeHostCandidates([], [])).toEqual([])
  })
})

describe('分用途测速与排序（速度优先，延迟破平）', () => {
  it('archive 探测与 raw 同判据：2xx 且非 HTML', () => {
    expect(isArchiveProbeOk(200, 'application/zip')).toBe(true)
    expect(isArchiveProbeOk(200, 'application/octet-stream')).toBe(true)
    expect(isArchiveProbeOk(200, 'text/html; charset=utf-8')).toBe(false)
    expect(isArchiveProbeOk(404, 'application/zip')).toBe(false)
    expect(buildProbeUrlForKind('https://m.example/gh/', 'archive')).toBe(`https://m.example/gh/${PROBE_TARGET_ARCHIVE}`)
    expect(buildProbeUrlForKind('https://m.example/', 'raw')).toContain('raw.githubusercontent.com')
  })

  it('失败行永远排在可用行之后', () => {
    expect(compareProbe({ ok: false, latencyMs: 1 }, { ok: true, latencyMs: 9999 })).toBeGreaterThan(0)
    expect(compareProbe({ ok: true, latencyMs: 9999 }, { ok: false, latencyMs: 1 })).toBeLessThan(0)
  })

  it('带宽差超过平档阈值时速度优先（TTFB 快但窄带的不许当最快）', () => {
    const narrow = { ok: true, latencyMs: 80, speedBps: 200 * 1024 }
    const wide = { ok: true, latencyMs: 900, speedBps: 2 * 1024 * 1024 }
    expect(compareProbe(wide, narrow)).toBeLessThan(0)
    expect(compareProbe(narrow, wide)).toBeGreaterThan(0)
  })

  it('速度同档（±15% 内）时用首字节延迟破平', () => {
    const slowTtfb = { ok: true, latencyMs: 300, speedBps: 1024 * 1024 }
    const fastTtfb = { ok: true, latencyMs: 90, speedBps: 1050 * 1024 }
    expect(compareProbe(fastTtfb, slowTtfb)).toBeLessThan(0)
    expect(compareProbe(slowTtfb, fastTtfb)).toBeGreaterThan(0)
  })

  it('一方没测出带宽时有数据的优先；都没有就只看延迟', () => {
    expect(compareProbe({ ok: true, latencyMs: 800, speedBps: 1024 }, { ok: true, latencyMs: 90 })).toBeLessThan(0)
    expect(compareProbe({ ok: true, latencyMs: 800 }, { ok: true, latencyMs: 90 })).toBeGreaterThan(0)
  })
})

describe('insteadOf 最长前缀语义（与 git 实际行为一致）', () => {
  const entries = parseGitInsteadOf(
    [
      'url.https://a.example/https://github.com/.insteadof https://github.com/',
      'url.https://b.example/https://github.com/me/.insteadof https://github.com/me/',
    ].join('\n'),
  )

  it('同址多条时取最长匹配前缀，而不是第一条', () => {
    expect(resolveInsteadOf(entries, 'https://github.com/me/repo.git')?.subsection).toBe('https://b.example/https://github.com/me/')
    expect(resolveInsteadOf(entries, 'https://github.com/other/repo.git')?.subsection).toBe('https://a.example/https://github.com/')
  })

  it('pushInsteadOf 条目不参与 fetch 判定；无匹配返回 null', () => {
    expect(resolveInsteadOf(parseGitInsteadOf('url.git@github.com:.pushinsteadof https://github.com/'))).toBeNull()
    expect(resolveInsteadOf(entries, 'https://gitlab.com/a/b')).toBeNull()
  })
})

describe('push 去向策略（读取走镜像，push 不被劫持）', () => {
  it('两种模式各对应一个子节，且都能被识别为“本应用写入”', () => {
    expect(pushSubsectionOf('direct')).toBe(GITHUB_BASE)
    expect(pushSubsectionOf('ssh')).toBe(GIT_SSH_PUSH_SUBSECTION)
    expect(isManagedPushSubsection(pushSubsectionOf('direct'))).toBe(true)
    expect(isManagedPushSubsection(pushSubsectionOf('ssh'))).toBe(true)
    // 用户自建的规则不是我们的：关闭时不能替用户删
    expect(isManagedPushSubsection('https://ghproxy.net/https://github.com/')).toBe(false)
  })

  it('按子节单独 unset（切模式时先清另一条，不留叠加）', () => {
    expect(buildGitPushUnsetSubsectionArgs(GIT_SSH_PUSH_SUBSECTION)).toEqual([
      'config',
      '--global',
      '--unset',
      `url.${GIT_SSH_PUSH_SUBSECTION}.pushInsteadOf`,
    ])
  })
})

describe('ssh 托管块必须抢在 Host * 之前（同名关键字首个生效）', () => {
  const cfg = 'Host work\n  HostName work.example\n  User me\n\nHost *\n  ServerAliveInterval 30\n'

  it('插入位置在首个 Host * 之前', () => {
    const out = applySshBlock(cfg)
    expect(out.indexOf(SSH_BLOCK_BEGIN)).toBeGreaterThanOrEqual(0)
    expect(out.indexOf(SSH_BLOCK_BEGIN)).toBeLessThan(out.indexOf('Host *'))
  })

  it('插到中间后 apply → remove 仍精确还原（不留空行、不吞上一行）', () => {
    expect(removeSshBlock(applySshBlock(cfg))).toBe(cfg)
  })

  it('Host * 在文件开头时块前不补空行，删除后同样恒等', () => {
    const raw = 'Host *\n  ServerAliveInterval 30\n'
    expect(applySshBlock(raw).startsWith(SSH_BLOCK_BEGIN)).toBe(true)
    expect(removeSshBlock(applySshBlock(raw))).toBe(raw)
  })

  it('没有 Host * 时追加到末尾', () => {
    const raw = 'Host a\n  HostName a.example\n'
    const out = applySshBlock(raw)
    expect(out.indexOf(SSH_BLOCK_BEGIN)).toBeGreaterThan(out.indexOf('HostName a.example'))
    expect(removeSshBlock(out)).toBe(raw)
  })
})

describe('一键懒人模式：连续失败计数与淘汰判定', () => {
  it('成功即清零，失败递增并封顶在淘汰阈值（不必记到很大）', () => {
    expect(nextFailStreak(undefined, true)).toBe(0)
    expect(nextFailStreak(2, true)).toBe(0)
    expect(nextFailStreak(undefined, false)).toBe(1)
    expect(nextFailStreak(1, false)).toBe(2)
    expect(nextFailStreak(2, false)).toBe(PROBE_RETIRE_STREAK)
    // 已到阈值后再失败不再累加：配置文件不该无限增长
    expect(nextFailStreak(PROBE_RETIRE_STREAK, false)).toBe(PROBE_RETIRE_STREAK)
  })

  it('脏数据（负值 / NaN / 非数字）按 0 起算，不会把镜像误判为失效', () => {
    expect(nextFailStreak(-5, false)).toBe(1)
    expect(nextFailStreak(Number.NaN, false)).toBe(1)
    expect(shouldRetire(Number.NaN)).toBe(false)
    expect(shouldRetire(undefined)).toBe(false)
  })

  it('单次抖动不淘汰：只有连续到阈值才判失效', () => {
    expect(shouldRetire(1)).toBe(false)
    expect(shouldRetire(PROBE_RETIRE_STREAK - 1)).toBe(false)
    expect(shouldRetire(PROBE_RETIRE_STREAK)).toBe(true)
  })
})

describe('一键懒人模式：最优镜像选择', () => {
  it('只在可用行里选，速度优先、延迟破平（与 compareProbe 同口径）', () => {
    const rows = [
      { id: 'dead', ok: false, latencyMs: 5 },
      { id: 'fast-ttfb', ok: true, latencyMs: 20, speedBps: 60 * 1024 },
      { id: 'fast-bw', ok: true, latencyMs: 300, speedBps: 900 * 1024 },
    ]
    expect(pickBestProbe(rows)?.id).toBe('fast-bw')
  })

  it('速度同档时用首字节延迟破平；全部不可用返回 null', () => {
    const rows = [
      { id: 'slow', ok: true, latencyMs: 400, speedBps: 1000 },
      { id: 'quick', ok: true, latencyMs: 80, speedBps: 1040 },
    ]
    expect(pickBestProbe(rows)?.id).toBe('quick')
    expect(pickBestProbe([{ id: 'x', ok: false, latencyMs: null }])).toBeNull()
    expect(pickBestProbe([])).toBeNull()
  })
})

describe('一键懒人模式：自动采纳候选', () => {
  const candidates = [
    { prefix: 'https://a.example/', name: 'A', ok: true, latencyMs: 120 },
    { prefix: 'https://dead.example/', name: 'Dead', ok: false, latencyMs: null },
    { prefix: 'https://known.example/', name: 'Known', ok: true, latencyMs: 30 },
    { prefix: 'https://c.example/', name: 'C', ok: true, latencyMs: 60 },
    { prefix: 'https://b.example/', name: 'B', ok: true, latencyMs: 200 },
  ]

  it('只取实测可用、清单里没有的，并按延迟排名限量', () => {
    const picks = pickAutoAdopt(candidates, ['https://known.example/'])
    // C(60ms) < A(120ms) < B(200ms)，Dead 不可用、Known 已在清单
    expect(picks.map(c => c.name)).toEqual(['C', 'A'])
    expect(picks).toHaveLength(AUTO_ADOPT_LIMIT)
  })

  it('limit 可收紧（剩余清单容量不足时只补差额）', () => {
    expect(pickAutoAdopt(candidates, ['https://known.example/'], 1).map(c => c.name)).toEqual(['C'])
    expect(pickAutoAdopt(candidates, ['https://known.example/'], 0)).toEqual([])
  })

  it('候选全部不可用时一个也不采纳（宁缺勿滥，不写配置）', () => {
    expect(pickAutoAdopt([{ prefix: 'https://dead.example/', name: 'Dead', ok: false, latencyMs: null }], [])).toEqual([])
  })
})
