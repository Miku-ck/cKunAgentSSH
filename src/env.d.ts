/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface FileEntry {
  name: string
  path: string
  isDirectory: boolean
  size: number
  modified: number
}

type SftpEntry = import('../shared/ipc-types').SftpEntry

type SSHConnectionRecord = import('../shared/ipc-types').SSHConnectionRecord

type SSHTestResult = import('../shared/ipc-types').SSHTestResult

/** 弹窗编辑器读取文件返回结构 */
interface EditorFileContent {
  content: string
  size: number
  readOnly: boolean
}

type SshReconnectStatus = import('../shared/ipc-types').SshReconnectStatus

type TunnelRule = import('../shared/ipc-types').TunnelRule

type TunnelStatus = import('../shared/ipc-types').TunnelStatus

/** GitHub 加速下载任务状态（主进程 → 渲染层，channel: ghaccel:download；不持久化） */
interface GhDownloadStatus {
  id: string
  state: 'downloading' | 'done' | 'error' | 'cancelled'
  path?: string
  fileName?: string
  transferred: number
  /** 未知（chunked / 续传未探测）为 0 */
  total: number
  error?: string
}

/** 加速自检的一行（主进程定义见 electron/github-accel.ts；skip = 未开启/环境不具备） */
interface GhaccelSelfCheckRow {
  key: 'git' | 'proxy' | 'ssh443' | 'hosts'
  state: 'ok' | 'fail' | 'skip'
  message: string
  latencyMs?: number
}

/** 镜像巡检状态（主进程 → 渲染层推送，channel: ghaccel:health；不持久化） */
interface GhaccelHealthStatus {
  state: 'unknown' | 'off' | 'ok' | 'degraded'
  activePrefix?: string
  activeName?: string
  latencyMs?: number
  /** 当前镜像失效原因（仅镜像；hosts 的问题见 hosts.message） */
  error?: string
  /** 当前镜像失效时探测到的最快备用（只提示，绝不自动切换） */
  fallback?: { prefix: string; name: string; latencyMs: number }
  /** hosts 托管块生效时的实时有效性；未写入不返回 */
  hosts?: { ok: boolean; message: string }
  /** 探测实际经过的代理（无代理不返回）：不经代理时探测结论与 git 真实体验可能不一致 */
  proxy?: string
  checkedAt: number
}

type GatewayRecord = import('../shared/ipc-types').GatewayRecord

type GatewayStatus = import('../shared/ipc-types').GatewayStatus

type GatewayHostKeyEvent = import('../shared/ipc-types').GatewayHostKeyEvent

/** 通道表的一行（主进程定义见 electron/transports/gateway-transport.ts） */
interface GatewayChannelRow {
  chan: number
  /** true = 网关发起（-R）；方向由 chan 奇偶隐含，这里给的是可读标签 */
  fromPeer: boolean
  dstHost: string
  dstPort: number
  bytesUp: number
  bytesDown: number
  /** peer-fin = 对端已单向 EOF，本侧仍可写 */
  state: 'open' | 'peer-fin'
}

