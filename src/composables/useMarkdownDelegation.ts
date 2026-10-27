/**
 * useMarkdownDelegation —— Markdown 渲染容器的事件委托（全局共用）
 *
 * `renderMarkdown()` 产出的 HTML 里有两类交互节点：
 *   1. `<a href>`：外链/本地链接。统一 preventDefault 后交系统浏览器或拦截，
 *      避免远程主机书写的 `file:///…` 直接把主窗口导航走（窗口带着 preload）。
 *   2. `.code-copy-btn`：代码块复制按钮。
 * 聊天（MarkdownStream）与执行历史（HistoryList）共用同一份实现，避免两边行为
 * 漂移（历史区曾漏掉此委托，复制按钮点了没反应）。
 */
import { useToastStore } from '@/stores/toast'

export function useMarkdownDelegation() {
  const toast = useToastStore()

  function onMarkdownClick(e: MouseEvent): void {
    const target = e.target as HTMLElement

    // 链接：http(s) 交系统浏览器，其余协议拦截并提示
    const anchor = target.closest?.('a') as HTMLAnchorElement | null
    if (anchor?.href) {
      e.preventDefault()
      let protocol = ''
      try { protocol = new URL(anchor.href).protocol } catch { protocol = '' }
      if (protocol === 'http:' || protocol === 'https:') {
        window.open(anchor.href, '_blank', 'noopener,noreferrer')
      } else {
        toast.warning('已在应用内拦截非 http(s) 链接')
      }
      return
    }

    // 代码块复制按钮
    if (!target.classList.contains('code-copy-btn')) return
    const wrapper = target.closest('.code-block-wrapper')
    const codeEl = wrapper?.querySelector('code')
    if (!codeEl) return
    const text = codeEl.textContent || ''
    navigator.clipboard.writeText(text).then(() => {
      target.textContent = '已复制'
      setTimeout(() => { target.textContent = '复制' }, 1500)
    }).catch(() => {
      toast.error('复制失败，请手动选择文本复制')
    })
  }

  return { onMarkdownClick }
}
