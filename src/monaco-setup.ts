/**
 * Monaco 轻量 shim — 启动路径可安全静态 import（不拖入编辑器本体）。
 *
 * 重负载运行时（编辑器 + worker + 语言 + 主题定义，原 ~10MB 主包大头）拆到
 * ./monaco-runtime，只在首次打开文件编辑器（SftpFileEditor.loadMonaco）时动态加载。
 *
 * H13 语义保持：Monaco 主题跟随设置切换。区别仅在时机 — Monaco 尚未加载时
 * 没有任何编辑器实例，setTheme 无处可作用，只记录 desiredTheme；加载完成的
 * 那一刻统一应用，主题不会丢。
 */

// 默认深色：与拆分前"启动即 setTheme('ckun-dark')"的初始效果一致
let loadedInstance: { editor: { setTheme(name: string): void } } | null = null
let desiredTheme: AppTheme = 'dark'
let loading: Promise<unknown> | null = null

/** 动态加载 Monaco 运行时并返回 monaco 实例 — 供 SftpFileEditor 调用 */
export function loadMonaco(): Promise<any> {
  if (!loading) {
    loading = import('./monaco-runtime').then((mod) => {
      const inst = mod.default as unknown as { editor: { setTheme(name: string): void } }
      loadedInstance = inst
      // 加载前累积的主题切换在此统一落上（含加载期间刚发生的切换）
      inst.editor.setTheme(resolveMonacoThemeName(desiredTheme))
      return inst
    })
    // 失败不缓存：chunk 加载失败（dev 重启/文件替换）只该废掉这一次打开，
    // 留 rejected promise 会让后续所有打开永久失败且无从恢复
    loading.catch(() => { loading = null })
  }
  return loading
}

/**
 * 主题切换入口 — useAppearance 调用。
 * Monaco 未加载时仅记录目标主题（无编辑器可作用，加载后自动应用）。
 */
export function applyMonacoTheme(theme: AppTheme): void {
  desiredTheme = theme
  if (!loadedInstance) return
  loadedInstance.editor.setTheme(resolveMonacoThemeName(theme))
}

// ──────────────────────────────────────────────────────────────────────────────
// 主题名解析（原驻 monaco-setup 的映射表，随懒加载拆分上移到轻量层）
// ──────────────────────────────────────────────────────────────────────────────

export type AppTheme = 'dark' | 'light' | 'system' | 'blue-screen' | 'forest' | 'mint' | 'bee' | 'coral'

type MonacoThemeName = 'ckun-dark' | 'ckun-light' | 'ckun-blue-screen' | 'ckun-forest' | 'ckun-mint' | 'ckun-bee' | 'ckun-coral'

const THEME_MAP: Record<string, MonacoThemeName> = {
  dark: 'ckun-dark',
  light: 'ckun-light',
  'blue-screen': 'ckun-blue-screen',
  forest: 'ckun-forest',
  mint: 'ckun-mint',
  bee: 'ckun-bee',
  coral: 'ckun-coral',
}

function resolveMonacoThemeName(theme: AppTheme): MonacoThemeName {
  if (theme === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'ckun-dark' : 'ckun-light'
  }
  return THEME_MAP[theme] || 'ckun-dark'
}
