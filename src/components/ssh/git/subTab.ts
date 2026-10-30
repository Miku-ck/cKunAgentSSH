/**
 * Git 页的子页选择（加速 | 源码管理）。
 *
 * 模块级单例：GitView 用它渲染与记忆「上次停留的子页」（会话内不丢），
 * 命令面板的深链条目（打开：Git 加速 / Git 源码管理）在导航前先写它 ——
 * GitView 是异步 chunk，页面还没挂载时深链目标只能存在这里。
 *
 * 2026-10-30 起「同步」不再是独立子页：远端一致性并入仓库工作台
 * （列表行三点状态 + 详情远端条/远端页签），故联合类型只剩两项。
 */
import { ref } from 'vue'

export type GitSubTab = 'accel' | 'repos'

/** 默认源码管理：它是 Git 页的工作台主入口（2026-10-25 起；此前默认加速） */
export const gitSubTab = ref<GitSubTab>('repos')

export function setGitSubTab(sub: GitSubTab): void {
  gitSubTab.value = sub
}
