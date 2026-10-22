/**
 * 终端入口的页面级守卫（B-1）
 *
 * 零会话时顶栏「终端」默认落到远程管理页（连接列表 + 标签条固定页签）；
 * 旧的本地 shell 启动页已删除，本地 shell 统一从标签条 ＋ 菜单开。
 * 这里把入口、默认页与标签语义钉住。
 */
import { describe, expect, it } from 'vitest'
import sshViewRaw from './SshView.vue?raw'
import titleBarRaw from '@/layouts/TitleBar.vue?raw'
import appRaw from '@/App.vue?raw'
import navRaw from '@/composables/useSshNavigation.ts?raw'

const strip = (s: string) =>
  s.replace(/\r\n/g, '\n').replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

const sshView = strip(sshViewRaw)
const titleBar = strip(titleBarRaw)
const app = strip(appRaw)
const nav = strip(navRaw)

describe('B-1 终端入口', () => {
  it('零会话默认落在远程管理页，旧启动页已删除', () => {
    expect(sshView).not.toContain('class="term-empty"')
    expect(sshView).not.toContain('term-shell-btn')
    expect(nav).toMatch(/function showTerminal[\s\S]{0,400}activeNavPage\.value = 'remote-mgr'/)
  })

  it('顶栏新增终端入口，并始终可点（不依赖会话是否存在）', () => {
    expect(titleBar).toContain('action-terminal')
    expect(titleBar).toContain("activePage === 'terminal'")
    expect(app).toContain('@action-terminal="sshViewRef?.showTerminal()"')
    expect(nav).toMatch(/showTerminal/)

    // TitlebarPage 必须认识 'terminal'，否则高亮态永远不亮
    expect(nav).toMatch(/type TitlebarPage = [^\n]*'terminal'/)
  })

  it('终端态能向 App 暴露，顶栏才知道何时点亮终端', () => {
    const expose = sshView.slice(sshView.indexOf('defineExpose('))
    expect(expose).toContain('showTerminal')
    expect(expose).toContain('terminalVisible')
  })

  it('会话标签暴露 tablist 语义，且标签条不再把 ＋ 菜单算进 tablist', () => {
    const listStart = sshView.indexOf('class="tabs-list" role="tablist"')
    expect(listStart, '标签容器缺少 role="tablist"').toBeGreaterThanOrEqual(0)
    expect(sshView).toContain('aria-label="终端会话"')
    const menuStart = sshView.indexOf('<NewTerminalMenu', listStart)
    expect(menuStart, '＋ 菜单应留在 tablist 外层').toBeGreaterThan(listStart)
    expect(sshView.lastIndexOf('</div>', menuStart), 'tablist 必须在 ＋ 菜单之前闭合').toBeGreaterThan(listStart)
  })

  it('远程管理以固定页签钉在标签条最左（合并方案 A）', () => {
    // 标志：顶栏「终端/远程」并为一个「终端」后，远程管理降级为标签条内
    // 的钉住页签——位于滚动区之前、不进 tablist、无关闭钮、不可拖拽；
    // 点击走 openRemoteManager（幂等，只开不关）。
    const mgrIdx = sshView.indexOf('term-tab-mgr')
    expect(mgrIdx, '固定页签缺失').toBeGreaterThanOrEqual(0)
    expect(sshView.indexOf('class="tabs-scroll"'), '固定页签必须在滚动区之前').toBeGreaterThan(mgrIdx)
    const chunk = sshView.slice(mgrIdx, sshView.indexOf('class="tabs-scroll"'))
    expect(chunk).toContain('openRemoteManager()')
    expect(chunk).not.toContain('tab-close')
    expect(chunk).not.toContain('draggable')
    // 零会话停在远程管理页时标签条也要显示，否则页签点不回来
    expect(sshView).toMatch(/v-show="\(\s*terminalVisible && activeNavPage === 'home' && store\.sessions\.length > 0\) \|\| activeNavPage === 'remote-mgr'"/)
  })
})
