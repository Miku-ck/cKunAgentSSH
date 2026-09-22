import { contextBridge, ipcRenderer } from 'electron'
import type { SshReconnectStatus } from './ssh-manager'

/**
 * 安全暴露给渲染进程的 API
 * 渲染进程通过 window.electronAPI 调用
 */
contextBridge.exposeInMainWorld('electronAPI', {
  // 窗口控制
  windowMinimize: () => ipcRenderer.invoke('win:minimize'),
  windowMaximize: () => ipcRenderer.invoke('win:maximize'),
  windowClose: () => ipcRenderer.invoke('win:close'),

  // 桌面偏好（自启 / 关窗隐藏到托盘 / 通知）与桌面通知
  getDesktopPrefs: () => ipcRenderer.invoke('prefs:get'),
  setDesktopPrefs: (patch: Record<string, boolean>) => ipcRenderer.invoke('prefs:set', patch),
  notifyDesktop: (title: string, body: string) => ipcRenderer.invoke('app:notify', title, body),

  // 后端状态监听
  /**
   * 监听后端状态变化
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onBackendStatus: (callback: (data: { status: string; port?: number; error?: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('backend:status', handler)
    return () => ipcRenderer.removeListener('backend:status', handler)
  },

  // 后端信息
  getBackendPort: () => ipcRenderer.invoke('backend:getPort'),
  getBackendToken: () => ipcRenderer.invoke('backend:getToken'),

  // 文件系统
  fsReadDir: (dirPath: string) => ipcRenderer.invoke('fs:readDir', dirPath),
  fsReadFile: (filePath: string) => ipcRenderer.invoke('fs:readFile', filePath),
  fsWriteFile: (filePath: string, content: string) => ipcRenderer.invoke('fs:writeFile', filePath, content),
  fsReadFileForEditor: (filePath: string) => ipcRenderer.invoke('fs:readFileForEditor', filePath),
  fsGetHomePath: () => ipcRenderer.invoke('fs:getHomePath'),
  fsGetDrives: () => ipcRenderer.invoke('fs:getDrives'),
  fsMkdir: (dirPath: string) => ipcRenderer.invoke('fs:mkdir', dirPath),
  fsRename: (oldPath: string, newPath: string) => ipcRenderer.invoke('fs:rename', oldPath, newPath),
  fsCopy: (srcPath: string, destPath: string, transferId: string) => ipcRenderer.invoke('fs:copy', srcPath, destPath, transferId),
  fsTrash: (filePath: string) => ipcRenderer.invoke('fs:trash', filePath),
  fsPathJoin: (...segments: string[]) => ipcRenderer.invoke('fs:pathJoin', ...segments),
  fsExists: (filePath: string) => ipcRenderer.invoke('fs:exists', filePath),
  fsStat: (filePath: string) => ipcRenderer.invoke('fs:stat', filePath),
  fsResolveProgram: (name: string) => ipcRenderer.invoke('fs:resolveProgram', name),
  // 本地 ZIP 打包/解压
  fsZipLocal: (sources: string[], localZipPath: string) => ipcRenderer.invoke('fs:zipLocal', sources, localZipPath),
  fsUnzipLocal: (localZipPath: string, localDestDir: string) => ipcRenderer.invoke('fs:unzipLocal', localZipPath, localDestDir),

  // PTY 终端
  ptyCreate: (opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => ipcRenderer.invoke('pty:create', opts),
  ptyWrite: (id: string, data: string) => ipcRenderer.invoke('pty:write', id, data),
  ptyResize: (id: string, cols: number, rows: number) => ipcRenderer.invoke('pty:resize', id, cols, rows),
  ptyDestroy: (id: string) => ipcRenderer.invoke('pty:destroy', id),
  ptyGetCwd: (id: string) => ipcRenderer.invoke('pty:getCwd', id),
  ptyCompleteCommand: (opts: { ptyId: string; cmd: string; prefix: string; cwd: string; isCommand: boolean; prevSegments: string[]; shell?: string }) =>
    ipcRenderer.invoke('pty:completeCommand', opts),
  /**
   * 监听 PTY 终端输出数据
   * @param callback 数据回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onPtyData: (callback: (data: { id: string; data: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('pty:data', handler)
    return () => ipcRenderer.removeListener('pty:data', handler)
  },
  /**
   * 监听 PTY 终端退出事件
   * @param callback 退出回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onPtyExit: (callback: (data: { id: string; exitCode: number }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('pty:exit', handler)
    return () => ipcRenderer.removeListener('pty:exit', handler)
  },

  // SSH 远程连接
  sshConnect: (config: any) => ipcRenderer.invoke('ssh:connect', config),
  sshConnectSftp: (config: any) => ipcRenderer.invoke('ssh:connectSftp', config),
  sshWrite: (id: string, data: string) => ipcRenderer.invoke('ssh:write', id, data),
  sshResize: (id: string, cols: number, rows: number) => ipcRenderer.invoke('ssh:resize', id, cols, rows),
  sshDisconnect: (id: string) => ipcRenderer.invoke('ssh:disconnect', id),
  // 重连修复：手动重连已断开的会话（重置计数，重新走 3 次退避）。
  // cols/rows 传当前终端实际尺寸，主进程按 ssh:connect 同款规则钳制
  sshReconnect: (id: string, cols?: number, rows?: number) => ipcRenderer.invoke('ssh:reconnect', id, cols, rows),
  sshGetCwd: (id: string) => ipcRenderer.invoke('ssh:getCwd', id),
  sshExec: (id: string, command: string, timeoutSec?: number) => ipcRenderer.invoke('ssh:exec', id, command, timeoutSec),
  /**
   * 监听 SSH 终端输出数据
   * @param callback 数据回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshData: (callback: (data: { id: string; data: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:data', handler)
    return () => ipcRenderer.removeListener('ssh:data', handler)
  },
  /**
   * 监听 SSH 连接关闭事件
   * willReconnect: 该断开是否为"意外断开且主进程将自动重连"（渲染层据此
   * 决定保留 tab 等待重连，还是走原有关闭清理流程）。用户主动断开不发本事件。
   * @param callback 关闭回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshClose: (callback: (data: { id: string; willReconnect?: boolean }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:close', handler)
    return () => ipcRenderer.removeListener('ssh:close', handler)
  },
  /**
   * 监听 SSH 重连状态变化（自动/手动重连进度，主进程推送）
   * phase: 'reconnecting' 首轮排程 | 'retry' 上一次失败后的重试排程
   *       | 'success' 重连成功 | 'gave-up' 放弃（3 次失败 / 连接配置已删除 / 重连被取消）
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshReconnect: (callback: (data: SshReconnectStatus) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:reconnect', handler)
    return () => ipcRenderer.removeListener('ssh:reconnect', handler)
  },
  /**
   * 监听新主机密钥（首次连接 TOFU 信任通知）
   * s1 修复：让用户知道新服务器指纹已被记录
   */
  onHostKeyNew: (callback: (data: { hostKey: string; fingerprint: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:host-key-new', handler)
    return () => ipcRenderer.removeListener('ssh:host-key-new', handler)
  },
  /**
   * 监听主机密钥不匹配（可能的中间人攻击警告）
   * s1 修复：前端可据此展示严重安全警告
   */
  onHostKeyMismatch: (callback: (data: { hostKey: string; expected: string; got: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:host-key-mismatch', handler)
    return () => ipcRenderer.removeListener('ssh:host-key-mismatch', handler)
  },

  // SSH 连接配置管理
  sshListConnections: () => ipcRenderer.invoke('ssh:listConnections'),
  sshSaveConnection: (conn: any) => ipcRenderer.invoke('ssh:saveConnection', conn),
  sshDeleteConnection: (id: string) => ipcRenderer.invoke('ssh:deleteConnection', id),
  sshDuplicateConnection: (id: string) => ipcRenderer.invoke('ssh:duplicateConnection', id),
  sshTestConnection: (config: any) => ipcRenderer.invoke('ssh:testConnection', config),
  sshMigrateLegacy: (legacy: any[]) => ipcRenderer.invoke('ssh:migrateLegacy', legacy),
  fsSelectFile: () => ipcRenderer.invoke('fs:selectFile'),
  fsSelectImage: () => ipcRenderer.invoke('fs:selectImage'),
  fsLoadBackgroundImage: (fileName: string) => ipcRenderer.invoke('fs:loadBackgroundImage', fileName),
  fsDeleteBackgroundImage: (fileName: string) => ipcRenderer.invoke('fs:deleteBackgroundImage', fileName),

  // 技能（含自进化生成的 auto skills）
  listSkills: (mode?: string) => ipcRenderer.invoke('skills:list', mode),
  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) => ipcRenderer.invoke('skills:updateEnabled', id, enabled, mode),
  deleteSkill: (id: string) => ipcRenderer.invoke('skills:delete', id),
  syncBuiltinSkills: (skills: any[]) => ipcRenderer.invoke('skills:syncBuiltin', skills),

  // SFTP 文件传输
  sftpOpen: (sshSessionId: string) => ipcRenderer.invoke('sftp:open', sshSessionId),
  sftpClose: (sshSessionId: string) => ipcRenderer.invoke('sftp:close', sshSessionId),
  sftpList: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:list', sshSessionId, remotePath),
  sftpMkdir: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:mkdir', sshSessionId, remotePath),
  sftpMkdirRecursive: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:mkdirRecursive', sshSessionId, remotePath),
  sftpExists: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:exists', sshSessionId, remotePath),
  sftpRename: (sshSessionId: string, oldPath: string, newPath: string) => ipcRenderer.invoke('sftp:rename', sshSessionId, oldPath, newPath),
  sftpDelete: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:delete', sshSessionId, remotePath),
  sftpUpload: (transferId: string, sshSessionId: string, localPath: string, remotePath: string) =>
    ipcRenderer.invoke('sftp:upload', transferId, sshSessionId, localPath, remotePath),
  sftpDownload: (transferId: string, sshSessionId: string, remotePath: string, localPath: string) =>
    ipcRenderer.invoke('sftp:download', transferId, sshSessionId, remotePath, localPath),
  sftpCancel: (transferId: string) => ipcRenderer.invoke('sftp:cancel', transferId),
  sftpReadFile: (sshSessionId: string, remotePath: string) =>
    ipcRenderer.invoke('sftp:readFile', sshSessionId, remotePath),
  sftpWriteFile: (sshSessionId: string, remotePath: string, content: string) =>
    ipcRenderer.invoke('sftp:writeFile', sshSessionId, remotePath, content),
  // 远程 ZIP 打包/解压（通过 SSH exec 执行）
  sftpZipRemote: (sshSessionId: string, sources: string[], remoteZipPath: string, cwd: string) =>
    ipcRenderer.invoke('sftp:zipRemote', sshSessionId, sources, remoteZipPath, cwd),
  sftpUnzipRemote: (sshSessionId: string, remoteZipPath: string, remoteDestDir: string) =>
    ipcRenderer.invoke('sftp:unzipRemote', sshSessionId, remoteZipPath, remoteDestDir),
  // P3 修复：补上 prevSegments — main.ts 的 ssh:completeCommand 处理器实际消费该字段
  // （buildBashCompletionScript 依赖它构建补全上下文），原类型漏声明
  sshCompleteCommand: (sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments?: string[] }) =>
    ipcRenderer.invoke('ssh:completeCommand', sshSessionId, opts),
  /**
   * 监听 SFTP 文件传输进度
   * @param callback 进度回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSftpProgress: (callback: (data: { id: string; transferred: number; total: number }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('sftp:progress', handler)
    return () => ipcRenderer.removeListener('sftp:progress', handler)
  },
  /**
   * 监听 SFTP 传输停滞事件（30s 无进度）
   * @param callback 停滞回调
   * @returns 取消监听函数
   */
  onSftpStalled: (callback: (data: { id: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('sftp:stalled', handler)
    return () => ipcRenderer.removeListener('sftp:stalled', handler)
  },
  /**
   * 监听 SFTP 传输关闭事件
   * @param callback 关闭回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSftpClosed: (callback: (data: { id: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('sftp:closed', handler)
    return () => ipcRenderer.removeListener('sftp:closed', handler)
  },
})

// ElectronAPI / FileEntry 类型声明位于 src/env.d.ts（渲染进程通过 window.electronAPI 消费）
