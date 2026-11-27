/**
 * 液态玻璃交互近似 —— 指针高光 / 弹性激发 / 明暗翻转 / 阴影增减。
 *
 * 设计约束：
 * - 不动任何组件模板：浮层靠运行时事件委托标记 liquid-glass（见 SURFACE_SELECTOR）。
 * - 只在 liquid 主题下挂监听，切走主题立即清理，不给其余主题留事件开销。
 * - 这是 CSS/JS 行为近似，不含 lensing 真折射；WebGPU / 窗口级材质不在本项目能力内。
 *
 * 事件委托而非逐元素绑定：弹窗是异步组件、按需挂载，逐元素绑定会漏掉后挂载的节点。
 */
import { onUnmounted, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import type { Theme } from '@/utils/appearance-tokens'

/** 参与液态玻璃的浮层白名单（与 global.css 的折射白名单一一对应） */
const SURFACE_SELECTOR = [
  '.settings-dialog', '.confirm-dialog', '.modal-dialog',
  '.modal', '.bulk-modal', '.dialog',
].join(', ')

/** 可做弹性反馈的小控件：浮层本体与内容区不在名单内（整窗缩放 = 视觉抖动） */
const CONTROL_SELECTOR = [
  'button', 'input', 'select', 'label', '[role="button"]',
  '.liquid-glyph', '.nav-item',
].join(', ')

export function useLiquidGlass() {
  const store = useSettingsStore()
  let bound = false
  const pressed = new Set<HTMLElement>()
  let raf = 0
  let pending: { surface: HTMLElement; x: number; y: number } | null = null

  function surfaceAt(target: EventTarget | null): HTMLElement | null {
    if (!(target instanceof Element)) return null
    const surface = target.closest<HTMLElement>(SURFACE_SELECTOR)
    // 异步弹窗后挂载时 markScrollEdges 已跑过；这里补齐标记，保证任何浮层都有动态规则。
    if (surface && !surface.classList.contains('liquid-glass')) surface.classList.add('liquid-glass')
    return surface
  }

  /** 高光跟随指针：rAF 合帧，每帧最多一次布局读取 + 一次样式写入。
      磨砂浮层上高频写 CSS 变量会让合成层反复重建，表现为持续微抖。 */
  function flushPointer() {
    raf = 0
    if (!pending) return
    const { surface, x, y } = pending
    const r = surface.getBoundingClientRect()
    surface.style.setProperty('--sheen-x', `${((x - r.left) / r.width * 100).toFixed(1)}%`)
    surface.style.setProperty('--sheen-y', `${((y - r.top) / r.height * 100).toFixed(1)}%`)
    pending = null
  }
  function onPointerMove(e: PointerEvent) {
    const surface = surfaceAt(e.target)
    if (!surface) return
    pending = { surface, x: e.clientX, y: e.clientY }
    if (!raf) raf = requestAnimationFrame(flushPointer)
  }

  /** 弹性激发：只对浮层内的小控件形变，浮层本体与内容区不参与。
      整窗 scale 会让 960px 的设置窗在每次点击时缩一下再弹回（用户反馈的"抖动"）。 */
  function onPointerDown(e: PointerEvent) {
    if (!(e.target instanceof Element)) return
    if (!surfaceAt(e.target)) return
    const control = e.target.closest<HTMLElement>(CONTROL_SELECTOR)
    if (!control) return
    control.classList.add('liquid-control', 'energized')
    pressed.add(control)
  }
  function relax() {
    for (const el of pressed) el.classList.remove('energized')
    pressed.clear()
  }

  /** 明暗翻转 + 阴影增减：只在真正跨入浮层时采样一次。
      pointerover 在浮层内每次跨子元素都触发，反复 toggle body class 会让
      box-shadow 过渡反复重启动，叠在磨砂层上就是持续闪烁。 */
  function onPointerOver(e: PointerEvent) {
    const surface = surfaceAt(e.target)
    if (!surface) return
    if (e.relatedTarget instanceof Node && surface.contains(e.relatedTarget)) return
    const r = surface.getBoundingClientRect()
    const behind = document.elementFromPoint(r.left + r.width / 2, r.top - 4)
    const bright = behind instanceof HTMLElement && isBright(behind)
    document.body.classList.toggle('liquid-surface-dark', !bright)
    document.body.classList.toggle('liquid-surface-light', bright)
  }

  /** 读计算样式亮度：取 --bg-base 的相对亮度，不用写死主题名 if */
  function isBright(el: HTMLElement): boolean {
    const raw = getComputedStyle(el).getPropertyValue('--bg-base').trim()
    const m = raw.match(/^#([0-9a-f]{6})$/i)
    if (!m) return false
    const n = parseInt(m[1], 16)
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(v => {
      const c = v / 255
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2] > 0.3
  }

  /** Scroll Edge 溶解：给有滚动overflow的浮层后代标记遮罩类 */
  function markScrollEdges() {
    for (const surface of document.querySelectorAll<HTMLElement>(SURFACE_SELECTOR)) {
      surface.classList.add('liquid-glass')
      for (const child of surface.querySelectorAll<HTMLElement>('*')) {
        if (child.scrollHeight > child.clientHeight + 8 && child.clientHeight > 0) {
          child.classList.add('liquid-scroll-edge')
        }
      }
    }
  }

  function bind() {
    if (bound) return
    bound = true
    document.addEventListener('pointermove', onPointerMove, { passive: true })
    document.addEventListener('pointerdown', onPointerDown, { passive: true })
    document.addEventListener('pointerup', relax, { passive: true })
    document.addEventListener('pointercancel', relax, { passive: true })
    document.addEventListener('pointerover', onPointerOver, { passive: true })
    markScrollEdges()
  }

  function unbind() {
    if (!bound) return
    bound = false
    document.removeEventListener('pointermove', onPointerMove)
    document.removeEventListener('pointerdown', onPointerDown)
    document.removeEventListener('pointerup', relax)
    document.removeEventListener('pointercancel', relax)
    document.removeEventListener('pointerover', onPointerOver)
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    pending = null
    relax()
    for (const surface of document.querySelectorAll<HTMLElement>(SURFACE_SELECTOR)) {
      surface.classList.remove('liquid-glass', 'energized')
      for (const child of surface.querySelectorAll('.liquid-scroll-edge')) {
        child.classList.remove('liquid-scroll-edge')
      }
    }
    document.body.classList.remove('liquid-surface-dark', 'liquid-surface-light')
  }

  watch(
    () => store.settings.appearance.theme,
    (theme: Theme) => {
      if (theme === 'liquid') bind()
      else unbind()
    },
    { immediate: true }
  )

  onUnmounted(unbind)
}
