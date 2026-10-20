/**
 * 主题令牌的静态验收（spec 2026-09-29-home-command-panel · 接缝 S4）
 *
 * 为什么在 CSS 上写测试：新主题是一组 `data-theme` 变量覆盖，**漏掉哪一条都不会
 * 编译报错**，只会在米纸底上留着一块深色值 —— 而这类缺陷恰好长在"没人截图看过"
 * 的角落里。所以把"覆盖集合"与文件头自己写下的约定变成断言，而不是靠人眼扫。
 */
import { describe, expect, it } from 'vitest'
// ?raw 由 vite/client 声明类型：本文件在 tsconfig.web 下检查，那里没有 node 类型，
// 读 node:fs 会让 typecheck 报错（测试跑在 vitest 的 node 环境里，两边不是一回事）。
import rawVariables from './variables.css?raw'
import rawGlobal from './global.css?raw'
import rawSshView from '../views/ssh/SshView.vue?raw'
import rawQuickCommandBar from '../components/ssh/QuickCommandBar.vue?raw'
import rawTitleBar from '../layouts/TitleBar.vue?raw'
import rawAgentTraceInline from '../components/agent/AgentTraceInline.vue?raw'
import rawHomeComposer from '../components/home/HomeComposer.vue?raw'
import rawRunModes from '../utils/run-modes?raw'
import rawApprovalCard from './approval-card.css?raw'
import rawSettingsGroups from '../components/settings/panels/settings-groups.css?raw'
import rawSettingsPanelBase from '../components/settings/panels/settings-panel-base.css?raw'
import rawButton from './button.css?raw'

/**
 * 剥掉注释再断言（CSS 块注释与模板里的 HTML 注释）。
 *
 * 这些注释会解释"先前是 border: 1px solid"、"先前是 bottom: calc(100% + 6px)" ——
 * 而本文件的骨干断言恰恰是 not.toMatch 那些写法。不剥的话，一条写清楚了的说明
 * 就把自己的回归断言打红（第一次跑就撞上了）。声明与注释分开看，两边才都能好好写。
 */
const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

const css = strip(rawVariables)
const globalCss = strip(rawGlobal)
const sshView = strip(rawSshView)
const quickCommandBar = strip(rawQuickCommandBar)
const titleBar = strip(rawTitleBar)
const agentTraceInline = strip(rawAgentTraceInline)
const homeComposer = strip(rawHomeComposer)
const runModes = strip(rawRunModes)

/** 共享样式表（不是某个组件内联的 <style> 块）：它们就是令牌层的直接消费方，
 *  所以单独按"裸引用"约束。留空字符串等于守卫恒真，故先钉住三份都真读到了东西
 *  —— vitest 的 css.include 是白名单，不在表里的 .css 用 ?raw 拿到的是空串。 */
const SHARED_STYLESHEETS: Record<string, string> = {
  'approval-card.css': strip(rawApprovalCard),
  'button.css': strip(rawButton),
  'settings-groups.css': strip(rawSettingsGroups),
  'settings-panel-base.css': strip(rawSettingsPanelBase),
}

/** 取某个 `[data-theme="x"]` 块的原文（到第一个 `}` 为止 —— 主题块内不嵌套规则） */
function blockOf(theme: string): string {
  const marker = `[data-theme="${theme}"]`
  const start = css.indexOf(marker)
  if (start < 0) throw new Error(`找不到主题块：${marker}`)
  const end = css.indexOf('}', start)
  return css.slice(start, end < 0 ? undefined : end)
}

