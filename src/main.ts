import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/global.css'
// 启动性能优化：原 `import './monaco-setup'` 静态链入 Monaco 全家桶（~10MB），
// 现 monaco-setup 已是零重量的 shim，由其消费方（useAppearance/SftpFileEditor）
// 在首开编辑器时动态加载，无需启动期登记。

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
