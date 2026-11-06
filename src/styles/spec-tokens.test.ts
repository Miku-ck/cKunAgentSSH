/**
 * spec-tokens.css 的静态验收
 *
 * 与 variables.test.ts 同一条纪律：新令牌必须有真实消费方。
 * 本文件扫 spec-tokens.css / grid.css / button.css 三份样式层，
 * 断言「每条令牌都被 var() 读取」。
 *
 * 新令牌与消费方同批落地，所以这里用"裸引用"约束（同 variables.test.ts）：
 * var(--x, 回落) 里的回落是 :root 那条声明的第二份副本，改 :root 时它会静默留在后面。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import rawSpecTokens from './spec-tokens.css?raw'
import rawGrid from './grid.css?raw'
import rawButton from './button.css?raw'
import { walk } from './fs-walk'

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '')

const specTokens = strip(rawSpecTokens)
const grid = strip(rawGrid)
const buttonCss = strip(rawButton)

/** :root 块里的令牌清单 */
const tokensOf = (block: string) =>
  [...block.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(m => m[1]).sort()

const rootBlock = specTokens.match(/:root\s*\{[\s\S]*?\}/)
if (!rootBlock) throw new Error('spec-tokens.css 里找不到 :root 块')
const definedTokens = tokensOf(rootBlock[0])

/** 规范层 + 消费方合计的源码 */
const ALL_SOURCES = specTokens + '\n' + grid + '\n' + buttonCss

/** 全仓 .vue 源码合计：反死代码守卫要跨文件找类名的真实消费方 */
const VUE_SOURCES = walk()
  .filter(f => f.endsWith('.vue'))
  .map(f => readFileSync(f, 'utf8'))
  .join('\n')

/** 令牌是否被 var() 读取 */
const readsVar = (src: string, token: string) =>
  new RegExp(`var\\(${token}[,)]`).test(src)

describe('spec-tokens.css 规范令牌层', () => {
  it('三份样式层都真读到了东西（css.include 白名单守卫）', () => {
    // 空串守卫：vitest 的 css.include 是白名单，漏一项就把空串当成"没有违规"
    expect(specTokens.length).toBeGreaterThan(80)
    expect(grid.length).toBeGreaterThan(80)
    expect(buttonCss.length).toBeGreaterThan(80)
  })

  it('每条令牌都有真实消费方（文件头维护约定）', () => {
    const orphan = definedTokens.filter(t => !readsVar(ALL_SOURCES, t))
    expect(orphan).toEqual([])
  })

  it('规范令牌一律裸引用：var(--x, 回落) 是 :root 声明的第二份副本', () => {
    for (const token of definedTokens) {
      expect(ALL_SOURCES, `${token} 有消费方写了回落值`)
        .not.toMatch(new RegExp(`var\\(${token},`))
    }
  })

  it('规范画布无关档与 24 栅格规则逐字对应', () => {
    expect(specTokens).toMatch(/--color-danger:\s*#c71a1a/)
    // 危险档前景是规范 7.4 原文（压在 #C71A1A 上的白字）
    expect(specTokens).toMatch(/--color-on-danger:\s*#ffffff/)
  })

  it('规范层不另立主色：主色只有一条 --accent-primary 事实源', () => {
    // 规范里的「系统主色」就是项目的 --accent-primary（ACCENT_MAP 十档强调色）。
    // 曾有一枚 --color-primary: #0078d4 与它同值并存，只在 button.css 的
    // :focus-visible 用了一次 —— 切强调色时按钮跟着变、焦点描边扒着 #0078d4 不放，
    // 同一屏两套「主色」。删掉它，焦点描边改读 --accent-primary。
    expect(specTokens).not.toMatch(/--color-primary:/)
    expect(buttonCss).toMatch(/outline:\s*2px solid var\(--accent-primary\)/)
    expect(buttonCss).not.toMatch(/var\(--color-primary\)/)
  })

  it('中性色不写死在规范层，派生自 variables.css 的主题令牌', () => {
    // 规范给的 #333333 / #909399 / #E4E7ED 是为浅色画布定义的，
    // 抄成 :root 固定字面量会给深色主题装浅色皮（主文字 1.37:1）。
    // 规范层只放画布无关档，中性色随主题走。
    for (const hex of ['#333333', '#909399', '#c0c4cc', '#e4e7ed', '#c6c6c6']) {
      expect(specTokens, `规范层里出现了浅色档字面量 ${hex}`).not.toContain(hex)
    }
    expect(specTokens).not.toMatch(/--color-text-primary:/)
    expect(specTokens).not.toMatch(/--color-border-light:/)
  })

  it('栅格间距是 --spacing-* 刻度的别名（不新写同值常量）', () => {
    // 规范原文 Gutter 12 / Padding 16 / Spacing 12，与 variables.css 的
    // --spacing-12 / --spacing-16 同值。别名接回而不是各写一份 px：
    // 两套名字并存时改了一套另一套不知道，消费点会静默分裂成两半。
    expect(specTokens).toMatch(/--gutter:\s*var\(--spacing-12\)/)
    expect(specTokens).toMatch(/--padding:\s*var\(--spacing-16\)/)
    expect(specTokens).toMatch(/--spacing:\s*var\(--spacing-12\)/)
  })

  it('规范字体档是 --font-sans 的别名（不另抄一份字表）', () => {
    // 规范 §4 要求 MAC 用 San Francisco / PingFangSC、Windows 用 Microsoft YaHei。
    // 项目 --font-sans 已满足（system-ui 在 MAC 解析成 San Francisco，回退含
    // PingFang SC / Microsoft YaHei），且手记主题会整族换衬线。
    // 规范层再抄一份固定栈会覆盖手记的衬线（64 处 --font-spec 消费点全部失效），
    // 且把 PingFang SC 排在 San Francisco 前面，与规范 §4 自己的字体序相悖。
    expect(specTokens).toMatch(/--font-spec:\s*var\(--font-sans\)/)
    expect(specTokens).not.toMatch(/--font-spec:\s*"/)
  })

  it('grid.css 响应式断点与规范一致（不再有断点令牌需要同步）', () => {
    // 曾有一批 --breakpoint-* / --grid-cols 令牌，注释写着「仅文档参考 / JS 消费方
    // 读取」—— 实际零消费方：CSS @media 不支持 var() 引用，也没有 JS 读它们。
    // 断点值只活在 grid.css 的 @media 里，这里直接钉住那一份。
    expect(grid).toMatch(/@media \(min-width:\s*768px\)/)
    expect(grid).toMatch(/@media \(min-width:\s*992px\)/)
    expect(grid).toMatch(/@media \(min-width:\s*1440px\)/)
    expect(grid).toMatch(/@media \(min-width:\s*1920px\)/)
  })

  it('grid.css 的每个列档都有真实消费方（反死代码守卫）', () => {
    // 旧版断言要求「24 列全存在」，那是在保护一批无消费方的死类 ——
    // grid.css 曾预置全部 .col-1..24 与各断点全套，实际只有 RemoteManager 用 5 档。
    // 现在反过来：定义了就必须被 .vue 模板消费，否则就是「以为落地了」的假象。
    const defined = [...grid.matchAll(/\.(col-(?:sm-|md-|lg-|xl-)?\d+)\s*\{/g)].map(
      m => m[1],
    )
    expect(defined.length).toBeGreaterThan(0)
    for (const cls of defined) {
      expect(VUE_SOURCES, `grid.css 定义了 .${cls} 但没有任何 .vue 使用`).toContain(
        cls,
      )
    }
  })

  it('grid.css 的栅格列锁定宽度（flex-shrink: 0，防列被压缩）', () => {
    // 只写 width 不锁 flex-shrink 时，容器窄于「各列宽之和」会静默压缩列宽，
    // 栅格比例失效（col-lg-6 想占 25%，实际缩到内容宽）。
    expect(grid).toMatch(/\.grid-col\s*\{[^}]*flex:\s*0\s+0\s+auto/)
  })

  it('button.css 底座读规范控件高度 / 间距 / 圆角 / 字体', () => {
    expect(buttonCss).toMatch(/height:\s*var\(--control-h\)/)
    expect(buttonCss).toMatch(/height:\s*var\(--control-h-sm\)/)
    expect(buttonCss).toMatch(/gap:\s*var\(--spacing\)/)
    expect(buttonCss).toMatch(/font-family:\s*var\(--font-spec\)/)
  })

  it('规范 7.4 的危险实色/危险前景在 button.css 有消费方', () => {
    expect(buttonCss).toMatch(/background:\s*var\(--color-danger\)/)
    expect(buttonCss).toMatch(/color:\s*var\(--color-on-danger\)/)
  })

  it('控件高度 xl 断点档写在同一处（规范 7.1/7.2）', () => {
    expect(specTokens).toMatch(
      /@media \(min-width: 1920px\)\s*\{\s*:root\s*\{\s*--control-h:\s*var\(--control-h-xl\)/,
    )
    // 消费方只读 --control-h，不得各自再写高度断点
    expect(buttonCss).not.toMatch(/@media[^{]*1920[^{]*\{[^}]*height/)
  })
})
