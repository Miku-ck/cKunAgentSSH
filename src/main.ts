import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { useToastStore } from './stores/toast'
import './styles/global.css'
// 审批卡片的共享视觉词汇：同一张卡由首页过程区的内联卡与运行面板共同渲染，
// 样式必须只有一份 —— 留在任一组件的 scoped 里，另一个消费者就得抄一遍。
import './styles/approval-card.css'
// Docker 详情弹窗的共享视觉词汇：卷/网络详情在 DockerView，容器详情已拆成
// DockerContainerDetail —— 两份弹窗骨架只留一处定义。
import './styles/docker-modal.css'
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
