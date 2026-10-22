/**
 * 文件类型图标注册表的静态验收。
 *
 * 为什么值得为一张表写测试：这张表有 3 个入口（精确文件名、扩展名、兜底），
 * 而错的那一种**不会编译报错也不会红**——只是某个文件在列表里顶着一张不相干的
 * 图形，或者更糟，落到"没有图标"的空位上。所以把"每个输入都必须解析出一个能画的
 * 条目"和"同一扩展名不许登记两次"变成断言。
 */
import { describe, expect, it } from 'vitest'
import {
  EXT_GROUPS,
  FALLBACK_ICON,
  FILE_TYPE_ICONS,
  NAME_ICONS,
  NAME_PREFIX_ICONS,
  iconForFile,
  resolveKey,
  usesBrandColor,
} from './file-type-icons'

/** 品牌 hex 与主题令牌引用两种合法形态。 */
const BRAND_HEX = /^#[0-9a-f]{6}$/
const TOKEN_REF = /^var\(--accent-[a-z0-9-]+\)$/

describe('注册表条目本身', () => {
  it('每个条目都有可绘制的 path、合法颜色和名字', () => {
    for (const [key, icon] of Object.entries(FILE_TYPE_ICONS)) {
      expect(icon.d.length, `${key} 没有 path`).toBeGreaterThan(0)
      icon.d.forEach((d, i) => {
        expect(d.trim().charAt(0), `${key}[${i}] 的 d 不是以命令开头`).toMatch(/^[Mm]/)
      })
      expect(icon.label, `${key} 缺 label`).toBeTruthy()
      const colorOk = BRAND_HEX.test(icon.color) || TOKEN_REF.test(icon.color)
      expect(colorOk, `${key} 的颜色既不是品牌 hex 也不是 --accent-* 令牌：${icon.color}`).toBe(true)
    }
  })

  it('取品牌色的条目一律写 hex（品牌色刻意不随主题变），其余读令牌随主题走', () => {
    // 「取品牌色」比「有品牌图形」宽：Java/C#/PowerShell/Word/Excel/PDF 的商标图形
    // 已从 simple-icons 下架，只能自绘形状配公布标色——颜色仍属品牌，形状不属。
    for (const [key, icon] of Object.entries(FILE_TYPE_ICONS)) {
      if (usesBrandColor(key)) expect(icon.color, `${key} 取品牌色却读令牌`).toMatch(BRAND_HEX)
      else expect(icon.color, `${key} 不取品牌色却写死 hex`).toMatch(TOKEN_REF)
    }
  })

  it('兜底条目本身可绘制，否则未知扩展名会拿到空图标', () => {
    expect(FALLBACK_ICON.d.length).toBeGreaterThan(0)
  })
})

describe('resolveKey 的入口优先级', () => {
  it('目录一律走目录图形，名字像什么都不算', () => {
    expect(resolveKey('backup.zip', true)).toBe('folder')
    expect(resolveKey('Dockerfile', true)).toBe('folder')
  })

  it('精确文件名优先于扩展名', () => {
    // docker-compose.yml 若按扩展名解析会掉进 yaml，而它该拿 Docker 标
    expect(resolveKey('docker-compose.yml', false)).toBe('docker')
    expect(resolveKey('nginx.conf', false)).toBe('nginx')
    expect(resolveKey('Dockerfile', false)).toBe('docker')
    expect(resolveKey('Dockerfile.dev', false)).toBe('docker')
  })

  it('文件名匹配大小写不敏感', () => {
    expect(resolveKey('README.MD', false)).toBe(resolveKey('readme.md', false))
    expect(resolveKey('NGINX.CONF', false)).toBe('nginx')
  })

  it('多点名取最后一段：backup.sql.gz 是压缩包不是数据库', () => {
    expect(resolveKey('backup.sql.gz', false)).toBe('archive')
    expect(resolveKey('app.config.json', false)).toBe('json')
  })

  it('隐藏文件与无扩展名都解析得出东西，不许返回 undefined', () => {
    for (const name of ['.gitignore', '.env', 'LICENSE', 'README', 'main', 'a.b.c.d']) {
      expect(iconForFile(name, false).d.length, `${name} 解析不出图形`).toBeGreaterThan(0)
    }
  })

  it('密钥/证书单独成类，不跟配置文件混在一起', () => {
    for (const name of ['server.pem', 'client.crt', 'id_rsa', 'id_ed25519.pub', 'keystore.p12']) {
      expect(resolveKey(name, false), name).toBe('cert')
    }
  })
})

