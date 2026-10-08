import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/global.css'
// 审批卡片的共享视觉词汇：同一张卡由首页过程区的内联卡与运行面板共同渲染，
// 样式必须只有一份 —— 留在任一组件的 scoped 里，另一个消费者就得抄一遍。
import './styles/approval-card.css'
// 启动性能优化：原 `import './monaco-setup'` 静态链入 Monaco 全家桶（~10MB），
// 现 monaco-setup 已是零重量的 shim，由其消费方（useAppearance/SftpFileEditor）
// 在首开编辑器时动态加载，无需启动期登记。

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