const tokensOf = (block: string) =>
  [...block.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(m => m[1]).sort()

const valueOf = (block: string, token: string) =>
  block.match(new RegExp(`${token}:\\s*([^;]+);`))?.[1]?.trim()

/** `:root` 里的默认值：令牌文件按 :root → 各主题块排序，首个命中即 :root */
const rootValueOf = (token: string) => valueOf(css, token)

/** 令牌被"读"了就算消费，带不带回落都一样读。回落本身不写：令牌与它的消费方
 *  同一批落地，`var(--x, 默认值)` 里的默认值是 :root 那条声明的第二份副本 ——
 *  改 :root 时它会静默留在后面，正是这轮一路在防的"两份定义"。 */
const readsVar = (src: string, token: string) =>
  new RegExp(`var\\(${token}[,)]`).test(src)

/** 找出「给 :root 令牌写回落值」的位置。按 :root 令牌表迭代，而不是手抄一枚枚清单：
 *  抄的那份只覆盖今天点名的几枚，第 N+1 枚加进 :root 时没有任何东西逼人登记。
 *  运行时注入的属性（--theme-gradient / --gradient-alpha / --chat-font-size /
 *  --rm-grid-cols / --app-bg-*）不在 :root 定义，它们行上的回落是**活**的（setProperty
 *  之前的那一次绘制要读得出东西），按 :root 迭代的写法天然放过。 */
function fallbackOffenders(sheets: Record<string, string>): string[] {
  const defined = tokensOf(css.match(/:root\s*\{[\s\S]*?\}/)![0])
  const hits: string[] = []
  for (const [file, src] of Object.entries(sheets)) {
    for (const token of defined) {
      if (new RegExp(`var\\(\\s*${token}\\s*,`).test(src)) hits.push(`${file} ${token}`)
    }
  }
  return hits
}

/** 取一条选择器规则的**声明体**（`{}` 内、不跨块）。
 *  直接用 `[\s\S]*?` 往后扫会一路读到下一条规则 —— 于是"这条规则里有 X"其实是
 *  "这之后的某处有 X"，断言就悄悄不再钉它声称的那件事了（本文件 :203 撞过）。 */
function ruleBlock(src: string, selector: string): string {
  const m = src.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`))
  if (!m) throw new Error(`找不到规则：${selector}`)
  return m[1]
}

const atelier = blockOf('atelier')
const atelierTokens = tokensOf(atelier)

/** 全部组件与样式源码：令牌消费方与"强调色底上不许写死白字"这两条都要按全仓扫，
 *  手抄清单只会覆盖今天这几条，新增的第 7 条令牌不会被强制登记。 */
const SOURCE_FILES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob('../**/*.{vue,css}', { query: '?raw', import: 'default', eager: true }),
  ).map(([path, raw]) => [path.replace(/^\.\./, 'src'), strip(raw as string)])
)
const ALL_SOURCES = Object.values(SOURCE_FILES).join('\n')

/** 历史遗留、本轮不追的孤儿令牌：变量表里定义了但全仓没人读。
 *  往里加东西等于认下一条新债，所以必须写清是 which 以及为什么。 */
const ORPHAN_TOKENS_ALLOWLIST: string[] = []

/** 上色的结构画布：纸纹必须落在这里，而不是 body（见 structural-canvas 用例）。
    首页里的消息流与内联过程不在列 —— 它们长在首页这块浮层里，纸从首页透上来。 */
const STRUCTURAL_CANVASES: Record<string, string> = {
  '.ssh-view / .term-tabs': sshView,
  '.titlebar': titleBar,
  '.quick-command-bar': quickCommandBar,
}

/** 「不许把主题写死」这条约束的管辖区：终端页那一族 + 设置那一族。
 *  按目录前缀扫，新文件自动在网内（两处共用，颜色与字族两条断言同一张网）。 */
const TOKENIZED_SCOPE = [
  'src/components/ssh/',
  'src/views/ssh/',
  'src/components/home/',
  'src/components/common/',
  'src/layouts/',
  // 设置这一族（7 个面板 + 3 个控件 + 2 份共享样式）与终端页同一个病灶：
  // 深色主题的字面量抄进组件，切浅色/手记就糊，切强调色又纹丝不动。
  // 2026-10-08 两轮清完，这里连屏一起钉住。
  'src/components/settings/',
]

const scopedSources = () =>
  Object.entries(SOURCE_FILES).filter(([file]) =>
    TOKENIZED_SCOPE.some((prefix) => file.startsWith(prefix)),
  )

/** 一条声明的字族取值（`font-family:` 到本条声明结束）。跨行声明也认。 */
function fontFamilies(src: string): string[] {
  return [...src.matchAll(/(?:^|[;{])\s*font-family:\s*([^;}]*)/g)].map(m => m[1].trim())
}

describe('atelier（手记）主题的令牌覆盖', () => {
  it('浅色主题覆盖的令牌，手记一个都不能少', () => {
    // 少一条 = 那条在米纸底上仍是深色主题的值。深色底写死的 status-*/danger-*/
    // card-perm-* 搬到纸上不是"不好看"，是文字对比度直接不够读不出来。
    const missing = tokensOf(blockOf('light')).filter(t => !atelierTokens.includes(t))
    expect(missing).toEqual([])
  })

  it('--bg-* 与同名 --bg-*-solid 同值（variables.css 头部写死的约定）', () => {
    // global.css 的 [data-gradient="on"] 用 -solid 派生半透明版；两者不同值时
    // 渐变主题下的面板底与实色主题差一档，且不会有任何报错。
    for (const base of ['--bg-elevated', '--bg-input']) {
      expect(valueOf(atelier, `${base}-solid`), `${base}-solid 要与 ${base} 同值`)
        .toBe(valueOf(atelier, base))
    }
  })

  it('手记一律本地资源：整份令牌表里没有任何要联网才拿得到的引用', () => {
    // 预览稿实测 fonts.googleapis.com 不可达（ERR_CONNECTION_REFUSED），
    // 当时整套界面静默掉进回退字体。Electron 断网/离线分发是常态不是边缘场景。
    //
    // 断言按**风险形状**收窄，不再一刀切禁 `url(`：纸纹颗粒是一段内联 SVG
    // （`url("data:image/svg+xml,…")`），它零网络依赖，正是不该被这条规则误伤的
    // 东西。禁不掉的是"要联网才成立"的引用：@import / @font-face /
    // 指向协议或路径的 url() / 指向素材文件的 url()。
    expect(css).not.toMatch(/@import/)
    expect(css).not.toMatch(/@font-face/)
    expect(css).not.toMatch(/url\(\s*['"]?(?:https?:)?\/\//)
    expect(css).not.toMatch(/url\(\s*['"]?[^)]*\.(?:png|jpe?g|gif|webp|woff2?|ttf|otf|eot)\b/i)
  })

  it('纸纹必须是内联 data: SVG，不许换成素材路径', () => {
    // 上一条例子只说"不许有网络引用"，这条把正面形状钉住：换成 ../assets/grain.png
    // 依然过得了上一条，但打包/asar 路径一变就静默没有纹理。
    expect(valueOf(atelier, '--canvas-bg')).toContain('data:image/svg+xml')
  })

  it('字体栈以通用族收尾 —— 一个都没装时落到 serif/mono，而不是悄悄掉回无衬线', () => {
    expect(valueOf(atelier, '--font-sans')?.endsWith('serif')).toBe(true)
    expect(valueOf(atelier, '--font-mono')?.endsWith('monospace')).toBe(true)
    // --font-display 是标题字族，同一条规则管着它
    expect(valueOf(atelier, '--font-display')?.endsWith('serif')).toBe(true)
  })

  it('手记不写 --accent-primary / --accent-on：那两个令牌归 JS 管', () => {
    // applyAccent 在 documentElement 上 setProperty，内联样式压过 [data-theme] 块
    // —— 在主题里写强调色是一条永远不生效的声明（"亮的空开关"的同族）。
    // 朱砂因此做成一条强调色（Accent: verm），不是塞进主题令牌。
    // --accent-on（压在强调色底上的前景色）同属这一族：它必须由 ACCENT_MAP 算出来。
    expect(atelierTokens).not.toContain('--accent-primary')
    expect(atelierTokens).not.toContain('--accent-primary-rgb')
    expect(atelierTokens).not.toContain('--accent-on')
  })

  it('新令牌在 :root 的默认值 = 补它们之前的行为（其余主题逐像素不变）', () => {
    // 这一组断言是"主题只加令牌、不动版面"这句承诺的机器版本：默认值一旦偏离
    // 今天的有效值，深色/浅色/森林等主题就会跟着变色，而没人会为此跑一遍截图。
    expect(rootValueOf('--canvas-bg')).toBe('var(--bg-base)')
    expect(rootValueOf('--card-foot-bg')).toBe('transparent')
    expect(rootValueOf('--card-foot-rule')).toBe('1px solid var(--divider-primary)')
    expect(rootValueOf('--font-display')).toBe('var(--font-sans)')
    expect(rootValueOf('--accent-on')).toBe('#ffffff')
    // ::selection 今天就是「强调色 35% 透明」，改读令牌时这个表达式一个字都不能动
    expect(rootValueOf('--wash')).toBe('color-mix(in srgb, var(--accent-primary) 35%, transparent)')
    expect(globalCss).toMatch(/::selection\s*\{[^}]*background:\s*var\(--wash\)/)
  })

  it('每条令牌都有真实消费方（文件头维护约定：无消费方 = 失效设置项）', () => {
    // 扫全表而不是手抄清单：手抄的那份只覆盖今天这 6 条，第 7 条加进 :root 时
    // 没有任何东西会逼人登记它的消费方 —— 而这正是这条约定唯一防得住的东西。
    const defined = tokensOf(css.match(/:root\s*\{[\s\S]*?\}/)![0])
    const orphan = defined.filter(
      t => !readsVar(ALL_SOURCES, t) && !ORPHAN_TOKENS_ALLOWLIST.includes(t),
    )
    expect(orphan).toEqual([])
  })

  it('新令牌一律裸引用：`var(--x, 回落)` 是 :root 那条声明的第二份副本', () => {
    // 令牌与消费方同批落地，回落就永远不会被读到；它只在 :root 被改时留在后面。
    for (const token of ['--canvas-bg', '--wash', '--card-foot-bg', '--card-foot-rule', '--font-display', '--accent-on']) {
      expect(ALL_SOURCES, `${token} 有消费方写了回落值`).not.toMatch(new RegExp(`var\\(${token},`))
    }
  })

  it('共享样式表里全部 :root 令牌一律裸引用（迭代令牌表，不抄清单）', () => {
    // 上一条按枚点名，approval-card.css 就带着 14 处 var() 回落躲过去了，而那些
    // 回落早已和 :root 分叉：--accent-cyan 是 #9cdcfe、回落写 #38bdf8，--danger-text
    // 是 #ff6b6b、回落写 #ef4444。分叉又读不到 = 假保险，所以这条扫**整张** :root 表。
    // 另外钉住"文件真读到了"：css.include 是白名单，漏一项就把空串当成"没有违规"。
    expect(Object.keys(SHARED_STYLESHEETS).sort()).toEqual([
      'approval-card.css',
      'button.css',
      'settings-groups.css',
      'settings-panel-base.css',
    ])
    for (const [file, src] of Object.entries(SHARED_STYLESHEETS)) {
      expect(src.length, `${file} 读到空串 —— vitest.config.ts 的 css.include 漏了它`)
        .toBeGreaterThan(80)
    }
    expect(fallbackOffenders(SHARED_STYLESHEETS)).toEqual([])
  })

  it('裸引用守卫自己会红（变异自检：解析类守卫绿着骗人比没有更糟）', () => {
    const target = SHARED_STYLESHEETS['approval-card.css']
    const mutated = {
      'approval-card.css': target.replace('.ap-code {', '.ap-code { color: var(--accent-cyan, #38bdf8);'),
    }
    expect(mutated['approval-card.css']).not.toBe(target)
    expect(fallbackOffenders(mutated)).toEqual(['approval-card.css --accent-cyan'])
  })

  it('强调色底上不许写死白字：全仓扫，前景只能读 --accent-on', () => {
    // 强调色板里有「白色」这枚（#ffffff），选中它时每条 `background: var(--accent-primary)`
    // 配 `color: #fff` 的规则都是白底白字 —— 按钮还在、字没了。--accent-on 由
    // applyAccent 按对比度算出来（见 appearance-tokens.contrastOnAccent）。
    // 这条按全仓扫：一处一处点名正是要防的"第二份清单"。
    const offenders: string[] = []
    for (const [file, src] of Object.entries(SOURCE_FILES)) {
      for (const m of src.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
        const [, sel, body] = m
        if (/background(?:-color)?:\s*var\(--accent-primary\)/.test(body)
          && /color:\s*(#fff\b|#ffffff|white)/.test(body)) {
          offenders.push(`${file} ${sel.trim().split('\n').pop()!.trim()}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('终端页面那一族不许把颜色写成字面量：只能读令牌，或自报组件局部令牌', () => {
    // 这轮在终端页修的一整批（快捷命令条 #cccccc、补全浮层 #79c0ff、标题条 #4ade80
    // 与窗口按钮 #cccccc、确认框 hover 跳成 #4a8fe3、toast 四档 ANSI 色）都是同一个形状：
    // 深色主题的字面量抄进了组件，切到浅色/手记就糊成一片，而切强调色时它纹丝不动。
    // 漏一条不会编译报错，只会在米纸上留一块黑 —— 所以按目录前缀扫，新文件自动在网内。
    //
    // 唯一豁免的形状是"组件局部令牌的声明"（`--x: #hex`）：那是 GpuNvtopPanel 立的做法
    // —— 固定深色的终端复刻面板把调色板集中在一块并写明改色只改这里。写进属性值就没人
    // 再看第二遍，声明成局部令牌至少是一处可读、可核对的来源。
    const inScope = scopedSources()
    // 空串守卫：glob 没命中、或 ?raw 拿到的是空模块（vitest 的 css.include 是白名单）时，
    // offender 恒空 —— 那条断言就成了假的绿。逐个钉住"真读到了东西"。
    expect(inScope.length).toBeGreaterThan(20)
    const blank = inScope.filter(([, src]) => src.trim().length < 20).map(([file]) => file)
    expect(blank, '这些在网内的文件读到了空串').toEqual([])

    const offenders: string[] = []
    for (const [file, src] of inScope) {
      src.split('\n').forEach((line, i) => {
        if (!/#[0-9a-fA-F]{3,8}\b/.test(line)) return
        // 注释里提一句"先前是 #cccccc"是这仓的写法，不是违规（strip 只剥块注释与 HTML 注释，
        // TS 的 // 行注释得在这里自己跳过）
        if (/^\s*(\/\/|\*|\/\/\/)/.test(line)) return
        // 豁免形状一：CSS 自定义属性声明（组件局部调色板，GpuNvtopPanel 立的做法）
        if (/^\s*--[a-z0-9-]+\s*:/.test(line)) return
        // 豁免形状二：UPPER_SNAKE 命名常量 —— 同一件事的 JS 版本：一处声明、有名字、可核对，
        // 而不是把 hex 混进模板串/内联样式里。TERMINAL_UNDERLAY 这种算对比度的输入值属于这类。
        if (/^\s*(?:const|let)\s+[A-Z][A-Z0-9_]*\b\s*[:=]/.test(line)) return
        offenders.push(`${file}:${i + 1} ${line.trim()}`)
      })
    }
    expect(offenders).toEqual([])

    // 变异自检：解析器绿着骗人比没有更糟（同上方"裸引用守卫自己会红"一条做法）
    const hit = (l: string) => /#[0-9a-fA-F]{3,8}\b/.test(l)
      && !/^\s*(\/\/|\*|\/\/\/)/.test(l)
      && !/^\s*--[a-z0-9-]+\s*:/.test(l)
      && !/^\s*(?:const|let)\s+[A-Z][A-Z0-9_]*\b\s*[:=]/.test(l)
    expect(hit('  color: #cccccc;')).toBe(true)
    expect(hit('  border: 1px solid #2a2a2a;')).toBe(true)
    expect(hit('  font-family: var(--font-mono, monospace); color: #79c0ff;')).toBe(true)
    expect(hit('  // 先前是 #cccccc，现在读令牌')).toBe(false)
    expect(hit('  --nv-bg: #050505;')).toBe(false)
    expect(hit('const TERMINAL_UNDERLAY = \'#17181c\'')).toBe(false)
  })

  it('已经存在的 --shadow-lg 不许再被抄成字面量', () => {
    // --shadow-lg 三套主题各有各的值（深色 0.4 纯黑 / 浅色 0.15 / 手记 0.18 且带纸色底），
    // 而组件里抄的那几处 `0 8px 24px rgba(0,0,0,0.4)` 与深色那一档逐字相同 —— 抄的就是深色，
    // 切到米纸上会压出一块过重的黑影。和颜色同一条病：漏了不报错，只是不跟主题。
    // 只认 box-shadow 属性用法，variables.css 里那三条令牌声明本身不会被这条抓到。
    const COPIED = /box-shadow:\s*0 8px 24px\s+rgba\(/
    const offenders: string[] = []
    for (const [file, src] of Object.entries(SOURCE_FILES)) {
      src.split('\n').forEach((line, i) => {
        if (COPIED.test(line)) offenders.push(`${file}:${i + 1} ${line.trim()}`)
      })
    }
    // 债账（同 ORPHAN_TOKENS_ALLOWLIST 的纪律）：并行会话正拿着的文件。
    // 2026-10-13：SftpView.vue 的那处抄写随传输样式整体迁出而消失，豁免按本测试
    // 自己写下的口径删掉 —— 债还上之后这条若还挂着，就是在替已不存在的代码说话。
    const DEBT: string[] = []
    expect(offenders.filter((o) => !DEBT.some((d) => o.startsWith(d)))).toEqual([])

    // 变异自检：正则绿着骗人比没有更糟
    expect(COPIED.test('  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);')).toBe(true)
    expect(COPIED.test('  box-shadow: 0 8px 24px rgba(0,0,0,0.4);')).toBe(true)
    expect(COPIED.test('  box-shadow: var(--shadow-lg);')).toBe(false)
    expect(COPIED.test('  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);')).toBe(false)
  })

  it('同一族里也不许把字族写成字面量：等宽只能读 --font-mono', () => {
    // 手记主题重写的是整套字族（--font-sans 落到 Georgia/宋体衬线、--font-mono 落到
    // Cascadia Mono），而组件里抄一句 `ui-monospace, Consolas, monospace` 或
    // `'JetBrains Mono', 'Fira Code', Consolas, monospace` 就绕过了它 —— 米纸衬线版面上
    // 独独一处工具名/模型 ID 是 Windows 控制台字。和颜色同一条病：漏了不报错，只是不跟主题。
    // 只认两种写法：`inherit`（跟着父级，本轮 .htt-btn 立的口径）与读字族令牌。
    const offenders: string[] = []
    for (const [file, src] of scopedSources()) {
      for (const value of fontFamilies(src)) {
        if (!/^(?:inherit|var\(--font-[a-z0-9-]+\))$/.test(value)) offenders.push(`${file} font-family: ${value}`)
      }
    }
    expect(offenders).toEqual([])

    // 变异自检：字面量与带回落的写法都要红（回落是 :root 那条声明的第二份副本）
    expect(fontFamilies('.a { font-family: ui-monospace, Consolas, monospace; }')).toEqual(['ui-monospace, Consolas, monospace'])
    expect(fontFamilies('.a { font-family: var(--font-mono, monospace); }')).toEqual(['var(--font-mono, monospace)'])
    expect(/^(?:inherit|var\(--font-[a-z0-9-]+\))$/.test('inherit')).toBe(true)
    expect(/^(?:inherit|var\(--font-[a-z0-9-]+\))$/.test('var(--font-mono)')).toBe(true)
    expect(/^(?:inherit|var\(--font-[a-z0-9-]+\))$/.test('monospace')).toBe(false)
  })

  it('纸纹落在上色的结构画布上，不是 body', () => {
    // body 的纸纹会被 .ssh-view{background:var(--bg-base)} 整片盖住（首页任务台是
    // 透明的，它背后正是 .ssh-view）。所以令牌必须由**真正上色的那一层**
    // 消费，否则 = 永远看不见的空开关。
    for (const [name, src] of Object.entries(STRUCTURAL_CANVASES)) {
      expect(src, `${name} 仍在直接画 --bg-base，纸纹到不了它`).not.toMatch(/background:\s*var\(--bg-base\)/)
      expect(readsVar(src, '--canvas-bg'), `${name} 必须改画 --canvas-bg`).toBe(true)
    }
    // 终端内容区必须仍是 --bg-terminal：ANSI 主题固定暗色，米纸上开终端前景不可读
    expect(sshView).toMatch(/background:\s*var\(--bg-terminal\)/)
    // 内联过程反过来：它长在首页消息流里，自己不能再画一层画布底（纸从首页透上来）
    expect(agentTraceInline).not.toMatch(/background:\s*var\(--canvas-bg/)
  })

  it('稿纸横线随 textarea 内部滚动（app 封顶 4 行，预览稿不封顶）', () => {
    // 背景默认 background-attachment: scroll = 钉在元素上。文字滚、线不滚 → 线与字
    // 错开，正是组件注释里声明要避免的那个失败。预览稿 grow() 无上限所以碰不到。
    expect(homeComposer).toMatch(/background-attachment:\s*local/)
  })

  it('UA 的灰色立体按钮 chrome 在 global.css 就被断掉', () => {
    // 没写 border/background 的 <button>，Chromium 给 rgb(240,240,240) 灰底 +
    // 2px outset 立体边。它不属于任何主题：首页 .htt-btn 与新建终端菜单四枚
    // .ntm-item 就是这么在蓝屏/米纸上露出一块系统灰的。逐类补 border:0 会把
    // 这个 bug 留给下一枚按钮，所以断根。
    expect(globalCss).toMatch(/button\s*\{[^}]*border:\s*0/ )
    expect(globalCss).toMatch(/button\s*\{[^}]*background:\s*transparent/)
  })

  it('审批图标按档位换记号，chip 的底是主题墨而不是固定深色档', () => {
    // 三档共用一张盾时，切到「自动审批」界面上什么都没变 —— 图标必须跟着档位走。
    // 记号表已上收到 run-modes（输入卡与运行通知区共用一份，改一处不漏另一处），
    // 按 PermissionModeKey 穷举（漏一档 = 编译错误），消费方必须真的读这张表而非自写图标。
    expect(runModes).toMatch(/PERM_MARKS: Record<PermissionModeKey,\s*string \| null>/)
    expect(homeComposer).toMatch(/PERM_MARKS\[permMode\.value\]/)
    expect(homeComposer).toMatch(/<path v-if="permMark" :d="permMark" \/>/)
    // chip 先前用 --bg-input（一个固定深色档），在浅一档的卡面上就是块更黑的补丁。
    // 现在压的是主题自己的墨：卡面是什么，它跟着是什么。
    const chip = ruleBlock(homeComposer, '.hcc-chip')
    expect(chip).toMatch(/background:\s*color-mix\(in srgb,\s*var\(--text-primary\)/)
    expect(chip).not.toMatch(/background:\s*var\(--bg-input\)/)
    expect(chip).not.toMatch(/border:\s*1px solid/)
  })

  it('底栏菜单朝上开：Composer 在滚动区之外，朝下会被窗口底边裁掉', () => {
    // Composer 固定在任务台底部，下方就是窗口边；它不在 .home-scroll 里，
    // 朝上展开不会被任何滚动容器裁切。
    expect(ruleBlock(homeComposer, '.hcc-menu')).toMatch(/bottom:\s*calc\(100% \+ 6px\)/)
    expect(ruleBlock(homeComposer, '.hcc-menu')).not.toMatch(/top:\s*calc\(100%/)
  })

  it('首页的控件不画自己的皮：底栏三枚与两个下拉都没有边框', () => {
    // .hcc-btn 先前是 background:var(--bg-base) + 1px 边框，而 --bg-base 比它坐在
    // 上面的卡面 --bg-elevated 还暗一档 → 卡上像挖了三个洞。现在透明底无边框，
    // 皮由 global.css 的 button reset 提供，卡面（含手记卡脚那道次级纸面）自然透上来。
    expect(ruleBlock(homeComposer, '.hcc-btn')).not.toMatch(/border:\s*1px/)
    expect(ruleBlock(homeComposer, '.hcc-btn')).not.toMatch(/background:\s*var\(--bg-base\)/)
    // 下拉同理：抬起来靠 shadow，不靠边框
    expect(ruleBlock(homeComposer, '.hcc-menu')).not.toMatch(/border:\s*1px solid/)
  })

  it('逐条放行只有一个决策位：卡只在内联过程里，不再有第二条队列', () => {
    // 同一条待决请求画两遍就是两把锁，而两张卡的能力可能不一样。旧的首页审批队列
    // 已删除，逐条决策归 ApprovalCard，批量（作用于「这一批」）只是快捷结算。
    expect(agentTraceInline).toMatch(/import ApprovalCard from '\.\/ApprovalCard\.vue'/)
    expect(agentTraceInline).toMatch(/<ApprovalCard\b/)
    expect(agentTraceInline).toMatch(/settleAll/)
    // 内联过程里出现 approve/deny 的逐条调用 = 绕过 useApprovalActions 的第二条通路
    expect(agentTraceInline).not.toMatch(/\bapprove\(/)
    expect(agentTraceInline).not.toMatch(/\bdeny\(/)
  })
})

describe('完整主题按手记标准重调（森林/蓝屏/薄荷/蜜蜂/珊瑚）', () => {
  // 重调前这些主题只覆盖 16 枚结构令牌，status/card/danger/tint/accent 全落在
  // 深色默认上 —— 彩底叠默认色相正是"换层皮"的观感来源。下面的断言把
  // "一个完整主题"的覆盖集合与质感层钉住；浅色的令牌表就是基准。
  const FULL_THEMES = ['forest', 'blue-screen', 'mint', 'bee', 'coral']

  it.each(FULL_THEMES)('%s：浅色主题覆盖的令牌一个都不能少', (theme) => {
    const tokens = tokensOf(blockOf(theme))
    const missing = tokensOf(blockOf('light')).filter(t => !tokens.includes(t))
    expect(missing).toEqual([])
  })

  it.each(FULL_THEMES)('%s：--bg-* 与同名 --bg-*-solid 同值', (theme) => {
    const block = blockOf(theme)
    for (const base of ['--bg-elevated', '--bg-input']) {
      expect(valueOf(block, `${base}-solid`), `${base}-solid 要与 ${base} 同值`)
        .toBe(valueOf(block, base))
    }
  })

  it.each(FULL_THEMES)('%s：质感层自持（画布光 + 主题调投影）', (theme) => {
    // 只换颜色的主题永远换不出性格：每个完整主题都有自己的画布光与投影色。
    const block = blockOf(theme)
    expect(valueOf(block, '--canvas-bg')).toContain('radial-gradient')
    expect(valueOf(block, '--shadow-lg')).not.toBe(rootValueOf('--shadow-lg'))
  })
})
