/**
 * 外观枚举与强调色表 —— **零依赖叶子模块**，不 import store、不 import composable。
 *
 * 为什么单独一个文件：这些定义原先住在 useAppearance.ts 里，而 store 要取
 * ACCENT_MAP 的键当白名单、useAppearance 又要取 store —— 一个真运行时环。
 * 环今天不炸只是因为 App.vue 恰好先 import 了 store（`useAppearance.ts` 的
 * import 排在它后面）；任何先碰到 useAppearance 的入口都会死在
 * `settings.ts` 那句 `Object.keys(ACCENT_MAP)` 上（测试里一import 就复现）。
 * 把表挪到叶子模块，环没有立足点；appearance-tokens.test.ts 把"这里不许出现
 * store/composable"钉成断言。
 */

/**
 * 外观枚举的唯一来源：settings store 的字段类型与设置面板的选项都从这里派生。
 * 此前 Theme/Accent 在 store 与面板里各抄一份，面板色板还另抄了一份 hex ——
 * 已经抄错：蓝色 swatch 显示 #58a6ff，实际写进 --accent-primary 的是 #0078d4。
 */
export type Theme = 'dark' | 'light' | 'system' | 'blue-screen' | 'forest' | 'mint' | 'bee' | 'coral' | 'atelier' | 'glass' | 'glass-light'
export type Accent = 'blue' | 'purple' | 'green' | 'orange' | 'white' | 'forest' | 'mint' | 'bee' | 'coral' | 'verm'
export type GradientMode = 'glow' | 'linear' | 'radial'
export type GradientDirection = 'tb' | 'bt' | 'lr' | 'rl' | 'tlbr' | 'trbl'
/** 终端背景模式：auto=跟随主题底色/全局背景图（现状），solid=自定义纯色+透明度 */
export type TerminalBgMode = 'auto' | 'solid'

/** 强调色 → CSS 变量值（面板色板与 applyAccent 共用同一张表） */
export const ACCENT_MAP: Record<Accent, { hex: string; rgb: string }> = {
  blue:   { hex: '#0078d4', rgb: '0, 120, 212' },
  purple: { hex: '#c586c0', rgb: '197, 134, 192' },
  green:  { hex: '#4ec9b0', rgb: '78, 201, 176' },
  orange: { hex: '#ce9178', rgb: '206, 145, 120' },
  white:  { hex: '#ffffff', rgb: '255, 255, 255' },
  forest: { hex: '#2ea44f', rgb: '46, 164, 79' },
  mint:   { hex: '#5eead4', rgb: '94, 234, 212' },
  bee:    { hex: '#f5c542', rgb: '245, 197, 66' },
  coral:  { hex: '#ff7b72', rgb: '255, 123, 114' },
  // 朱砂（手记主题的招牌色）。它是强调色不是主题色：--accent-primary 由这里
  // 写进 :root 的内联样式，主题块里写同名令牌永远不生效（见 variables.css 注释）。
  verm:   { hex: '#c2402a', rgb: '194, 64, 42' },
}

/** 压在强调色底上的深字（浅底档用）与白字（深底档用）两个终值 */
const ON_LIGHT = '#15151a'
const ON_DARK = '#ffffff'

/**
 * 6 位 hex 颜色的唯一判定：settings store 的脏值闸门（asHexColor）与渲染层的
 * hexToRgba 共用，否则两处各写一份字面正则，放开三位缩写/8 位 alpha 时必漂 ——
 * 典型后果是 store 放行、渲染层回落 transparent 的静默分歧。
 */
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/
export function isHexColor(value: unknown): value is string {
  return typeof value === 'string' && HEX_COLOR_RE.test(value)
}

/**
 * 两色之间的 WCAG 对比度（1–21，与顺序无关）。
 *
 * 单独成函数而不是让调用方各自抄 `(L1+0.05)/(L2+0.05)`：那条表达式就是
 * contrastOnAccent 的判据本体，抄第二份迟早和 3:1 阈值分叉。
 */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/**
 * 压在 --accent-primary 底上的前景色：按 WCAG 相对亮度算，白字读到 <3:1 就换深字。
 *
 * 为什么不手写一张 accent → on 的表：那会是第三份需要跟人同步的色表（面板色板
 * 抄 hex 已经抄错过一次，见 ACCENT_MAP 上方注释），而且新增一档强调色忘记登记
 * 就又是一条"亮的空开关"。
 *
 * 阈值取 3:1（大号/半粗字形的可读线），不取 4.5:1：后者会把 #0078d4 这类本来
 * 白字就达标的档一起翻成深字 —— 那不是修 bug 是换设计。
 * 实测落点：朱砂 5.2:1、蓝 4.5:1、森林 3.2:1 保持白字；白 1.0、薄荷 1.5、
 * 蜜蜂 1.6、绿 2.0、珊瑚 2.5、橙 2.7、紫 2.8 翻成深字（这七档今天就是白字糊亮底）。
 */
export function contrastOnAccent(hex: string): string {
  return contrastRatio('#ffffff', hex) < 3 ? ON_LIGHT : ON_DARK
}

/** sRGB 相对亮度（WCAG 定义） */
export function relativeLuminance(hex: string): number {
  const int = parseInt(hex.slice(1), 16)
  const weights = [0.2126, 0.7152, 0.0722]
  return [16, 8, 0].reduce((sum, shift, i) => {
    const c = ((int >> shift) & 255) / 255
    const lin = c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
    return sum + lin * weights[i]
  }, 0)
}

/**
 * 半透明色压在不透明底色上的终值（逐通道线性插值，与浏览器 compositing 同口径）。
 *
 * 为什么需要它而不是直接拿 fg 算对比度：设置面板里"终端纯色背景 + 透明度"这一组
 * 的实际底是主题的 --bg-terminal（各套主题都是深值），40% 的白落在深底上仍是灰，
 * 按原色算就会误报"文字看不见"。
 */
export function blendHex(fgHex: string, alphaPct: number, baseHex: string): string {
  if (!isHexColor(fgHex) || !isHexColor(baseHex)) return fgHex
  const a = Math.max(0, Math.min(100, alphaPct)) / 100
  const f = parseInt(fgHex.slice(1), 16)
  const b = parseInt(baseHex.slice(1), 16)
  const out = [16, 8, 0].map(shift => {
    const v = Math.round(((f >> shift) & 255) * a + ((b >> shift) & 255) * (1 - a))
    return v.toString(16).padStart(2, '0')
  })
  return `#${out.join('')}`
}
