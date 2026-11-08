import type { BrowserWindow } from 'electron'

/**
 * 窗口销毁守卫下的 IPC 推送。
 *
 * 改造前这段守卫在 pty-manager / ssh-manager / tunnel-manager 三个类里各一份、
 * main.ts 与 fs-handler.ts 里再内联 4 份，共 7 份逐字相同的副本。这里收成单点：
 * 窗口为空或已销毁时直接返回；webContents.send 抛错（EPIPE 等）静默忽略。
 * 返回值与异常行为与改造前完全一致——不新增日志、不重抛。
 */
export function safeSend(
  win: BrowserWindow | null | undefined,
  channel: string,
  data: unknown,
): void {
  if (win && !win.isDestroyed()) {
    try { win.webContents.send(channel, data) } catch { /* EPIPE / 窗口已销毁 */ }
  }
}