interface ElectronAPI {
  windowMinimize: () => Promise<void>
  /**
   * 桌面偏好类型走 inline import：env.d.ts 不能有顶层 import，
   * 否则整个文件从全局脚本变成模块，下面所有 ambient 声明全部失效。
   */
  getDesktopPrefs: () => Promise<import('../shared/desktop-prefs').DesktopPrefs>
  setDesktopPrefs: (
    patch: Partial<import('../shared/desktop-prefs').DesktopPrefs>,
  ) => Promise<import('../shared/desktop-prefs').DesktopPrefs>
  notifyDesktop: (title: string, body: string) => Promise<boolean>
  /**
   * 自更新（tar 形态）。state 类型来自 shared/update-core，同样走 inline import
   * （env.d.ts 不能有顶层 import，否则全局声明全部失效）。
   */
  updateGetState: () => Promise<import('../shared/update-core').UpdateState | null>
  updateCheck: () => Promise<import('../shared/update-core').UpdateState | null>
  updateDownload: () => Promise<import('../shared/update-core').UpdateState | null>
  updateSetFeed: (url: string) => Promise<import('../shared/update-core').UpdateState | null>
  /** ok=false 表示标记没写成功/未就绪；true 之后主进程会退出，助手接手换包并重启 */
  updateApply: () => Promise<{
    ok: boolean
    state: import('../shared/update-core').UpdateState | null
  }>
  onUpdateState: (
    callback: (data: import('../shared/update-core').UpdateState) => void,
  ) => () => void
  /**
   * 诊断包（本地落盘，不联网）：system.json + 脱敏后的主日志 + 崩溃转储，打成 tar.gz。
   * 类型同样走 inline import（env.d.ts 不能有顶层 import）。
   */
  diagExport: () => Promise<import('../shared/diagnostics-core').DiagnosticsResult>
  /** 在系统文件管理器里定位最近一次导出的包（已删/移则退到产物目录） */
  diagReveal: () => Promise<void>
  windowMaximize: () => Promise<void>
  windowClose: () => Promise<void>
  onBackendStatus: (callback: (data: { status: string; port?: number; error?: string }) => void) => () => void
  getBackendPort: () => Promise<number>
  getBackendToken: () => Promise<string>
  /** 后端通过健康检查后才 resolve（值为实际端口）；getBackendPort 非零不代表就绪 */
  whenBackendReady: () => Promise<number>
  fsReadDir: (dirPath: string) => Promise<FileEntry[]>
  fsReadFile: (filePath: string) => Promise<string>
  fsWriteFile: (filePath: string, content: string) => Promise<void>
  fsReadFileForEditor: (filePath: string) => Promise<EditorFileContent>
  fsReadBinary: (filePath: string) => Promise<{ base64: string; size: number }>
  fsSelectFile: () => Promise<string | null>
  fsSelectImage: () => Promise<{ fileName: string; mime: string; name: string } | null>
  fsLoadBackgroundImage: (fileName: string) => Promise<string>
  fsDeleteBackgroundImage: (fileName: string) => Promise<boolean>
  fsGetHomePath: () => Promise<string>
  fsGetDrives: () => Promise<string[]>
  fsMkdir: (dirPath: string) => Promise<void>
  fsRename: (oldPath: string, newPath: string) => Promise<void>
  fsCopy: (srcPath: string, destPath: string, transferId: string) => Promise<void>
  fsTrash: (filePath: string) => Promise<void>
  fsPathJoin: (...segments: string[]) => Promise<string>
  fsExists: (filePath: string) => Promise<boolean>
  fsStat: (filePath: string) => Promise<{ isDirectory: boolean; isFile: boolean; size: number; modified: number }>
  fsResolveProgram: (name: string) => Promise<string | null>
  /** OS 原生拖拽的 File → 本地真实路径（Electron 32 起 File.path 已移除，S5-07） */
  getPathForFile: (file: File) => string
  fsZipLocal: (sources: string[], localZipPath: string) => Promise<void>
  fsUnzipLocal: (localZipPath: string, localDestDir: string) => Promise<void>
  ptyCreate: (opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => Promise<{ id: string; shell: string }>
  ptyWrite: (id: string, data: string) => Promise<void>
  ptyGetCwd: (id: string) => Promise<string>
  ptyResize: (id: string, cols: number, rows: number) => Promise<void>
  ptyDestroy: (id: string) => Promise<void>
  ptyCompleteCommand: (opts: { ptyId: string; cmd: string; prefix: string; cwd: string; isCommand: boolean; prevSegments: string[]; shell?: string }) => Promise<string>
  onPtyData: (callback: (data: { id: string; data: string }) => void) => () => void
  onPtyExit: (callback: (data: { id: string; exitCode: number }) => void) => () => void
  sshConnect: (config: {
    id: string; name: string; host: string; port: number; username: string;
    authType: 'password' | 'privateKey' | 'agent';
    password?: string; privateKeyPath?: string; passphrase?: string;
    readyTimeout?: number; keepaliveInterval?: number;
    cols?: number; rows?: number;
  }) => Promise<{ id: string; name: string }>
  sshConnectSftp: (config: {
    id: string; name: string; host: string; port: number; username: string;
    authType: 'password' | 'privateKey' | 'agent';
    password?: string; privateKeyPath?: string; passphrase?: string;
    readyTimeout?: number; keepaliveInterval?: number;
  }) => Promise<{ id: string; name: string }>
  sshWrite: (id: string, data: string) => Promise<void>
  sshResize: (id: string, cols: number, rows: number) => Promise<void>
  sshDisconnect: (id: string) => Promise<void>
  // 重连修复：手动重连已断开的会话（重置计数，重新走 3 次退避）。
  // cols/rows 传当前终端实际尺寸，主进程按 ssh:connect 同款规则钳制
  sshReconnect: (id: string, cols?: number, rows?: number) => Promise<void>
  sshGetCwd: (id: string) => Promise<string>
  // C8：timeoutSec 可选，默认 30 秒，主进程钳制 1..600
  sshExec: (sshSessionId: string, command: string, timeoutSec?: number) => Promise<{ stdout: string; stderr: string; exitCode: number | null }>
  // 终止远端进程：signal 可选 SIGTERM(15)/SIGKILL(9)/SIGHUP(1)/SIGINT(2)/SIGCONT(18)/SIGSTOP(19)/SIGTSTP(20)，默认 SIGTERM
  sshKillProcess: (sshSessionId: string, pid: number, signal?: number) => Promise<{ code: number; stdout: string; stderr: string }>
  onSshData: (callback: (data: { id: string; data: string }) => void) => () => void
  // 重连修复：willReconnect=true 表示意外断开且主进程将自动重连 —
  // 渲染层保留 tab 等待重连（不 removeSession）；false/缺省走原关闭清理流程。
  // 用户主动断开（关 tab/断开按钮）不会发出本事件
  onSshClose: (callback: (data: { id: string; willReconnect?: boolean }) => void) => () => void
  // 重连修复：重连状态变化（自动/手动重连进度，驱动终端系统行与状态区显示）
  onSshReconnect: (callback: (data: SshReconnectStatus) => void) => () => void
  // s1 修复：主机密钥验证事件（首次连接通知 + 密钥变更警告 + 钉扎表不可用拒绝）
  onHostKeyNew: (callback: (data: { hostKey: string; fingerprint: string }) => void) => () => void
  onHostKeyMismatch: (callback: (data: { hostKey: string; expected: string; got: string }) => void) => () => void
  onHostKeyUnpinned: (callback: (data: { hostKey: string; fingerprint: string }) => void) => () => void
  sshListConnections: () => Promise<SSHConnectionRecord[]>
  sshSaveConnection: (conn: SSHConnectionRecord) => Promise<SSHConnectionRecord>
  sshDeleteConnection: (id: string) => Promise<void>
  sshDuplicateConnection: (id: string) => Promise<SSHConnectionRecord>
  sshTestConnection: (config: Omit<SSHConnectionRecord, 'tags' | 'createdAt' | 'updatedAt'>) => Promise<SSHTestResult>
  sshMigrateLegacy: (legacy: any[]) => Promise<number>
  /** 打开 ~/.ssh/config（不存在时先建空文件再打开），返回实际路径 */
  sshOpenConfig: () => Promise<string>
  // SSH 隧道（端口转发）
  tunnelList: () => Promise<TunnelRule[]>
  tunnelSave: (rule: TunnelRule) => Promise<TunnelRule>
  tunnelDelete: (id: string) => Promise<void>
  tunnelStart: (id: string) => Promise<TunnelStatus>
  tunnelStop: (id: string) => Promise<TunnelStatus>
  tunnelStatusAll: () => Promise<TunnelStatus[]>
  /** 保存前预检：本机监听端口被占即时报（而非启动时才暴露 EADDRINUSE） */
  tunnelCheckPort: (host: string, port: number) => Promise<{ ok: boolean; message?: string }>
  onTunnelStatus: (callback: (data: TunnelStatus) => void) => () => void
  // GitHub 加速（镜像套件；主进程实现见 electron/github-accel.ts，类型源在 shared/github-accel-core.ts）
  ghaccelGetConfig: () => Promise<import('../shared/github-accel-core').GithubAccelConfig>
  ghaccelSaveLists: (proxies: import('../shared/github-accel-core').AccelProxy[]) => Promise<import('../shared/github-accel-core').GithubAccelConfig>
  ghaccelProbeAll: () => Promise<Array<{
    id: string
    name: string
    target: string
    ok: boolean
    latencyMs: number | null
    /** 实测下行速度（B/s）：仅代理行有（archive 维度） */
    speedBps?: number | null
    /** archive（整仓下载）维度是否可用 */
    archiveOk?: boolean
    status?: number
    error?: string
  }>>
  /**
   * 一键自动调优（总控按钮调用）：实测选优 + 淘汰连续失效 + 每次刷新候选池并在可用不足时自动采纳。
   * 会改镜像清单（内置只停用、自建/自动采纳的移除），因此返回详细的「做了什么」。
   */
  ghaccelAutoTune: () => Promise<{
    best: { id: string; name: string; prefix: string } | null
    probed: Array<{
      id: string
      name: string
      target: string
      ok: boolean
      latencyMs: number | null
      speedBps?: number | null
      archiveOk?: boolean
      status?: number
      error?: string
    }>
    retired: Array<{ id: string; name: string; action: 'disabled' | 'removed' }>
    adopted: Array<{ id: string; name: string; prefix: string }>
    discovered: number
  }>
  ghaccelGitStatus: () => Promise<{
    available: boolean
    entries: Array<{ key: string; subsection: string; value: string; push?: boolean }>
    active: boolean
    activeSubsection?: string
    managed: boolean
    backup: string | null
    /** 当前 push 目的地规则：none / 本应用直连 / 本应用 SSH / 用户自建 */
    pushRewrite: 'none' | 'direct' | 'ssh' | 'user'
    /** 用户自建 push 规则时，其子节（便于 UI 说明“不接管”） */
    pushSubsection?: string
    message?: string
  }>
  ghaccelGitEnable: (prefix: string, pushMode?: 'direct' | 'ssh') => Promise<ReturnType<ElectronAPI['ghaccelGitStatus']>>
  ghaccelGitDisable: () => Promise<ReturnType<ElectronAPI['ghaccelGitStatus']>>
  /** 端到端自检（只读）：git / 代理 / SSH 443 / hosts 四项 */
  ghaccelSelfCheck: () => Promise<GhaccelSelfCheckRow[]>
  /** 最近一次巡检结果（标题栏挂载时拉取，不必等下一次推送） */
  ghaccelHealthStatus: () => Promise<GhaccelHealthStatus>
  ghaccelSsh443Status: () => Promise<{ applied: boolean; path: string; exists: boolean; message?: string }>
  ghaccelSsh443Apply: () => Promise<{ applied: boolean; path: string; exists: boolean }>
  ghaccelSsh443Remove: () => Promise<{ applied: boolean; path: string; exists: boolean }>
  // 私人令牌（PAT）配置（读原文 / 写全量；渲染层 maskToken 脱敏展示）
  ghaccelGetTokens: () => Promise<import('../shared/github-accel-core').GitPrivateToken[]>
  ghaccelSaveTokens: (tokens: import('../shared/github-accel-core').GitPrivateToken[]) =>
    Promise<import('../shared/github-accel-core').GitPrivateToken[]>
  // GitHub 加速下载器（主进程实现见 electron/gh-download.ts）
  ghaccelDownloadStart: (url: string, opts?: { gunzip?: boolean }) => Promise<GhDownloadStatus>
  ghaccelDownloadCancel: (id: string) => Promise<void>
  ghaccelDownloadList: () => Promise<GhDownloadStatus[]>
  ghaccelShowItemInFolder: (id: string) => Promise<void>
  onGhaccelDownload: (callback: (data: GhDownloadStatus) => void) => () => void
  onGhaccelHealth: (callback: (data: GhaccelHealthStatus) => void) => () => void
  // hosts 优选 IP（主进程实现见 electron/gh-hosts.ts）
  ghaccelHostsStatus: () => Promise<{
    applied: boolean
    path: string
    writable: boolean
    entries: { domain: string; ip: string }[]
    /** 块的生成时间原文（ISO 串）；null = 块内无可比对的 `# generated:` 行 */
    generatedAt: string | null
    /** 块写入至今的天数；null = 块内无可比对的生成时间戳（旧版本或手改的块） */
    staleDays: number | null
    /** 是否该提示重选（超阈值或时效未知） */
    stale: boolean
    message?: string
  }>
  ghaccelHostsPick: () => Promise<Array<{ domain: string; ip: string | null; latencyMs: number | null; candidates: number; verified: boolean; error?: string }>>
  ghaccelHostsApply: (entries?: { domain: string; ip: string }[]) => Promise<ReturnType<ElectronAPI['ghaccelHostsStatus']>>
  ghaccelHostsRemove: () => Promise<ReturnType<ElectronAPI['ghaccelHostsStatus']>>
  ghaccelSaveOptions: (patch: { lastProxyPrefix?: string; pushMode?: 'direct' | 'ssh'; hostsAutoRefresh?: boolean }) =>
    Promise<import('../shared/github-accel-core').GithubAccelConfig>
  // Git 仓库管理（主进程实现见 electron/git-repos.ts；命令/解析纯逻辑在 shared/git-repos-core.ts）
  gitreposGetRegistry: () => Promise<import('../shared/git-repos-core').GitReposRegistry>
  /** 只读：枚举某本机仓库的全部 git worktree（任务台「工作树」落点用） */
  gitreposListWorktrees: (path: string) => Promise<import('../shared/git-repos-core').GitWorktree[]>
  /** 新建工作树：自动建分支 + 仓库旁目录，返回可直接落点的 worktree */
  gitreposCreateWorktree: (path: string, branch: string) => Promise<import('../shared/git-repos-core').GitWorktree>
  /** 清理失效工作树记录（git worktree prune，不碰现存目录） */
  gitreposPruneWorktrees: (path: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  gitreposAddLocal: (path: string) => Promise<import('../shared/git-repos-core').GitReposRegistry>
  gitreposRemoveLocal: (path: string) => Promise<import('../shared/git-repos-core').GitReposRegistry>
  gitreposSetRemotePaths: (connectionId: string, paths: string[]) => Promise<import('../shared/git-repos-core').GitReposRegistry>
  /** 本机目录选择器（登记本地仓库入口）；取消返回 null */
  gitreposPickDirectory: () => Promise<string | null>
  gitreposStatusAll: (paths: string[]) => Promise<import('../shared/git-repos-core').LocalRepoStatusResult[]>
  gitreposFetchOne: (path: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  gitreposPullOne: (path: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  /** V2 写操作：切分支 / 提交所选 / 推送（远程侧由渲染层 sshExec 下发并计入审计） */
  gitreposSwitchBranch: (path: string, branch: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  gitreposCommit: (path: string, files: string[], msg: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  /** 选中文件对 HEAD 的 diff 原始输出（只读，供提交信息 AI 归纳） */
  gitreposDiffFiles: (path: string, files: string[]) => Promise<import('../shared/git-repos-core').GitRawResult>
  gitreposPush: (path: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  /** 详情三段原始输出（status / branch -vv / log -20），解析在渲染层 */
  gitreposDetail: (path: string) => Promise<{ ok: boolean; error?: string; statusRaw?: string; branchRaw?: string; logRaw?: string }>
  /** 提交图谱原始输出（--graph --all + \x01 分列格式），解析在渲染层 parseGraphLog */
  gitreposGraphLog: (path: string, limit: number) => Promise<import('../shared/git-repos-core').GitRawResult>
  // 三仓同步（主仓 + 只读镜像；实现 electron/git-repos.ts，分型纯逻辑在 shared/git-repos-core.ts）
  gitreposSyncProbe: (paths: string[]) => Promise<import('../shared/git-repos-core').SyncProbeResult[]>
  /** remotes 缺省 = 全部落后远端，传单元素 = 单家补齐；逐家回报非原子 */
  gitreposSyncCatchup: (path: string, remotes?: string[]) => Promise<import('../shared/git-repos-core').SyncCatchupResult>
  gitreposSyncMerge: (path: string, remote: string) => Promise<import('../shared/git-repos-core').GitRawResult>
  // TLS 接入网关（安全接入）
  gatewayList: () => Promise<GatewayRecord[]>
  gatewaySave: (rec: GatewayRecord) => Promise<GatewayRecord>
  gatewayDelete: (id: string) => Promise<void>
  gatewayConnect: (id: string) => Promise<GatewayStatus>
  gatewayDisconnect: (id: string) => Promise<GatewayStatus>
  gatewayStatusAll: () => Promise<GatewayStatus[]>
  gatewayChannels: (id: string) => Promise<GatewayChannelRow[]>
  gatewayTrustHostKey: (id: string, spki: string) => Promise<void>
  /** 取消 TOFU 确认：主进程清掉待确认指纹（不落库），下次连接重新问 */
  gatewayHostKeyReject: (id: string) => Promise<void>
  onGatewayStatus: (callback: (data: GatewayStatus) => void) => () => void
  onGatewayHostKeyNew: (callback: (data: GatewayHostKeyEvent) => void) => () => void
  // Skills
  listSkills: (mode?: string) => Promise<any[]>
  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) => Promise<{ id: string; enabled: boolean }>
  deleteSkill: (id: string) => Promise<{ deleted: boolean }>
  syncBuiltinSkills: (skills: any[]) => Promise<{ upserted: number; orphans_removed: number; total: number }>

  // SFTP
  sftpOpen: (sshSessionId: string) => Promise<void>
  sftpClose: (sshSessionId: string) => Promise<void>
  sftpList: (sshSessionId: string, remotePath: string) => Promise<SftpEntry[]>
  sftpMkdir: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpMkdirRecursive: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpExists: (sshSessionId: string, remotePath: string) => Promise<boolean>
  sftpStat: (sshSessionId: string, remotePath: string) => Promise<{ size: number; isDirectory: boolean; isSymbolicLink: boolean; modified: number }>
  sftpRename: (sshSessionId: string, oldPath: string, newPath: string) => Promise<void>
  sftpDelete: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpChmod: (sshSessionId: string, remotePath: string, mode: number) => Promise<void>
  sftpUpload: (transferId: string, sshSessionId: string, localPath: string, remotePath: string, meta?: import('../shared/upload-task-core').UploadTaskMeta) => Promise<void>
  sftpDownload: (transferId: string, sshSessionId: string, remotePath: string, localPath: string, meta?: import('../shared/upload-task-core').UploadTaskMeta) => Promise<void>
  sftpCancel: (transferId: string) => Promise<void>
  sftpRegisterUploadTasks: (sshSessionId: string, tasks: import('../shared/upload-task-core').RegisteredUploadTask[]) => Promise<void>
  sftpListUploadTasks: (connectionId: string) => Promise<import('../shared/upload-task-core').UploadTaskRecord[]>
  sftpRemoveUploadTask: (id: string) => Promise<void>
  sftpClearUploadTasks: (connectionId: string) => Promise<void>
  sftpPauseUploadTask: (id: string, interrupted: boolean) => Promise<void>
  sftpGetSessionConnectionId: (sshSessionId: string) => Promise<string | null>
  sftpReadFile: (sshSessionId: string, remotePath: string) => Promise<EditorFileContent>
  sftpReadBinary: (sshSessionId: string, remotePath: string) => Promise<{ base64: string; size: number }>
  sftpWriteFile: (sshSessionId: string, remotePath: string, content: string) => Promise<void>
  sftpZipRemote: (sshSessionId: string, sources: string[], remoteZipPath: string, cwd: string) => Promise<{ stdout: string; stderr: string; exitCode: number | null }>
  sftpUnzipRemote: (sshSessionId: string, remoteZipPath: string, remoteDestDir: string) => Promise<{ stdout: string; stderr: string; exitCode: number | null }>
  sshCompleteCommand: (sshSessionId: string, opts: { cmd: string; prefix: string; isCommand: boolean; prevSegments: string[] }) => Promise<string>
  onSftpProgress: (callback: (data: { id: string; transferred: number; total: number }) => void) => () => void
  onSftpStalled: (callback: (data: { id: string }) => void) => () => void
  onSftpClosed: (callback: (data: { id: string }) => void) => () => void
}

interface Window {
  electronAPI?: ElectronAPI
}
