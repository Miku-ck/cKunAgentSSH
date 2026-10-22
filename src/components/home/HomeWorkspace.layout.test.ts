/**
 * 历史记录弹窗的版面守卫（M-1 修订：右侧抽屉 → 居中弹窗）
 *
 * 弹窗 Teleport 到 body + fixed 遮罩，不再参与任务台布局。抽屉时代那套
 * 「主区/输入区为右栏让出宽度」的规则留着就是一条无效 padding —— 这里
 * 同时钉住它已退场，以及弹窗仍是一层真正的模态（role=dialog / aria-modal）。
 */
import { describe, expect, it } from 'vitest'
import rawWorkspace from './HomeWorkspace.vue?raw'
import rawDialog from './HomeHistoryDialog.vue?raw'

const workspace = rawWorkspace.replace(/\r\n/g, '\n')
const css = workspace.slice(workspace.indexOf('<style'))
const dialog = rawDialog.replace(/\r\n/g, '\n')

describe('历史记录弹窗的版面与模态契约', () => {
  it('任务台不再为历史面板预留右栏（抽屉让位规则已退场）', () => {
    expect(workspace).not.toContain('is-history-open')
    expect(css).not.toMatch(/\.home-main\s*\{[^}]*padding-right/)
    expect(css).not.toMatch(/\.composer-wrap\s*\{[^}]*padding-right/)
  })

  it('弹窗走 Teleport + fixed 全屏遮罩，不压在某一栏里', () => {
    expect(dialog).toContain('<Teleport to="body">')
    expect(dialog).toMatch(/\.hd-overlay\s*\{[^}]*position:\s*fixed/)
    expect(dialog).toMatch(/\.hd-overlay\s*\{[^}]*inset:\s*0/)
  })

  it('弹窗是模态：role=dialog + aria-modal=true', () => {
    expect(dialog).toContain('role="dialog"')
    expect(dialog).toContain('aria-modal="true"')
  })
})

describe('历史条目删除契约', () => {
  // 点垃圾桶就是要删，再弹一层确认等于把用户刚做的决定重问一次。
  it('删除入口直接执行，不再二次确认', () => {
    const start = workspace.indexOf('function deleteConversation')
    expect(start, '没找到 deleteConversation').toBeGreaterThan(-1)
    const body = workspace.slice(start, workspace.indexOf('\n}', start))
    expect(body).not.toMatch(/\bconfirm\(/)
    expect(body).toContain('sshAi.deleteConversation(id)')
  })
})
