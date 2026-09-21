/**
 * watchTerminalConfig — 换字号/字体后必须把新网格下发给 PTY 的回归测试。
 *
 * 字号变了但容器像素尺寸没变，ResizeObserver 不会触发 —— 少了 fit 之后的
 * 那次 resize，远端 PTY 仍按旧行列数渲染，vim/htop 直接串列。
 *
 * 测试跑在 node 环境，先补 localStorage / window 两个最小垫片（settings store 需要）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'

const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

import { useSettingsStore } from '@/stores/settings'
import { watchTerminalConfig } from '@/composables/useTerminalConfig'

function makeTerminal(cols = 80, rows = 24) {
  return { options: {} as Record<string, unknown>, cols, rows }
}

describe('watchTerminalConfig', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('字号变化重新 fit 后，网格变了就下发 resize', async () => {
    const t = makeTerminal()
    // fit() 模拟：字号越大，同容器能容纳的列数越少
    const fitAddon = {
      fit: vi.fn(() => {
        t.cols = (t.options.fontSize as number) > 14 ? 60 : 80
        t.rows = (t.options.fontSize as number) > 14 ? 17 : 24
      }),
    }
    const onResized = vi.fn()
    watchTerminalConfig(t as unknown as Terminal, fitAddon as unknown as FitAddon, () => true, onResized)

    const settings = useSettingsStore()
    settings.settings.appearance.terminal.fontSize = 16
    await nextTick()

    expect(t.options.fontSize).toBe(16)
    expect(fitAddon.fit).toHaveBeenCalledTimes(1)
    expect(onResized).toHaveBeenCalledTimes(1)
  })

  it('fit 后网格未变则不打扰 PTY', async () => {
    const t = makeTerminal()
    const fitAddon = { fit: vi.fn() }  // 不改 cols/rows
    const onResized = vi.fn()
    watchTerminalConfig(t as unknown as Terminal, fitAddon as unknown as FitAddon, () => true, onResized)

    const settings = useSettingsStore()
    settings.settings.appearance.terminal.fontFamily = 'consolas'
    await nextTick()

    expect(t.options.fontFamily).toContain('Consolas')
    expect(fitAddon.fit).toHaveBeenCalledTimes(1)
    expect(onResized).not.toHaveBeenCalled()
  })

  it('终端不可见时既不 fit 也不下发', async () => {
    const t = makeTerminal()
    const fitAddon = { fit: vi.fn() }
    const onResized = vi.fn()
    watchTerminalConfig(t as unknown as Terminal, fitAddon as unknown as FitAddon, () => false, onResized)

    const settings = useSettingsStore()
    settings.settings.appearance.terminal.fontSize = 18
    await nextTick()

    expect(t.options.fontSize).toBe(18)  // 字号仍然生效，只是留到可见时再算网格
    expect(fitAddon.fit).not.toHaveBeenCalled()
    expect(onResized).not.toHaveBeenCalled()
  })
})
