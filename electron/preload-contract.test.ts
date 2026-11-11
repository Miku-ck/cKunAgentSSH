/**
 * contextBridge 运行时契约守卫。
 *
 * 源码文本守卫（shared/ipc-contract.test.ts）只能证明「字符串对得上」，证明不了
 * 163 个方法真的被暴露、真的调到对应频道、监听真的能注销。这里 mock electron，
 * 加载真实 preload.ts 并捕获 exposeInMainWorld 的实参逐项验证。
 */
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { INVOKE_CHANNELS, EVENT_CHANNELS } from '../shared/ipc-contract'

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  on: vi.fn(),
  removeListener: vi.fn(),
  // 用普通对象记录暴露实参，不依赖 vi.fn() 的调用历史（hoisted mock 会被生命周期清理）
  exposed: { key: '', api: {} as Record<string, (...args: any[]) => any> },
}))

vi.mock('electron', () => ({
  contextBridge: {
    exposeInMainWorld: (key: string, value: Record<string, (...args: any[]) => any>) => {
      mocks.exposed.key = key
      mocks.exposed.api = value
    },
  },
  ipcRenderer: {
    invoke: mocks.invoke,
    on: mocks.on,
    removeListener: mocks.removeListener,
  },
  webUtils: { getPathForFile: vi.fn(() => '/tmp/probe') },
}))

beforeAll(async () => {
  await import('./preload')
})

describe('electronAPI 暴露形状', () => {
  it('挂在 electronAPI 上，共 166 个成员且全为函数', () => {
    expect(mocks.exposed.key).toBe('electronAPI')
    expect(Object.keys(mocks.exposed.api)).toHaveLength(166)
    for (const [key, fn] of Object.entries(mocks.exposed.api)) {
      expect(typeof fn, `${key} 不是函数`).toBe('function')
    }
    expect(typeof mocks.exposed.api.getPathForFile).toBe('function')
  })

  it('146 个 invoke 成员调用时把对应频道作为首参传给 ipcRenderer', () => {
    for (const key of Object.keys(INVOKE_CHANNELS)) {
      mocks.invoke.mockClear()
      mocks.exposed.api[key]('probe')
      expect(mocks.invoke, `${key} 没有发起 invoke`).toHaveBeenCalledTimes(1)
      expect(mocks.invoke.mock.calls[0][0], `${key} 频道不匹配`).toBe(
        INVOKE_CHANNELS[key as keyof typeof INVOKE_CHANNELS],
      )
    }
  })

  it('18 个监听成员注册与注销成对，且频道与回调对象一致', () => {
    for (const key of Object.keys(EVENT_CHANNELS)) {
      mocks.on.mockClear()
      mocks.removeListener.mockClear()
      const callback = vi.fn()
      const off = mocks.exposed.api[key](callback)
      expect(typeof off, `${key} 未返回取消监听函数`).toBe('function')
      const channel = EVENT_CHANNELS[key as keyof typeof EVENT_CHANNELS]
      expect(mocks.on.mock.calls[0][0], `${key} 注册频道不匹配`).toBe(channel)
      off()
      expect(mocks.removeListener.mock.calls[0][0], `${key} 注销频道不匹配`).toBe(channel)
      expect(mocks.removeListener.mock.calls[0][1]).toBe(mocks.on.mock.calls[0][1])
    }
  })

  it('监听回调只透传 data，不透传 event 对象', () => {
    mocks.on.mockClear()
    const callback = vi.fn()
    mocks.exposed.api.onBackendStatus(callback)
    const handler = mocks.on.mock.calls[0][1]
    handler({ sender: 'electron' }, { status: 'ready', port: 7777 })
    expect(callback).toHaveBeenCalledWith({ status: 'ready', port: 7777 })
    expect(callback).not.toHaveBeenCalledWith({ sender: 'electron' }, expect.anything())
  })
})
