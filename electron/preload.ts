import { contextBridge, ipcRenderer, webUtils } from 'electron'
import { INVOKE_CHANNELS, EVENT_CHANNELS } from '../shared/ipc-contract'
import type { ApiKey } from '../shared/ipc-contract'
import { onEventOf } from './preload-helpers'
import type { SshReconnectStatus } from './managers/ssh-manager'
import type { TunnelStatus } from './managers/tunnel-manager'
import type { GatewayHostKeyEvent, GatewayStatus } from './managers/gateway-manager'
import type {
  RegisteredUploadTask,
  UploadTaskMeta,
  UploadTaskRecord,
} from '../shared/upload-task-core'
import type { UpdateState as UpdateStatePayload } from '../shared/update-core'
import type { DiagnosticsResult as DiagnosticsResultPayload } from '../shared/diagnostics-core'

/**
 * 安全暴露给渲染进程的 API
 * 渲染进程通过 window.electronAPI 调用
 */
contextBridge.exposeInMainWorld('electronAPI', {
  // 窗口控制
  windowMinimize: () => ipcRenderer.invoke(INVOKE_CHANNELS.windowMinimize),
  windowMaximize: () => ipcRenderer.invoke(INVOKE_CHANNELS.windowMaximize),
  windowClose: () => ipcRenderer.invoke(INVOKE_CHANNELS.windowClose),

  // 桌面偏好（自启 / 关窗隐藏到托盘 / 通知）与桌面通知
  getDesktopPrefs: () => ipcRenderer.invoke(INVOKE_CHANNELS.getDesktopPrefs),
  setDesktopPrefs: (patch: Record<string, boolean>) => ipcRenderer.invoke(INVOKE_CHANNELS.setDesktopPrefs, patch),
  notifyDesktop: (title: string, body: string) => ipcRenderer.invoke(INVOKE_CHANNELS.notifyDesktop, title, body),

  // 自动更新（tar 形态）。apply 成功返回后主进程会自行退出，助手接手换包并重启。
  updateGetState: () => ipcRenderer.invoke(INVOKE_CHANNELS.updateGetState),
  updateCheck: () => ipcRenderer.invoke(INVOKE_CHANNELS.updateCheck),
  updateDownload: () => ipcRenderer.invoke(INVOKE_CHANNELS.updateDownload),
  updateSetFeed: (url: string) => ipcRenderer.invoke(INVOKE_CHANNELS.updateSetFeed, url),
  updateApply: (): Promise<{ ok: boolean; state: UpdateStatePayload | null }> =>
    ipcRenderer.invoke(INVOKE_CHANNELS.updateApply),
  /** 每次状态迁移推一条（与 backend:status 同形）；返回取消监听函数 */
  onUpdateState: onEventOf<UpdateStatePayload>(EVENT_CHANNELS.onUpdateState),

  // 诊断包（本地落盘，不联网）：导出成功返回包路径，reveal 在系统文件管理器里定位它
  diagExport: (): Promise<DiagnosticsResultPayload> => ipcRenderer.invoke(INVOKE_CHANNELS.diagExport),
  diagReveal: () => ipcRenderer.invoke(INVOKE_CHANNELS.diagReveal),

  // 后端状态监听
  /**
   * 监听后端状态变化
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onBackendStatus: onEventOf<{ status: string; port?: number; error?: string }>(EVENT_CHANNELS.onBackendStatus),

  // 后端信息
  getBackendPort: () => ipcRenderer.invoke(INVOKE_CHANNELS.getBackendPort),
  getBackendToken: () => ipcRenderer.invoke(INVOKE_CHANNELS.getBackendToken),
  // 真就绪（健康检查通过）才 resolve；getBackendPort 非零不代表就绪
  whenBackendReady: () => ipcRenderer.invoke(INVOKE_CHANNELS.whenBackendReady),

  // 文件系统
  fsReadDir: (dirPath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsReadDir, dirPath),
  fsReadFile: (filePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsReadFile, filePath),
  fsWriteFile: (filePath: string, content: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsWriteFile, filePath, content),
  fsReadFileForEditor: (filePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsReadFileForEditor, filePath),
  fsReadBinary: (filePath: string): Promise<{ base64: string; size: number }> =>
    ipcRenderer.invoke(INVOKE_CHANNELS.fsReadBinary, filePath),
  fsGetHomePath: () => ipcRenderer.invoke(INVOKE_CHANNELS.fsGetHomePath),
  fsGetDrives: () => ipcRenderer.invoke(INVOKE_CHANNELS.fsGetDrives),
  fsMkdir: (dirPath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsMkdir, dirPath),
  fsRename: (oldPath: string, newPath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsRename, oldPath, newPath),
  fsCopy: (srcPath: string, destPath: string, transferId: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsCopy, srcPath, destPath, transferId),
  fsTrash: (filePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsTrash, filePath),
  fsPathJoin: (...segments: string[]) => ipcRenderer.invoke(INVOKE_CHANNELS.fsPathJoin, ...segments),
  fsExists: (filePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsExists, filePath),
  fsStat: (filePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsStat, filePath),
  fsResolveProgram: (name: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsResolveProgram, name),
  /**
   * OS 原生拖拽取本地真实路径（S5-07）。
   * Electron 32 起 `File.path` 被移除，contextIsolation 下渲染层拿不到它，
   * 于是「拖文件到远程侧上传」这条路一直是死的（拖任何文件都只弹
   * 「无法获取拖入文件的本地路径」）。webUtils.getPathForFile 是唯一受支持的
   * 替代，且只能在 preload 里调用——渲染层直接把 File 传过来即可。
   */
  getPathForFile: (file: File) => webUtils.getPathForFile(file),
  // 本地 ZIP 打包/解压
  fsZipLocal: (sources: string[], localZipPath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsZipLocal, sources, localZipPath),
  fsUnzipLocal: (localZipPath: string, localDestDir: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsUnzipLocal, localZipPath, localDestDir),

  // PTY 终端
  ptyCreate: (opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => ipcRenderer.invoke(INVOKE_CHANNELS.ptyCreate, opts),
  ptyWrite: (id: string, data: string) => ipcRenderer.invoke(INVOKE_CHANNELS.ptyWrite, id, data),
  ptyResize: (id: string, cols: number, rows: number) => ipcRenderer.invoke(INVOKE_CHANNELS.ptyResize, id, cols, rows),
  ptyDestroy: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.ptyDestroy, id),
  ptyGetCwd: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.ptyGetCwd, id),
  ptyCompleteCommand: (opts: { ptyId: string; cmd: string; prefix: string; cwd: string; isCommand: boolean; prevSegments: string[]; shell?: string }) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.ptyCompleteCommand, opts),
  /**
   * 监听 PTY 终端输出数据
   * @param callback 数据回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onPtyData: onEventOf<{ id: string; data: string }>(EVENT_CHANNELS.onPtyData),
  /**
   * 监听 PTY 终端退出事件
   * @param callback 退出回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onPtyExit: onEventOf<{ id: string; exitCode: number }>(EVENT_CHANNELS.onPtyExit),

  // SSH 远程连接
  sshConnect: (config: any) => ipcRenderer.invoke(INVOKE_CHANNELS.sshConnect, config),
  sshConnectSftp: (config: any) => ipcRenderer.invoke(INVOKE_CHANNELS.sshConnectSftp, config),
  sshWrite: (id: string, data: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sshWrite, id, data),
  sshResize: (id: string, cols: number, rows: number) => ipcRenderer.invoke(INVOKE_CHANNELS.sshResize, id, cols, rows),
  sshDisconnect: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sshDisconnect, id),
  // 重连修复：手动重连已断开的会话（重置计数，重新走 3 次退避）。
  // cols/rows 传当前终端实际尺寸，主进程按 ssh:connect 同款规则钳制
  sshReconnect: (id: string, cols?: number, rows?: number) => ipcRenderer.invoke(INVOKE_CHANNELS.sshReconnect, id, cols, rows),
  sshGetCwd: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sshGetCwd, id),
  sshExec: (id: string, command: string, timeoutSec?: number) => ipcRenderer.invoke(INVOKE_CHANNELS.sshExec, id, command, timeoutSec),
  /** 终止远端进程。signal 默认 SIGTERM(15)，可选 SIGKILL(9) 等 */
  sshKillProcess: (id: string, pid: number, signal?: number) => ipcRenderer.invoke(INVOKE_CHANNELS.sshKillProcess, id, pid, signal),
  /**
   * 监听 SSH 终端输出数据
   * @param callback 数据回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshData: onEventOf<{ id: string; data: string }>(EVENT_CHANNELS.onSshData),
  /**
   * 监听 SSH 连接关闭事件
   * willReconnect: 该断开是否为"意外断开且主进程将自动重连"（渲染层据此
   * 决定保留 tab 等待重连，还是走原有关闭清理流程）。用户主动断开不发本事件。
   * @param callback 关闭回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshClose: onEventOf<{ id: string; willReconnect?: boolean }>(EVENT_CHANNELS.onSshClose),
  /**
   * 监听 SSH 重连状态变化（自动/手动重连进度，主进程推送）
   * phase: 'reconnecting' 首轮排程 | 'retry' 上一次失败后的重试排程
   *       | 'success' 重连成功 | 'gave-up' 放弃（3 次失败 / 连接配置已删除 / 重连被取消）
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSshReconnect: onEventOf<SshReconnectStatus>(EVENT_CHANNELS.onSshReconnect),
  /**
   * 监听新主机密钥（首次连接 TOFU 信任通知）
   * s1 修复：让用户知道新服务器指纹已被记录
   */
  onHostKeyNew: onEventOf<{ hostKey: string; fingerprint: string }>(EVENT_CHANNELS.onHostKeyNew),
  /**
   * 监听主机密钥不匹配（可能的中间人攻击警告）
   * s1 修复：前端可据此展示严重安全警告
   */
  onHostKeyMismatch: onEventOf<{ hostKey: string; expected: string; got: string }>(EVENT_CHANNELS.onHostKeyMismatch),
  /**
   * 监听「钉扎表读不出来、无法判断这台主机是否见过」的拒绝事件。
   * 与 host-key-mismatch 分开发：那边的文案断言"密钥变更/中间人"，这里断言的是
   * 本机 known_hosts 不可用，两种处置动作不同，混用会让用户去查服务器而不是查磁盘。
   */
  onHostKeyUnpinned: onEventOf<{ hostKey: string; fingerprint: string }>(EVENT_CHANNELS.onHostKeyUnpinned),

  // SSH 连接配置管理
  sshListConnections: () => ipcRenderer.invoke(INVOKE_CHANNELS.sshListConnections),
  sshSaveConnection: (conn: any) => ipcRenderer.invoke(INVOKE_CHANNELS.sshSaveConnection, conn),
  sshDeleteConnection: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sshDeleteConnection, id),
  sshDuplicateConnection: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sshDuplicateConnection, id),
  sshTestConnection: (config: any) => ipcRenderer.invoke(INVOKE_CHANNELS.sshTestConnection, config),
  sshMigrateLegacy: (legacy: any[]) => ipcRenderer.invoke(INVOKE_CHANNELS.sshMigrateLegacy, legacy),
  sshOpenConfig: () => ipcRenderer.invoke(INVOKE_CHANNELS.sshOpenConfig),

  // SSH 隧道（端口转发）
  tunnelList: () => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelList),
  tunnelSave: (rule: any) => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelSave, rule),
  tunnelDelete: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelDelete, id),
  tunnelStart: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelStart, id),
  tunnelStop: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelStop, id),
  tunnelStatusAll: () => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelStatusAll),
  // 保存前预检：本机监听端口被占即时报（而非启动时才暴露 EADDRINUSE）
  tunnelCheckPort: (host: string, port: number) => ipcRenderer.invoke(INVOKE_CHANNELS.tunnelCheckPort, host, port),

  // GitHub 加速（镜像套件）
  ghaccelGetConfig: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelGetConfig),
  ghaccelSaveLists: (proxies: any[]) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSaveLists, proxies),
  ghaccelProbeAll: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelProbeAll),
  ghaccelAutoTune: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelAutoTune),
  ghaccelGitStatus: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelGitStatus),
  ghaccelGitEnable: (prefix: string, pushMode?: 'direct' | 'ssh') => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelGitEnable, prefix, pushMode),
  ghaccelGitDisable: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelGitDisable),
  ghaccelSelfCheck: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSelfCheck),
  ghaccelHealthStatus: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelHealthStatus),
  ghaccelSsh443Status: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSsh443Status),
  ghaccelSsh443Apply: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSsh443Apply),
  ghaccelSsh443Remove: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSsh443Remove),
  // 私人令牌（PAT）配置（读原文 / 写全量；渲染层自身脱敏展示）
  ghaccelGetTokens: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelGetTokens),
  ghaccelSaveTokens: (tokens: any[]) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSaveTokens, tokens),
  // GitHub 加速下载器
  ghaccelDownloadStart: (url: string, opts?: { gunzip?: boolean }) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelDownloadStart, url, opts),
  ghaccelDownloadCancel: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelDownloadCancel, id),
  ghaccelDownloadList: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelDownloadList),
  ghaccelShowItemInFolder: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelShowItemInFolder, id),
  // hosts 优选 IP（增强模式）
  ghaccelHostsStatus: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelHostsStatus),
  ghaccelHostsPick: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelHostsPick),
  ghaccelHostsApply: (entries?: { domain: string; ip: string }[]) => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelHostsApply, entries),
  ghaccelHostsRemove: () => ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelHostsRemove),
  ghaccelSaveOptions: (patch: { lastProxyPrefix?: string; pushMode?: 'direct' | 'ssh'; hostsAutoRefresh?: boolean }) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.ghaccelSaveOptions, patch),
  // Git 仓库管理（本地注册表 + 本地 git 执行；远端命令由渲染层经 sshExec 下发）
  gitreposGetRegistry: () => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposGetRegistry),
  gitreposListWorktrees: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposListWorktrees, path),
  gitreposCreateWorktree: (path: string, branch: string) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.gitreposCreateWorktree, path, branch),
  gitreposPruneWorktrees: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposPruneWorktrees, path),
  gitreposAddLocal: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposAddLocal, path),
  gitreposRemoveLocal: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposRemoveLocal, path),
  gitreposSetRemotePaths: (connectionId: string, paths: string[]) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.gitreposSetRemotePaths, connectionId, paths),
  gitreposPickDirectory: () => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposPickDirectory),
  gitreposStatusAll: (paths: string[]) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposStatusAll, paths),
  gitreposFetchOne: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposFetchOne, path),
  gitreposPullOne: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposPullOne, path),
  // V2 写操作（详情弹窗动作）：远程侧不经这里，渲染层 sshExec 下发并计入审计
  gitreposSwitchBranch: (path: string, branch: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposSwitchBranch, path, branch),
  gitreposCommit: (path: string, files: string[], msg: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposCommit, path, files, msg),
  /** 选中文件对 HEAD 的 diff 原始输出（只读，供提交信息 AI 归纳），截断在渲染层 clampDiffText */
  gitreposDiffFiles: (path: string, files: string[]) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposDiffFiles, path, files),
  gitreposPush: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposPush, path),
  gitreposDetail: (path: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposDetail, path),
  /** 提交图谱原始输出（--graph --all + \x01 分列格式），解析在渲染层 parseGraphLog */
  gitreposGraphLog: (path: string, limit: number) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposGraphLog, path, limit),
  // 三仓同步（主仓 + 只读镜像）：探测 / 补齐 / 分叉合并
  gitreposSyncProbe: (paths: string[]) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposSyncProbe, paths),
  /** remotes 缺省 = 全部落后远端，传单元素 = 单家补齐；逐家回报非原子 */
  gitreposSyncCatchup: (path: string, remotes?: string[]) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposSyncCatchup, path, remotes),
  gitreposSyncMerge: (path: string, remote: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gitreposSyncMerge, path, remote),
  /** 下载进度与状态迁移（一条通道，state 区分）；返回取消监听函数 */
  onGhaccelDownload: onEventOf<any>(EVENT_CHANNELS.onGhaccelDownload),
  /** 镜像巡检结果（主进程每 30 分钟/配置变更后推送）；返回取消监听函数 */
  onGhaccelHealth: onEventOf<any>(EVENT_CHANNELS.onGhaccelHealth),
  /**
   * 监听隧道状态变化（主进程推送，每次状态迁移一条）
   * @param callback 状态回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onTunnelStatus: onEventOf<TunnelStatus>(EVENT_CHANNELS.onTunnelStatus),

  // TLS 接入网关（安全接入）
  gatewayList: () => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayList),
  gatewaySave: (rec: any) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewaySave, rec),
  gatewayDelete: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayDelete, id),
  gatewayConnect: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayConnect, id),
  gatewayDisconnect: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayDisconnect, id),
  gatewayStatusAll: () => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayStatusAll),
  gatewayChannels: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayChannels, id),
  gatewayTrustHostKey: (id: string, spki: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayTrustHostKey, id, spki),
  // 取消 TOFU：让主进程清掉待确认指纹，否则「取消」只是把弹窗收起来（什么也没撤销）
  gatewayHostKeyReject: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.gatewayHostKeyReject, id),
  /** 会话状态变化（含重连退避进度），每次迁移推一条 */
  onGatewayStatus: onEventOf<GatewayStatus>(EVENT_CHANNELS.onGatewayStatus),
  /** 首次见到网关证书（TOFU）：UI 确认后调 gatewayTrustHostKey 落库 */
  onGatewayHostKeyNew: onEventOf<GatewayHostKeyEvent>(EVENT_CHANNELS.onGatewayHostKeyNew),
  fsSelectFile: () => ipcRenderer.invoke(INVOKE_CHANNELS.fsSelectFile),
  fsSelectImage: () => ipcRenderer.invoke(INVOKE_CHANNELS.fsSelectImage),
  fsLoadBackgroundImage: (fileName: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsLoadBackgroundImage, fileName),
  fsDeleteBackgroundImage: (fileName: string) => ipcRenderer.invoke(INVOKE_CHANNELS.fsDeleteBackgroundImage, fileName),

  // 技能（含自进化生成的 auto skills）
  listSkills: (mode?: string) => ipcRenderer.invoke(INVOKE_CHANNELS.listSkills, mode),
  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) => ipcRenderer.invoke(INVOKE_CHANNELS.updateSkillEnabled, id, enabled, mode),
  deleteSkill: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.deleteSkill, id),
  syncBuiltinSkills: (skills: any[]) => ipcRenderer.invoke(INVOKE_CHANNELS.syncBuiltinSkills, skills),

  // SFTP 文件传输
  sftpOpen: (sshSessionId: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpOpen, sshSessionId),
  sftpClose: (sshSessionId: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpClose, sshSessionId),
  sftpList: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpList, sshSessionId, remotePath),
  sftpMkdir: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpMkdir, sshSessionId, remotePath),
  sftpMkdirRecursive: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpMkdirRecursive, sshSessionId, remotePath),
  sftpExists: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpExists, sshSessionId, remotePath),
  sftpStat: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpStat, sshSessionId, remotePath),
  sftpRename: (sshSessionId: string, oldPath: string, newPath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpRename, sshSessionId, oldPath, newPath),
  sftpDelete: (sshSessionId: string, remotePath: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpDelete, sshSessionId, remotePath),
  sftpChmod: (sshSessionId: string, remotePath: string, mode: number) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpChmod, sshSessionId, remotePath, mode),
  sftpUpload: (transferId: string, sshSessionId: string, localPath: string, remotePath: string, meta?: UploadTaskMeta) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpUpload, transferId, sshSessionId, localPath, remotePath, meta),
  sftpDownload: (transferId: string, sshSessionId: string, remotePath: string, localPath: string, meta?: UploadTaskMeta) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpDownload, transferId, sshSessionId, remotePath, localPath, meta),
  sftpCancel: (transferId: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpCancel, transferId),
  // 上传记录（主进程权威写入；渲染层只读展示与显式操作）
  sftpRegisterUploadTasks: (sshSessionId: string, tasks: RegisteredUploadTask[]) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpRegisterUploadTasks, sshSessionId, tasks),
  sftpListUploadTasks: (connectionId: string): Promise<UploadTaskRecord[]> =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpListUploadTasks, connectionId),
  sftpRemoveUploadTask: (id: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpRemoveUploadTask, id),
  sftpClearUploadTasks: (connectionId: string) => ipcRenderer.invoke(INVOKE_CHANNELS.sftpClearUploadTasks, connectionId),
  sftpPauseUploadTask: (id: string, interrupted: boolean) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpPauseUploadTask, id, interrupted),
  sftpGetSessionConnectionId: (sshSessionId: string): Promise<string | null> =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpGetSessionConnectionId, sshSessionId),
  sftpReadFile: (sshSessionId: string, remotePath: string) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpReadFile, sshSessionId, remotePath),
  sftpReadBinary: (sshSessionId: string, remotePath: string): Promise<{ base64: string; size: number }> =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpReadBinary, sshSessionId, remotePath),
  sftpWriteFile: (sshSessionId: string, remotePath: string, content: string) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpWriteFile, sshSessionId, remotePath, content),
  // 远程 ZIP 打包/解压（通过 SSH exec 执行）
  sftpZipRemote: (sshSessionId: string, sources: string[], remoteZipPath: string, cwd: string) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpZipRemote, sshSessionId, sources, remoteZipPath, cwd),
  sftpUnzipRemote: (sshSessionId: string, remoteZipPath: string, remoteDestDir: string) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sftpUnzipRemote, sshSessionId, remoteZipPath, remoteDestDir),
  // P3 修复：补上 prevSegments — main.ts 的 ssh:completeCommand 处理器实际消费该字段
  // （buildBashCompletionScript 依赖它构建补全上下文），原类型漏声明
  sshCompleteCommand: (sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments: string[] }) =>
    ipcRenderer.invoke(INVOKE_CHANNELS.sshCompleteCommand, sshSessionId, opts),
  /**
   * 监听 SFTP 文件传输进度
   * @param callback 进度回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSftpProgress: onEventOf<{ id: string; transferred: number; total: number }>(EVENT_CHANNELS.onSftpProgress),
  /**
   * 监听 SFTP 传输停滞事件（30s 无进度）
   * @param callback 停滞回调
   * @returns 取消监听函数
   */
  onSftpStalled: onEventOf<{ id: string }>(EVENT_CHANNELS.onSftpStalled),
  /**
   * 监听 SFTP 传输关闭事件
   * @param callback 关闭回调
   * @returns 取消监听函数，务必在组件 onUnmounted 时调用以避免内存泄漏
   */
  onSftpClosed: onEventOf<{ id: string }>(EVENT_CHANNELS.onSftpClosed),
} satisfies Record<ApiKey, (...args: never[]) => unknown>)

// ElectronAPI / FileEntry 类型声明位于 src/env.d.ts（渲染进程通过 window.electronAPI 消费）
