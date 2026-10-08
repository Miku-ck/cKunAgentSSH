/**
 * copy-cancel.ts — 本地复制（fs:copy）的协作式取消登记（S5-11）
 *
 * 背景：`sftp:cancel` 原先只通知 SSHManager 的传输表，而 fs:copy 是一条都不在表里
 * 注册的逐文件循环 ⇒ 渲染端对复制行点「取消」是静默空操作，行永远停在「复制中」。
 * fs:copy 的循环本身就在主进程里逐个 await，取消只需要在下一次迭代前看一眼标记。
 *
 * 表项必须随任务生命周期增删：transferId 由渲染端的计数器生成、会跨会话复用，
 * 留着已结束的残项会让下一次同 id 的复制一开头就被判成取消。
 */
export type CopyState = { cancelled: boolean }

const activeCopies = new Map<string, CopyState>()

/** 开跑前登记，返回循环里读的那个工作对象 */
export function beginCopy(transferId: string): CopyState {
  const state: CopyState = { cancelled: false }
  activeCopies.set(transferId, state)
  return state
}

/** 收尾（含失败/取消路径）必须调用，见 endCopy 的注释 */
export function endCopy(transferId: string): void {
  activeCopies.delete(transferId)
}

/**
 * 请求取消。返回是否命中一条在跑的复制 —— `sftp:cancel` 是同一条通道服务两类
 * 任务（SFTP 传输 + 本地复制），调用方拿这个值判断「有没有我份内的事要办」。
 */
export function requestCopyCancel(transferId: string): boolean {
  const state = activeCopies.get(transferId)
  if (!state) return false
  state.cancelled = true
  return true
}

/** 仅供测试与排障：当前登记了多少条在跑的复制 */
export function activeCopyCount(): number {
  return activeCopies.size
}
