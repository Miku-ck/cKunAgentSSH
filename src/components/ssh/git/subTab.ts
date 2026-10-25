/**
 * Git 页的子页选择（加速 | 仓库）。
 *
 * 模块级单例：GitView 用它渲染与记忆「上次停留的子页」（会话内不丢），
 * 命令面板的深链条目（打开：Git 加速 / Git 仓库管理）在导航前先写它 ——
 * GitView 是异步 chunk，页面还没挂载时深链目标只能存在这里。
 */
import { ref } from 'vue'

export type GitSubTab = 'accel' | 'repos'

/** 默认仓库：仓库管理是 Git 页的工作台主入口（2026-10-25 起；此前默认加速） */
export const gitSubTab = ref<GitSubTab>('repos')

export function setGitSubTab(sub: GitSubTab): void {
  gitSubTab.value = sub
}
