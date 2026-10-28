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
import { SRC, rel, walk } from '../styles/fs-walk'

const read = (rel: string) => readFileSync(join(SRC, rel), 'utf8')

/** 已接共享件、且容器自己接住初始焦点的弹窗。下面的全仓交叉核对也靠这份清单。 */
const INTEGRATED = [
  'components/bot/ChatImageLightbox.vue',
  'components/ssh/SftpImagePreview.vue',
  'components/ssh/SftpConflictDialog.vue',
  'components/ssh/SftpTransferDialog.vue',
  'components/ssh/docker/DockerNetworkDetailDialog.vue',
  'components/ssh/docker/DockerVolumeDetailDialog.vue',
  'views/ssh/DashboardView.vue',
  'components/ssh/git/GitReposPanel.vue',
  'components/ssh/SftpView.vue',
  'components/ssh/SftpFileEditor.vue',
  'components/home/HomeHistoryDialog.vue',
  'components/home/TaskStructureDialog.vue',
  'components/common/CommandPalette.vue',
  'components/settings/panels/ModelsPanel.vue',
  'components/ssh/docker/DockerConnectionPicker.vue',
  'components/ssh/docker/DockerContainerDetail.vue',
  'components/ssh/docker/DockerImageTransferModal.vue',
  'components/ssh/docker/DockerPullDialog.vue',
  'components/ssh/NewConnectionDialog.vue',
  'components/ssh/QuickCommandEditor.vue',
  'components/ssh/BulkCommandModal.vue',
  'components/ssh/git/GitRepoDetailPanel.vue',
  'components/ssh/GatewayDialogs.vue',
]

/** 单独断言过的几个（初始焦点不是容器，或有嵌入让位逻辑），不参与上面那条批量检查。 */
const MANUALLY_CHECKED = [
  'components/common/BaseModal.vue',
  'components/common/ConfirmDialog.vue',
  'components/ssh/TunnelModal.vue',
  'components/ssh/AccessModal.vue',
  // 独立弹 / 嵌入统一新建弹窗两种形态，role 与 tabindex 都是动态的
  'components/ssh/ConnectionModal.vue',
]

