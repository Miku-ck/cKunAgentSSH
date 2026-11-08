import { ipcRenderer } from 'electron'

/**
 * 与改造前 preload.ts 的监听模板逐字等价：注册 → 返回 removeListener 闭包。
 * 泛型只约束回调数据类型，不改变运行时行为（handler 仍是 (_event, data) => callback(data)）。
 */
export function onEventOf<T>(channel: string) {
  return (callback: (data: T) => void) => {
    const handler = (_event: unknown, data: T) => callback(data)
    ipcRenderer.on(channel, handler)
    return () => ipcRenderer.removeListener(channel, handler)
  }
}
