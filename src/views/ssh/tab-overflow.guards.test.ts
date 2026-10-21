/** 溢出菜单的存在性与接线守卫（交互行为靠 rendererOnly 视觉验收） */
import { describe, expect, it } from 'vitest'
import sshView from './SshView.vue?raw'
import menu from '@/components/ssh/TabOverflowMenu.vue?raw'

describe('标签溢出菜单', () => {
  it('SshView 以 ResizeObserver 判定溢出并挂载菜单', () => {
    expect(sshView).toContain('ResizeObserver')
    expect(sshView).toContain('TabOverflowMenu')
  })

  it('菜单列出全部会话并带状态点判据', () => {
    expect(menu).toContain('store.sessions')
    expect(menu).toContain('tabStatusKind')
  })

  it('菜单 Teleport 到 body（逃出 .term-tabs 的 overflow:hidden）', () => {
    expect(menu).toContain('Teleport')
  })
})
