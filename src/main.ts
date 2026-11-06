import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { useToastStore } from './stores/toast'
import './styles/global.css'
// 24 栅格规范令牌层与布局系统（spec-tokens → grid）。
// 与现有 variables.css 并存：规范里「画布无关」的档（危险实色 + 前景）经此层
// 供 button.css 消费；控件高度/间距/字体都接回 variables.css 的既有令牌
// （--control-h / --spacing-* / --font-sans），中性色板随主题走 --text-*/--border-*
// （规范的 #333333/#E4E7ED 是浅色画布档，直接套深色主题会给主文字装 1.37:1 的皮）。
import './styles/spec-tokens.css'
import './styles/grid.css'
// Markdown 渲染容器的共享视觉词汇：聊天（MarkdownStream）与执行历史（HistoryList）
// 都经 renderMarkdown 产出 HTML，代码块/表格/标题样式只能有一份。
import './styles/markdown.css'
// 审批卡片的共享视觉词汇：同一张卡由首页过程区的内联卡与运行面板共同渲染，
// 样式必须只有一份 —— 留在任一组件的 scoped 里，另一个消费者就得抄一遍。
import './styles/approval-card.css'
// Docker / K8s 页样式已懒加载化：移除静态 import，改由 DockerView / K8sView
// 各自 onMounted 时经 styles/load-css 动态拉入。首屏 CSS 因此减少 ~25KB，
// Docker / K8s 不在首屏的访问链路中也不再承担这份额外体积。

// 启动性能优化：原 `import './monaco-setup'` 静态链入 Monaco 全家桶（~10MB），
// 现 monaco-setup 已是零重量的 shim，由其消费方（useAppearance/SftpFileEditor）
// 在首开编辑器时动态加载，无需启动期登记。

const app = createApp(App)
app.use(createPinia())
// ARCH-001：渲染进程唯一的全局错误边界。此前无任何兜底 —— 任一组件的渲染/
// 生命周期钩子抛出未捕获异常，Vue 中止该组件树更新，长时驻留的终端界面直接
// 白屏且无提示、无恢复入口。此处上报 toast（pinia 装配后 store 可用）并保留
// 组件链到 console；不返回 false，错误照常按 Vue 默认策略传播。
app.config.errorHandler = (err, instance, info) => {
  console.error(`[vue] 未处理错误（${info}）:`, err, instance ?? null)
  try {
    useToastStore().error(`界面异常：${err instanceof Error ? err.message : String(err)}`)
  } catch { /* store 尚不可用（极早期失败）时保底 console */ }
}
app.mount('#app')
