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
import rawTitleBar from '../layouts/TitleBar.vue?raw'
import rawRunView from '../components/agent/RunView.vue?raw'
import rawHomeCard from '../components/home/HomeCommandCard.vue?raw'
import rawHomeTitle from '../components/home/HomeTargetTitle.vue?raw'
import rawHomeQueue from '../components/home/HomeApprovalQueue.vue?raw'

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
const titleBar = strip(rawTitleBar)
const runView = strip(rawRunView)
const homeCard = strip(rawHomeCard)
const homeTitle = strip(rawHomeTitle)
const homeQueue = strip(rawHomeQueue)

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
    RunView 不在列 —— 它 2026-09-30 起是首页里的一块，纸从首页透上来，自己不画底。 */
const STRUCTURAL_CANVASES: Record<string, string> = {
  '.ssh-view / .term-tabs': sshView,
  '.titlebar': titleBar,
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

  it('纸纹落在上色的结构画布上，不是 body', () => {
    // body 的纸纹会被 .ssh-view{background:var(--bg-base)} 整片盖住（首页画布是
    // 透明的 .home-pane，它背后正是 .ssh-view）。所以令牌必须由**真正上色的那一层**
    // 消费，否则 = 永远看不见的空开关。
    for (const [name, src] of Object.entries(STRUCTURAL_CANVASES)) {
      expect(src, `${name} 仍在直接画 --bg-base，纸纹到不了它`).not.toMatch(/background:\s*var\(--bg-base\)/)
      expect(readsVar(src, '--canvas-bg'), `${name} 必须改画 --canvas-bg`).toBe(true)
    }
    // 终端内容区必须仍是 --bg-terminal：ANSI 主题固定暗色，米纸上开终端前景不可读
    expect(sshView).toMatch(/background:\s*var\(--bg-terminal\)/)
    // RunView 反过来：它是首页里的一块，自己不再画一层画布底（纸从首页透上来）
    expect(runView).not.toMatch(/background:\s*var\(--canvas-bg/)
  })

  it('稿纸横线随 textarea 内部滚动（app 封顶 4 行，预览稿不封顶）', () => {
    // 背景默认 background-attachment: scroll = 钉在元素上。文字滚、线不滚 → 线与字
    // 错开，正是组件注释里声明要避免的那个失败。预览稿 grow() 无上限所以碰不到。
    expect(homeCard).toMatch(/background-attachment:\s*local/)
  })

  it('UA 的灰色立体按钮 chrome 在 global.css 就被断掉', () => {
    // 没写 border/background 的 <button>，Chromium 给 rgb(240,240,240) 灰底 +
    // 2px outset 立体边。它不属于任何主题：首页 .htt-btn 与新建终端菜单四枚
    // .ntm-item 就是这么在蓝屏/米纸上露出一块系统灰的。逐类补 border:0 会把
    // 这个 bug 留给下一枚按钮，所以断根。
    expect(globalCss).toMatch(/button\s*\{[^}]*border:\s*0/ )
    expect(globalCss).toMatch(/button\s*\{[^}]*background:\s*transparent/)
  })

  it('标题那枚目标词与整句同字族同尺寸同间距，只留虚线作区别', () => {
    // 它先前是 --accent-primary 上色 + 同色虚线，后又独立成等宽 17px/500，左右还各
    // 塞了 5px margin 与 6px padding —— 一句问话里贴了一张标签。现在字族/字号/字重/
    // 字色全继承，额外空隙清零，区别只剩下面那道虚线（参考稿也不留空隙）。
    const btn = ruleBlock(homeTitle, '.htt-btn')
    expect(btn).toMatch(/font-family:\s*inherit/)
    expect(btn).toMatch(/font-size:\s*inherit/)
    expect(btn).toMatch(/color:\s*inherit/)
    expect(btn).not.toMatch(/padding:/)
    expect(btn).toMatch(/border-bottom:\s*1px dashed var\(--text-tertiary\)/)
    expect(ruleBlock(homeTitle, '.htt-pick')).not.toMatch(/margin:/)
    // 整句压到 --text-secondary：这行是问句，不是一块招牌
    expect(ruleBlock(homeTitle, '.htt')).toMatch(/color:\s*var\(--text-secondary\)/)
    // 当初给它画的是记号笔色块（--wash-mark）。改成虚线后那个令牌一个读取方都不剩，
    // 必须整条摘掉：只写不读的令牌 = 失效设置项（variables.css 头部维护约定）。
    expect(css).not.toContain('--wash-mark')
  })

  it('审批图标按档位换记号，chip 的底是主题墨而不是固定深色档', () => {
    // 三档共用一张盾时，切到「自动审批」界面上什么都没变 —— 图标必须跟着档位走。
    // 记号表按 PermissionModeKey 穷举（漏一档 = 编译错误），不另写第二份档位清单。
    expect(homeCard).toMatch(/Record<PermissionModeKey,\s*string \| null>/)
    expect(homeCard).toMatch(/<path v-if="permMark" :d="permMark" \/>/)
    // chip 先前用 --bg-input（一个固定深色档），在浅一档的卡面上就是块更黑的补丁。
    // 现在压的是主题自己的墨：卡面是什么，它跟着是什么。
    const chip = ruleBlock(homeCard, '.hcc-chip')
    expect(chip).toMatch(/background:\s*color-mix\(in srgb,\s*var\(--text-primary\)/)
    expect(chip).not.toMatch(/background:\s*var\(--bg-input\)/)
    expect(chip).not.toMatch(/border:\s*1px solid/)
  })

  it('底栏菜单朝下开：向上溢出在滚动画布里是拿不回来的', () => {
    // 实测（.home-pane{overflow-y:auto}）：菜单顶到 y=21、画布顶到 y=66 → 上面
    // 45px 被裁，且负方向没有滚动区，滚不动也点不到。朝下溢出至少还在滚动范围内。
    // 同屏的 .htt-menu 一直就是朝下开的，这条把两者钉在同一方向。
    expect(ruleBlock(homeCard, '.hcc-menu')).toMatch(/top:\s*calc\(100% \+ 6px\)/)
    expect(ruleBlock(homeCard, '.hcc-menu')).not.toMatch(/bottom:\s*calc\(100%/)
    expect(ruleBlock(homeTitle, '.htt-menu')).toMatch(/top:\s*calc\(100% \+ 6px\)/)
  })

  it('首页的控件不画自己的皮：底栏三枚与两个下拉都没有边框', () => {
    // .hcc-btn 先前是 background:var(--bg-base) + 1px 边框，而 --bg-base 比它坐在
    // 上面的卡面 --bg-elevated 还暗一档 → 卡上像挖了三个洞。现在透明底无边框，
    // 皮由 global.css 的 button reset 提供，卡面（含手记卡脚那道次级纸面）自然透上来。
    expect(ruleBlock(homeCard, '.hcc-btn')).not.toMatch(/border:\s*1px/)
    expect(ruleBlock(homeCard, '.hcc-btn')).not.toMatch(/background:\s*var\(--bg-base\)/)
    // 下拉同理：抬起来靠 shadow，不靠边框
    expect(ruleBlock(homeCard, '.hcc-menu')).not.toMatch(/border:\s*1px solid/)
    expect(ruleBlock(homeTitle, '.htt-menu')).not.toMatch(/border:\s*1px solid/)
  })

  it('逐条放行只有一个决策位：卡在内联，首页窄条不得再批', () => {
    // C1 把过程区搬进首页之后，队列与内联卡同屏。同一条待决请求画两遍就是两把锁：
    // 两处都能点批准，而两张卡的能力不一样（内联四档含 session，队列三档）。
    // 裁决见 spec 2026-09-29-home-command-panel C2 —— 决策归卡，队列只报数、
    // 批量（作用于「这一批」，与逐条不是同一件事）与追溯。
    expect(runView).toMatch(/import ApprovalCard from '\.\/ApprovalCard\.vue'/)
    expect(runView).toMatch(/<ApprovalCard\b/)
    // 队列里出现 approve/deny 的逐条调用 = 第二个决策位回来了
    expect(homeQueue).not.toMatch(/\bapprove\(/)
    expect(homeQueue).not.toMatch(/\bdeny\(/)
    expect(homeQueue).toMatch(/settleAll/)
    expect(homeQueue).not.toMatch(/haq-card/)
  })
})
