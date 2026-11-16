/**
 * AI 专属终端的页面级守卫（AI 的 SSH 与用户终端隔离 · 用户故事 3）
 *
 * AI 的远程会话由输入框选中连接建立（connectAiRemote，带 ai 标记），与用户终端
 * tab 隔离。终端页据此：① 给 AI 会话画「AI」徽标；② 禁用户输入（只读 —— AI 的
 * 命令仍经 sshWrite 直达通道，输出照常渲染）；③ 运行中不许关那条 tab。
 * 终端无法在 node 环境实例化，与 SshView.terminal-entry / terminal-page-polish
 * 同样用源码守卫把这三件事的接线钉住。
 */
import { describe, expect, it } from 'vitest'
import sshViewRaw from './SshView.vue?raw'
import remoteTerminalRaw from '@/components/ssh/RemoteTerminal.vue?raw'
import xtermRaw from '@/composables/useXtermInstance.ts?raw'
import sshViewCssRaw from './ssh-view.css?raw'

const strip = (s: string) =>
  s.replace(/\r\n/g, '\n').replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

const sshView = strip(sshViewRaw)
const remoteTerminal = strip(remoteTerminalRaw)
const xterm = strip(xtermRaw)

describe('AI 专属终端', () => {
  it('AI 会话 tab 画「AI」徽标，与用户终端区分', () => {
    expect(sshView).toContain('v-if="s.ai"')
    expect(sshView).toContain('class="tab-ai-badge"')
    // 徽标文字就是 AI（不是图标占位），且只对 ai 会话渲染
    expect(sshView).toMatch(/v-if="s\.ai"[^>]*class="tab-ai-badge"[^>]*>\s*AI\s*</)
    // 徽标有样式（强调色描边 + 可读文字色），不是裸 span
    expect(sshViewCssRaw).toContain('.tab-ai-badge')
  })

  it('运行中关 AI tab 被拦下并说明原因，其余会话照常关', () => {
    expect(sshView).toContain('useAgentStore')
    expect(sshView).toContain('function requestCloseSession')
    // 守卫判据：是 AI 会话且正在运行 —— 两者缺一都不拦
    expect(sshView).toMatch(/s\?\.ai && agent\.isRunning/)
    expect(sshView).toContain("toast.warning('AI 正在使用这条连接')")
    // 三个直接关闭出口都走守卫，不再裸调 store.closeSession
    expect(sshView).toContain('@click.stop="requestCloseSession(s.id)"')
    expect(sshView).toContain('@auxclick.middle.prevent="requestCloseSession(s.id)"')
    expect(sshView).toMatch(/function closeActiveSession[\s\S]{0,120}requestCloseSession/)
  })

  it('右键批量关闭运行中也不波及 AI tab', () => {
    expect(sshView).toMatch(/function handleTabCtxAction[\s\S]{0,500}agent\.isRunning/)
    expect(sshView).toMatch(/function handleTabCtxAction[\s\S]{0,500}AI 正在使用这条连接/)
  })

  it('AI 终端只读：RemoteTerminal 按会话 ai 标记注入 readOnly', () => {
    expect(remoteTerminal).toMatch(/readOnly:\s*\(\)\s*=>\s*!!sessionSelf\.value\?\.ai/)
  })

  it('只读脚手架：禁键盘输入、拦右键粘贴与补全，搜索仍放行', () => {
    expect(xterm).toContain('readOnly?: () => boolean')
    // 键盘输入由 xterm 原生 disableStdin 挡住（AI 的命令走 sshWrite，不经 onData）
    expect(xterm).toMatch(/disableStdin:\s*!!opts\.readOnly\?\.\(\)/)
    // 右键粘贴绕过 onData，必须单独拦
    expect(xterm).toMatch(/contextMenuHandler = \(e: Event\) => \{[\s\S]{0,120}opts\.readOnly\?\.\(\)/)
    // 补全是输入辅助：只读态在 document 键位拦截里早退（搜索唤起在此之前，仍放行）
    expect(xterm).toMatch(/if \(opts\.readOnly\?\.\(\)\) return/)
  })
})