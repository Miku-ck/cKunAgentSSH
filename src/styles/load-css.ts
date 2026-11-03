/**
 * 动态 CSS 加载工具 — 为非首屏页面提供按需样式加载。
 *
 * 首屏 CSS 只有 global.css / button.css / variables.css / markdown.css /
 * approval-card.css 是真正阻塞渲染的，Docker/K8s 页样式（~25KB）只在用户
 * 首次进入对应页面时才需要，由各页面 onMounted 时懒加载，避免塞进主 CSS。
 *
 * 用 Vite 的动态 import() 拆出独立 CSS chunk，运行时 fetch 注入。
 * 路径必须是字面量字符串（禁用变量），否则 Vite 无法静态分析拆包。
 */

export function loadDockerModalCss(): Promise<void> {
  return import('./docker-modal.css').then(() => {}).catch((e) => {
    console.warn('[loadCss] docker-modal.css 加载失败:', e)
  })
}

export function loadDockerPageCss(): Promise<void> {
  return import('./docker-page.css').then(() => {}).catch((e) => {
    console.warn('[loadCss] docker-page.css 加载失败:', e)
  })
}

export function loadK8sPageCss(): Promise<void> {
  return import('./k8s-page.css').then(() => {}).catch((e) => {
    console.warn('[loadCss] k8s-page.css 加载失败:', e)
  })
}
