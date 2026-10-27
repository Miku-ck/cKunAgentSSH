/// <reference types="node" />
/**
 * 弹窗焦点管理守卫 —— 一份实现，所有弹窗共用
 *
 * Tab 环形与「关闭后把焦点还给触发器」曾经只长在 BaseModal 里，自绘 overlay 的
 * 那批弹窗各写各的（有的只有 Esc，有的什么都没有）。这里钉住两件事：
 *  1. 共享件确实存在且能力齐（window 级 Tab 监听、isConnected 兜底的焦点还原）；
 *  2. 接入的弹窗真的调了它，而不是又抄一份 FOCUSABLE 选择器。
 *
 * 行为本身（焦点怎么跳）没法在 node 环境下测 —— 仓库没有 jsdom；能钉的是契约。
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { SRC } from '../styles/fs-walk'

const read = (rel: string) => readFileSync(join(SRC, rel), 'utf8')

describe('弹窗焦点管理', () => {
  const trap = read('composables/useModalFocusTrap.ts')

  it('共享件负责 Tab 环形，监听挂在 window 上', () => {
    expect(trap).toContain('export function useModalFocusTrap')
    expect(trap).toContain("e.key !== 'Tab'")
    // 挂在容器上收不到「焦点已跑到弹窗外」的场景，必须挂 window
    expect(trap).toContain("window.addEventListener('keydown', onKeydown, true)")
    expect(trap).toContain('return { activate, deactivate')
  })

  it('关闭时把焦点还给触发器，且容忍触发器已卸载', () => {
    expect(trap).toContain('prevFocus')
    expect(trap).toContain('isConnected')
    expect(trap).toContain('prevFocus.focus()')
  })

  it('BaseModal 不再自带一份焦点陷阱实现', () => {
    const modal = read('components/common/BaseModal.vue')
    expect(modal).toContain("from '../../composables/useModalFocusTrap'")
    expect(modal).toContain('activate()')
    expect(modal).toContain('deactivate()')
    // 选择器字符串留在共享件里；组件里再抄一份就是两套行为
    expect(modal).not.toContain('const FOCUSABLE')
  })

  it('ConfirmDialog 接上共享件，初始焦点落在主按钮', () => {
    const dialog = read('components/common/ConfirmDialog.vue')
    expect(dialog).toContain("from '../../composables/useModalFocusTrap'")
    expect(dialog).toMatch(/initialFocus:\s*'\.confirm-btn\.ok'/)
    // 容器要能接住初始焦点
    expect(dialog).toContain('tabindex="-1"')
    expect(dialog).toContain('activate()')
    expect(dialog).toContain('deactivate()')
  })

  it('自绘 overlay 的弹窗也接上（Tunnel / Access）', () => {
    const tunnel = read('components/ssh/TunnelModal.vue')
    expect(tunnel).toContain("from '../../composables/useModalFocusTrap'")
    expect(tunnel).toContain('activate()')
    expect(tunnel).toContain('deactivate()')
    // 顺手补上此前完全没有的对话框语义（无 role/无 Esc）
    expect(tunnel).toContain('role="dialog"')
    expect(tunnel).toContain('aria-labelledby="tunnel-modal-title"')
    expect(tunnel).toContain("e.key === 'Escape'")

    const access = read('components/ssh/AccessModal.vue')
    expect(access).toContain("from '../../composables/useModalFocusTrap'")
    // 嵌入宿主时不抢焦点也不拦 Esc（宿主弹窗自己管）
    expect(access).toMatch(/if \(props\.embedded\) return\s*\n\s*activate\(\)/)
    expect(access).toContain('tabindex="-1"')
  })

  it('SFTP 三个自绘弹窗也接上（预览 / 冲突 / 传输）', () => {
    const files = [
      'components/ssh/SftpImagePreview.vue',
      'components/ssh/SftpConflictDialog.vue',
      'components/ssh/SftpTransferDialog.vue',
    ]
    for (const file of files) {
      const src = read(file)
      expect(src, `${file} 未接共享件`).toContain("from '../../composables/useModalFocusTrap'")
      expect(src, `${file} 打开时未激活`).toContain('activate()')
      expect(src, `${file} 关闭时未还原`).toContain('deactivate()')
      // 容器要能接住初始焦点，否则 activate() 无处可去
      expect(src, `${file} 容器缺 tabindex="-1"`).toContain('tabindex="-1"')
    }
  })

  it('DESIGN.md 记着「焦点管理走共享件」', () => {
    expect(readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')).toContain('useModalFocusTrap')
  })
})
