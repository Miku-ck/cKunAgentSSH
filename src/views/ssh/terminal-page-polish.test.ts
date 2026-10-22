/**
 * 终端页轻量精修的页面级守卫：顶栏入口顺序、会话标签类型图标、
 * xterm 容器留白、命令坞空态动作。
 */
import { describe, expect, it } from 'vitest'
import sshViewRaw from './SshView.vue?raw'
import titleBarRaw from '@/layouts/TitleBar.vue?raw'
import quickCommandRaw from '@/components/ssh/QuickCommandBar.vue?raw'
import localTerminalRaw from '@/components/ssh/LocalTerminal.vue?raw'
import remoteTerminalRaw from '@/components/ssh/RemoteTerminal.vue?raw'

const strip = (s: string) =>
  s.replace(/\r\n/g, '\n').replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

const sshView = strip(sshViewRaw)
const titleBar = strip(titleBarRaw)
const quickCommand = strip(quickCommandRaw)
const localTerminal = strip(localTerminalRaw)
const remoteTerminal = strip(remoteTerminalRaw)

describe('终端页轻量精修', () => {
  it('顶栏按 SFTP → 终端（含远程管理）→ 监控 排列', () => {
    // 合并方案 A：原「远程」入口已并入「终端」，不再有独立按钮
    const sftp = titleBar.indexOf('aria-label="SFTP 文件传输"')
    const terminal = titleBar.indexOf('aria-label="终端（含远程管理）"')
    const monitor = titleBar.indexOf('aria-label="监控仪表盘"')
    expect(sftp).toBeGreaterThanOrEqual(0)
    expect(terminal).toBeGreaterThan(sftp)
    expect(monitor).toBeGreaterThan(terminal)
    expect(titleBar).not.toContain('aria-label="远程终端"')
  })

  it('本地与远程会话标签各带类型图标', () => {
    expect(sshView).toContain('<IconMonitor v-if="s.type === \'remote\'"')
    expect(sshView).toContain('<IconTerminal v-else')
    expect(sshView).toContain("import IconTerminal from '@/components/icons/IconTerminal.vue'")
  })

  it('终端容器内边距由 padding 提供（FitAddon 会扣掉）', () => {
    expect(localTerminal).toMatch(/\.local-terminal\s*\{[^}]*padding:\s*8px 10px/)
    expect(remoteTerminal).toMatch(/\.remote-terminal\s*\{[^}]*padding:\s*8px 10px/)
  })

  it('命令坞有标签与色点，空态是可直接添加的动作', () => {
    expect(quickCommand).toContain('qc-label')
    expect(quickCommand).toContain('qc-dot')
    expect(quickCommand).toContain('qc-empty-action')
    expect(quickCommand).toContain('添加快捷命令')
    expect(quickCommand).not.toContain('暂无快捷命令')
  })

})
