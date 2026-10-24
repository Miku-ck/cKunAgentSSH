import { contextBridge, ipcRenderer, webUtils } from 'electron'
import type { SshReconnectStatus } from './ssh-manager'
import type { TunnelStatus } from './tunnel-manager'
import type { GatewayHostKeyEvent, GatewayStatus } from './gateway-manager'

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
  // 真就绪（健康检查通过）才 resolve；getBackendPort 非零不代表就绪
  whenBackendReady: () => ipcRenderer.invoke('backend:whenReady'),

  // 文件系统
  fsReadDir: (dirPath: string) => ipcRenderer.invoke('fs:readDir', dirPath),
  fsReadFile: (filePath: string) => ipcRenderer.invoke('fs:readFile', filePath),
  fsWriteFile: (filePath: string, content: string) => ipcRenderer.invoke('fs:writeFile', filePath, content),
  fsReadFileForEditor: (filePath: string) => ipcRenderer.invoke('fs:readFileForEditor', filePath),
  fsReadBinary: (filePath: string): Promise<{ base64: string; size: number }> =>
    ipcRenderer.invoke('fs:readBinary', filePath),
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
  /**
   * OS 原生拖拽取本地真实路径（S5-07）。
   * Electron 32 起 `File.path` 被移除，contextIsolation 下渲染层拿不到它，
   * 于是「拖文件到远程侧上传」这条路一直是死的（拖任何文件都只弹
   * 「无法获取拖入文件的本地路径」）。webUtils.getPathForFile 是唯一受支持的
   * 替代，且只能在 preload 里调用——渲染层直接把 File 传过来即可。
   */
  getPathForFile: (file: File) => webUtils.getPathForFile(file),
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
  /** 终止远端进程。signal 默认 SIGTERM(15)，可选 SIGKILL(9) 等 */
  sshKillProcess: (id: string, pid: number, signal?: number) => ipcRenderer.invoke('ssh:killProcess', id, pid, signal),
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
  /**
   * 监听「钉扎表读不出来、无法判断这台主机是否见过」的拒绝事件。
   * 与 host-key-mismatch 分开发：那边的文案断言"密钥变更/中间人"，这里断言的是
   * 本机 known_hosts 不可用，两种处置动作不同，混用会让用户去查服务器而不是查磁盘。
   */
  onHostKeyUnpinned: (callback: (data: { hostKey: string; fingerprint: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ssh:host-key-unpinned', handler)
    return () => ipcRenderer.removeListener('ssh:host-key-unpinned', handler)
  },

  // SSH 连接配置管理
  sshListConnections: () => ipcRenderer.invoke('ssh:listConnections'),
  sshSaveConnection: (conn: any) => ipcRenderer.invoke('ssh:saveConnection', conn),
  sshDeleteConnection: (id: string) => ipcRenderer.invoke('ssh:deleteConnection', id),
  sshDuplicateConnection: (id: string) => ipcRenderer.invoke('ssh:duplicateConnection', id),
  sshTestConnection: (config: any) => ipcRenderer.invoke('ssh:testConnection', config),
  sshMigrateLegacy: (legacy: any[]) => ipcRenderer.invoke('ssh:migrateLegacy', legacy),

  // SSH 隧道（端口转发）
  tunnelList: () => ipcRenderer.invoke('tunnel:list'),
  tunnelSave: (rule: any) => ipcRenderer.invoke('tunnel:save', rule),
  tunnelDelete: (id: string) => ipcRenderer.invoke('tunnel:delete', id),
  tunnelStart: (id: string) => ipcRenderer.invoke('tunnel:start', id),
  tunnelStop: (id: string) => ipcRenderer.invoke('tunnel:stop', id),
  tunnelStatusAll: () => ipcRenderer.invoke('tunnel:statusAll'),
  // 保存前预检：本机监听端口被占即时报（而非启动时才暴露 EADDRINUSE）
  tunnelCheckPort: (host: string, port: number) => ipcRenderer.invoke('tunnel:checkPort', host, port),

  // GitHub 加速（镜像套件）
  ghaccelGetConfig: () => ipcRenderer.invoke('ghaccel:getConfig'),
  ghaccelSaveLists: (proxies: any[], webMirrors: any[]) => ipcRenderer.invoke('ghaccel:saveLists', proxies, webMirrors),
  ghaccelProbeAll: () => ipcRenderer.invoke('ghaccel:probeAll'),
  ghaccelGitStatus: () => ipcRenderer.invoke('ghaccel:gitStatus'),
  ghaccelGitEnable: (prefix: string) => ipcRenderer.invoke('ghaccel:gitEnable', prefix),
  ghaccelGitDisable: () => ipcRenderer.invoke('ghaccel:gitDisable'),
  ghaccelSelfCheck: () => ipcRenderer.invoke('ghaccel:selfCheck'),
  ghaccelHealthStatus: () => ipcRenderer.invoke('ghaccel:healthStatus'),
  ghaccelSsh443Status: () => ipcRenderer.invoke('ghaccel:ssh443Status'),
  ghaccelSsh443Apply: () => ipcRenderer.invoke('ghaccel:ssh443Apply'),
  ghaccelSsh443Remove: () => ipcRenderer.invoke('ghaccel:ssh443Remove'),
  ghaccelOpenExternal: (url: string) => ipcRenderer.invoke('ghaccel:openExternal', url),
  // GitHub 加速下载器
  ghaccelDownloadStart: (url: string, opts?: { gunzip?: boolean }) => ipcRenderer.invoke('ghaccel:downloadStart', url, opts),
  ghaccelDownloadCancel: (id: string) => ipcRenderer.invoke('ghaccel:downloadCancel', id),
  ghaccelDownloadList: () => ipcRenderer.invoke('ghaccel:downloadList'),
  ghaccelShowItemInFolder: (id: string) => ipcRenderer.invoke('ghaccel:showItemInFolder', id),
  // hosts 优选 IP（增强模式）
  ghaccelHostsStatus: () => ipcRenderer.invoke('ghaccel:hostsStatus'),
  ghaccelHostsPick: () => ipcRenderer.invoke('ghaccel:hostsPick'),
  ghaccelHostsApply: (entries?: { domain: string; ip: string }[]) => ipcRenderer.invoke('ghaccel:hostsApply', entries),
  ghaccelHostsRemove: () => ipcRenderer.invoke('ghaccel:hostsRemove'),
  ghaccelSaveOptions: (patch: { lastProxyPrefix?: string }) =>
    ipcRenderer.invoke('ghaccel:saveOptions', patch),
  // Git 仓库管理（本地注册表 + 本地 git 执行；远端命令由渲染层经 sshExec 下发）
  gitreposGetRegistry: () => ipcRenderer.invoke('gitrepos:getRegistry'),
  gitreposAddLocal: (path: string) => ipcRenderer.invoke('gitrepos:addLocal', path),
  gitreposRemoveLocal: (path: string) => ipcRenderer.invoke('gitrepos:removeLocal', path),
  gitreposSetRemotePaths: (connectionId: string, paths: string[]) =>
    ipcRenderer.invoke('gitrepos:setRemotePaths', connectionId, paths),
  gitreposPickDirectory: () => ipcRenderer.invoke('gitrepos:pickDirectory'),
  gitreposStatusAll: (paths: string[]) => ipcRenderer.invoke('gitrepos:statusAll', paths),
  gitreposFetchOne: (path: string) => ipcRenderer.invoke('gitrepos:fetchOne', path),
  gitreposPullOne: (path: string) => ipcRenderer.invoke('gitrepos:pullOne', path),
  // V2 写操作（详情弹窗动作）：远程侧不经这里，渲染层 sshExec 下发并计入审计
  gitreposSwitchBranch: (path: string, branch: string) => ipcRenderer.invoke('gitrepos:switchBranch', path, branch),
  gitreposCommit: (path: string, files: string[], msg: string) => ipcRenderer.invoke('gitrepos:commit', path, files, msg),
  gitreposPush: (path: string) => ipcRenderer.invoke('gitrepos:push', path),
  gitreposDetail: (path: string) => ipcRenderer.invoke('gitrepos:detail', path),
  /** 提交图谱原始输出（--graph --all + \x01 分列格式），解析在渲染层 parseGraphLog */
  gitreposGraphLog: (path: string, limit: number) => ipcRenderer.invoke('gitrepos:graphLog', path, limit),
  /** 下载进度与状态迁移（一条通道，state 区分）；返回取消监听函数 */
  onGhaccelDownload: (callback: (data: any) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ghaccel:download', handler)
    return () => ipcRenderer.removeListener('ghaccel:download', handler)
  },
  /** 镜像巡检结果（主进程每 30 分钟/配置变更后推送）；返回取消监听函数 */
  onGhaccelHealth: (callback: (data: any) => void) => {
    const handler = (_event: any, data: any) => callback(data)
    ipcRenderer.on('ghaccel:health', handler)
    return () => ipcRenderer.removeListener('ghaccel:health', handler)
  },
  /**
   * 监听隧道状态变化（主进程推送，每次状态迁移一条）
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onTunnelStatus: (callback: (data: TunnelStatus) => void) => {
    const handler = (_event: any, data: TunnelStatus) => callback(data)
    ipcRenderer.on('tunnel:status', handler)
    return () => ipcRenderer.removeListener('tunnel:status', handler)
  },

  // TLS 接入网关（安全接入）
  gatewayList: () => ipcRenderer.invoke('gateway:list'),
  gatewaySave: (rec: any) => ipcRenderer.invoke('gateway:save', rec),
  gatewayDelete: (id: string) => ipcRenderer.invoke('gateway:delete', id),
  gatewayConnect: (id: string) => ipcRenderer.invoke('gateway:connect', id),
  gatewayDisconnect: (id: string) => ipcRenderer.invoke('gateway:disconnect', id),
  gatewayStatusAll: () => ipcRenderer.invoke('gateway:statusAll'),
  gatewayChannels: (id: string) => ipcRenderer.invoke('gateway:channels', id),
  gatewayTrustHostKey: (id: string, spki: string) => ipcRenderer.invoke('gateway:trustHostKey', id, spki),
  // 取消 TOFU：让主进程清掉待确认指纹，否则「取消」只是把弹窗收起来（什么也没撤销）
  gatewayHostKeyReject: (id: string) => ipcRenderer.invoke('gateway:hostKeyReject', id),
  /** 会话状态变化（含重连退避进度），每次迁移推一条 */
  onGatewayStatus: (callback: (data: GatewayStatus) => void) => {
    const handler = (_event: any, data: GatewayStatus) => callback(data)
    ipcRenderer.on('gateway:status', handler)
    return () => ipcRenderer.removeListener('gateway:status', handler)
  },
  /** 首次见到网关证书（TOFU）：UI 确认后调 gatewayTrustHostKey 落库 */
  onGatewayHostKeyNew: (callback: (data: GatewayHostKeyEvent) => void) => {
    const handler = (_event: any, data: GatewayHostKeyEvent) => callback(data)
    ipcRenderer.on('gateway:host-key-new', handler)
    return () => ipcRenderer.removeListener('gateway:host-key-new', handler)
  },
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
  sftpStat: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:stat', sshSessionId, remotePath),
  sftpRename: (sshSessionId: string, oldPath: string, newPath: string) => ipcRenderer.invoke('sftp:rename', sshSessionId, oldPath, newPath),
  sftpDelete: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke('sftp:delete', sshSessionId, remotePath),
  sftpUpload: (transferId: string, sshSessionId: string, localPath: string, remotePath: string) =>
    ipcRenderer.invoke('sftp:upload', transferId, sshSessionId, localPath, remotePath),
  sftpDownload: (transferId: string, sshSessionId: string, remotePath: string, localPath: string) =>
    ipcRenderer.invoke('sftp:download', transferId, sshSessionId, remotePath, localPath),
  sftpCancel: (transferId: string) => ipcRenderer.invoke('sftp:cancel', transferId),
  sftpReadFile: (sshSessionId: string, remotePath: string) =>
    ipcRenderer.invoke('sftp:readFile', sshSessionId, remotePath),
  sftpReadBinary: (sshSessionId: string, remotePath: string): Promise<{ base64: string; size: number }> =>
    ipcRenderer.invoke('sftp:readBinary', sshSessionId, remotePath),
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