/** 唯一豁免：SettingsDialog 自持 topmostModal + setAppInert，设置页里所有模态的 Tab 都归它路由。 */
const TAB_HOSTED_BY = ['components/common/SettingsDialog.vue']

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

  it('已接入的自绘弹窗清单（SFTP / Docker 详情 / 终端首页 / git）', () => {
    for (const file of INTEGRATED) {
      const src = read(file)
      expect(src, `${file} 未接共享件`).toMatch(/composables\/useModalFocusTrap/)
      expect(src, `${file} 打开时未激活`).toContain('activate()')
      expect(src, `${file} 关闭时未还原`).toContain('deactivate()')
      // 容器要能接住初始焦点，否则 activate() 无处可去
      expect(src, `${file} 容器缺 tabindex="-1"`).toContain('tabindex="-1"')
      // 自绘遮罩的弹窗自己管 Esc；上面这批原先四个里一个都没有
      expect(src, `${file} 没有 Esc 退出路径`).toContain('Escape')
    }
  })

  it('弹窗叠层：上层压上来时下层让位，不抢 Tab 也不还原焦点', () => {
    // 编辑器的「未保存的更改」确认框就压在自己上面。两层都挂 window 监听的话
    // 一次 Tab 会被两个 set 各拦一次、焦点互相踢，所以要有暂停/恢复。
    expect(trap).toContain('function suspend()')
    expect(trap).toContain('function resume()')
    expect(trap).toContain('return { activate, deactivate, suspend, resume')

    const editor = read('components/ssh/SftpFileEditor.vue')
    expect(editor).toContain("from '../../composables/useModalFocusTrap'")
    // 编辑器本体一层、未保存确认框一层
    expect(editor).toMatch(/useModalFocusTrap\(\s*editorRef/)
    expect(editor).toMatch(/useModalFocusTrap\(\s*unsavedRef/)
    expect(editor).toContain('.suspend()')
    expect(editor).toContain('.resume()')
    expect(editor).toMatch(/initialFocus:\s*'\.unsaved-btn\.save'/)
  })

  it('SFTP 主视图的三个弹窗接上，连接列表可用键盘选中', () => {
    const view = read('components/ssh/SftpView.vue')
    // 连接选择 / 重命名·新建 / 压缩包命名
    expect(view).toMatch(/useModalFocusTrap\(\s*connRef/)
    expect(view).toMatch(/useModalFocusTrap\(\s*renameRef/)
    expect(view).toMatch(/useModalFocusTrap\(\s*zipRef/)
    // three 个弹窗容器都要能接住初始焦点
    expect(view.match(/tabindex="-1"/g)?.length ?? 0).toBeGreaterThanOrEqual(3)
    // Esc 从「只绑在输入框上」改为 window 级统一出口
    expect(view).toContain("e.key !== 'Escape'")
    // 连接条目此前是纯 div + @click：键盘根本选不到
    expect(view).toMatch(/class="conn-item"[\s\S]{0,200}?tabindex="0"/)
    expect(view).toMatch(/@keydown\.enter[^\n]*handleConnect/)
  })

  it('历史弹窗不再自带一份 Tab 实现，命令行面板的 Esc 不再只绑在输入框上', () => {
    const history = read('components/home/HomeHistoryDialog.vue')
    expect(history).toContain("from '../../composables/useModalFocusTrap'")
    expect(history).not.toContain('const FOCUSABLE')
    // 原先 @keydown 绑在容器上：焦点一旦漏到背后就收不到
    expect(history).not.toContain('@keydown="onKeydown"')

    const palette = read('components/common/CommandPalette.vue')
    expect(palette).toMatch(/initialFocus:\s*'\.palette-input'/)
    expect(palette).toMatch(/onMounted\(\(\) => \{[\s\S]*?window\.addEventListener\('keydown', onWindowKey\)/)
  })

  it('设置页里嵌套的对话框只拿「初始焦点 + 还原」，Tab 路由留给宿主', () => {
    // SettingsDialog 自持 topmostModal + inert：设置页里所有模态的 Tab 都归它
    const settings = read('components/common/SettingsDialog.vue')
    expect(settings).toContain('topmostModal')
    expect(settings).toContain('setAppInert')

    const models = read('components/settings/panels/ModelsPanel.vue')
    // 两边都拦 Tab 的话一次按键被先后拦两次，焦点跳格
    expect(models).toMatch(/manageTab:\s*false/)
    expect(models).toContain("from '../../../composables/useModalFocusTrap'")
    // 提供商选择此前是纯 div：键盘既选不到也打不开
    expect(models).toMatch(/class="provider-select"[\s\S]{0,120}?tabindex="0"/)
  })

  it('嵌入宿主的弹窗把对话框语义与 tabindex 一起让位', () => {
    // ConnectionModal 既能独立弹（SshView 直开），也能嵌进 NewConnectionDialog 的一页。
    // 嵌入时如果还给自己挂 role/tabindex，一次 Tab 会被宿主与它各拦一次。
    const conn = read('components/ssh/ConnectionModal.vue')
    expect(conn).toMatch(/:tabindex="embedded \? undefined : '-1'"/)
    expect(conn).toMatch(/if \(props\.embedded\) return\s*\n\s*activate\(\)/)
    expect(conn).toContain('deactivate()')
  })

  it('两处「选择 SSH 连接」列表能用键盘选（SFTP / 终端首页）', () => {
    // 两处都是 div + @click：弹窗能弹出、也能 Tab 到，但列表项根本进不了 Tab 序。
    const sftp = read('components/ssh/SftpView.vue')
    expect(sftp).toMatch(/class="conn-item"[\s\S]{0,200}?tabindex="0"/)
    expect(sftp).toMatch(/@keydown\.enter[^\n]*handleConnect/)

    const dash = read('views/ssh/DashboardView.vue')
    expect(dash).toMatch(/class="dc-item"[\s\S]{0,200}?tabindex="0"/)
    expect(dash).toMatch(/@keydown\.enter[^\n]*handleConnect/)
  })

  it('全仓的 role=dialog 都能在清单里找到（新增弹窗不许漏接）', () => {
    // 上面那份清单是人维护的，会漏；新增一个自绘弹窗时没人会想到回来登记。
    // 这里反向扫一遍：代码里写了 role=dialog 的文件，要么在上面，要么在豁免里。
    const declared = new Set([...INTEGRATED, ...MANUALLY_CHECKED, ...TAB_HOSTED_BY])
    const missing = walk(SRC)
      .filter(file => file.endsWith('.vue'))
      .filter(file => /role="(dialog|alertdialog)"|:role="[^"]*'dialog'/.test(readFileSync(file, 'utf8')))
      .map(rel)
      .filter(file => !declared.has(file))
    expect(missing, `这些文件有对话框语义但没接/没登记：${missing.join(', ')}`).toEqual([])
  })

  it('DESIGN.md 记着「焦点管理走共享件」', () => {
    expect(readFileSync(join(SRC, '..', 'DESIGN.md'), 'utf8')).toContain('useModalFocusTrap')
  })
})