describe('常见类型各归各位', () => {
  const cases: Array<[string, string]> = [
    ['index.ts', 'typescript'],
    ['app.jsx', 'javascript'],
    ['update_version.py', 'python'],
    ['mkdocs.yml', 'yaml'],
    ['pyproject.toml', 'toml'],
    ['mineru.template.json', 'json'],
    ['README.md', 'markdown'],
    ['LICENSE', 'document'],
    ['SECURITY.md', 'markdown'],
    ['hermes.log', 'log'],
    ['ntuser.ini', 'config'],
    ['install.ps1', 'powershell'],
    ['run_docker_vllm.sh', 'gnubash'],
    ['photo.PNG', 'image'],
    ['site.tar.bz2', 'archive'],
    ['libssl.so', 'binary'],
    ['tool.exe', 'binary'],
    ['data.csv', 'spreadsheet'],
    ['sheet.xlsx', 'excel'],
    ['report.pdf', 'pdf'],
    ['report.docx', 'word'],
    ['deck.pptx', 'powerpoint'],
    ['font.woff2', 'font'],
    ['clip.mp4', 'video'],
    ['voice.opus', 'audio'],
    ['infra.tf', 'terraform'],
    ['schema.graphql', 'graphql'],
    ['app.Style.css', 'css'],
    ['.gitignore', 'git'],
    ['.gitattributes', 'git'],
    ['Component.vue', 'vue'],
    ['mod.rs', 'rust'],
    ['Main.java', 'java'],
    ['Program.cs', 'csharp'],
  ]
  for (const [name, key] of cases) {
    it(`${name} → ${key}`, () => {
      expect(resolveKey(name, false)).toBe(key)
    })
  }

  it('没见过的扩展名落到兜底，且兜底就是那张通用文档', () => {
    expect(resolveKey('widget.qqqzzz', false)).toBe('unknown')
    expect(iconForFile('widget.qqqzzz', false)).toBe(FALLBACK_ICON)
  })
})

describe('登记表自身的一致性', () => {
  it('同一扩展名不许登记在两个类型下', () => {
    // 后写的会静默覆盖先写的，而两条规则各自都"看起来对"——h 归 C 还是 C++、
    // jar 归压缩包还是 Java，全靠这条断言逼着当场决定，而不是等列表里出现错图形。
    const seen = new Map<string, string>()
    const dupes: string[] = []
    for (const [key, list] of Object.entries(EXT_GROUPS)) {
      for (const ext of list) {
        if (seen.has(ext)) dupes.push(`${ext}: ${seen.get(ext)} 与 ${key}`)
        else seen.set(ext, key)
      }
    }
    expect(dupes).toEqual([])
  })

  it('每个映射目标都是注册表里真画得出来的条目', () => {
    // 拼错一个 key 不会报错，只会让那一族文件悄悄拿到兜底图形
    const missing: string[] = []
    const has = (k: string) => k in FILE_TYPE_ICONS
    for (const key of Object.keys(EXT_GROUPS)) if (!has(key)) missing.push(`EXT_GROUPS.${key}`)
    for (const key of Object.values(NAME_ICONS)) if (!has(key)) missing.push(`NAME_ICONS→${key}`)
    for (const [, key] of NAME_PREFIX_ICONS) if (!has(key)) missing.push(`NAME_PREFIX→${key}`)
    expect(missing).toEqual([])
  })
})
