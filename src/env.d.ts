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

/** SFTP 远程目录条目 */
interface SftpEntry {
  name: string
  path: string
  isDirectory: boolean
  size: number
  modified: number
  mode: string
}

/** SSH 连接配置(渲染进程与主进程共用形态) */
interface SSHConnectionRecord {
  id: string
  name: string
  host: string
  port: number
  username: string
  authType: 'password' | 'privateKey' | 'agent'
  password?: string
  privateKeyPath?: string
  passphrase?: string
  /** 主进程列表接口附带的布尔标志（不含凭据内容）：是否已保存密码/passphrase，
   *  供编辑弹窗提示"留空则保持原值"，避免误触"请输入密码"或误以为凭据丢失 */
  hasPassword?: boolean
  hasPassphrase?: boolean
  /** 备注(单行文字,最多 10 字). 旧数据可能没有此字段,但 tags 数组可作为兜底 */
  note?: string
  /** 目标环境标记('test' | 'prod'). 驱动生产环境高风险命令的审计与审批强化. 旧数据默认 'test' */
  environment?: 'test' | 'prod'
  /** S4-03：主进程保存时附上来的凭据告警 —— 'encryption-unavailable' 表示系统密钥环
   *  不可用，本次提交的密码/passphrase 没有落盘（不落明文）。渲染层据此提示用户，
   *  否则「保存成功」是假信号，下次连接才暴露成认证失败。 */
  credentialNotice?: 'encryption-unavailable'
  tags: string[]
  readyTimeout?: number       // 毫秒
  keepaliveInterval?: number  // 毫秒
  lastConnectedAt?: number
  createdAt: number
  updatedAt: number
}

interface SSHTestResult {
  ok: boolean
  message: string
  latencyMs: number
}

/** 弹窗编辑器读取文件返回结构 */
interface EditorFileContent {
  content: string
  size: number
  readOnly: boolean
}

/**
 * SSH 重连状态变化事件（主进程 → 渲染层，channel: ssh:reconnect）。
 * 主进程类型定义见 electron/ssh-manager.ts（两个 tsconfig 程序互不包含，
 * 此处为渲染层副本）
 */
interface SshReconnectStatus {
  id: string
  /** reconnecting=首轮排程（断开后自动/手动点击后）| retry=上一次失败后的重试排程 | success=重连成功 | gave-up=放弃 */
  phase: 'reconnecting' | 'retry' | 'success' | 'gave-up'
  /** 当前（reconnecting/retry）或最后一次（success/gave-up）尝试序号，1 起；gave-up + cancelled 时为 0（哨兵值） */
  attempt: number
  maxAttempts: number
  /** reconnecting/retry 阶段：下一次尝试前的退避等待（ms） */
  delayMs?: number
  /** gave-up 阶段：exhausted=RECONNECT_MAX_ATTEMPTS 次均失败 | config-missing=连接配置已删除 | cancelled=重连被取消（如断开后连接配置被删除，tab 仍在）| not-retryable=确定性失败，重连窗口直接收摊（S4-04） */
  reason?: 'exhausted' | 'config-missing' | 'cancelled' | 'not-retryable'
  /** gave-up 阶段带上最后一次失败的分类结果，UI 得以说清是口令错还是主机密钥变了（S0-18） */
  errorCode?: string
  errorMessage?: string
}

/** SSH 隧道规则（主进程定义见 electron/tunnel-store.ts，此处为渲染层副本） */
interface TunnelRule {
  id: string
  /** 展示名，可空（UI 显示自动名「L 8080 → db:5432」式） */
  name: string
  /** 引用 SSHConnectionRecord.id（弱引用：连接被删 → 启动报 config-missing） */
  connectionId: string
  /** 数据面：缺省视为 'ssh'（存量规则零迁移）；'gateway' 时走下面的网关会话 */
  via?: 'ssh' | 'gateway'
  /** 引用 GatewayRecord.id（仅 via='gateway'） */
  gatewayId?: string
  type: 'local' | 'remote' | 'dynamic'
  /** 默认 127.0.0.1，仅 local 可自定义；dynamic 强制 127.0.0.1 */
  listenHost?: string
  listenPort?: number
  /** 默认 127.0.0.1，仅 remote；0.0.0.0 需 UI 显式确认风险 */
  remoteBindHost?: string
  bindPort?: number
  dstHost?: string
  dstPort?: number
  autoStart: boolean
  createdAt: number
  updatedAt: number
}

/** 隧道运行状态（主进程 → 渲染层推送，channel: tunnel:status；不持久化） */
interface TunnelStatus {
  id: string
  state: 'starting' | 'active' | 'error' | 'stopped'
  /** error 时给人看的失败原因 */
  message?: string
  connections?: number
  startedAt?: number
}

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
  checkedAt: number
}

/** TLS 接入网关记录（主进程定义见 electron/gateway-store.ts；口令不出主进程） */
interface GatewayRecord {
  id: string
  name: string
  host: string
  port: number
  /** CA（PEM），缺省时信任判定完全交给下面的指纹 */
  caPem?: string
  /** 已确认的服务端 SPKI 指纹（sha256/…），缺省 = 首次连接走 TOFU */
  trustedSpki?: string
  user: string
  /** 仅保存时上行；下行副本里永远没有这个字段 */
  password?: string
  createdAt: number
  updatedAt: number
}

/** 网关会话状态（主进程 → 渲染层推送，channel: gateway:status；不持久化） */
interface GatewayStatus {
  id: string
  name: string
  host: string
  port: number
  state: 'idle' | 'connecting' | 'online' | 'reconnecting' | 'error'
  message?: string
  /** 当前活动转发通道数 */
  channels?: number
  /** 网关协商的心跳间隔（秒），仅在会话就绪后有值 */
  heartbeatSec?: number
  role?: string
  sessionId?: string
  spki?: string
  sinceAt?: number
  /** 第几次重连（0 = 未断过） */
  attempt?: number
}

/** 首次见到网关证书（TOFU），UI 确认后调 gatewayTrustHostKey 落库 */
interface GatewayHostKeyEvent {
  id: string
  name: string
  host: string
  port: number
  spki: string
}

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
  ghaccelSaveLists: (
    proxies: import('../shared/github-accel-core').AccelProxy[],
    webMirrors: import('../shared/github-accel-core').AccelWebMirror[],
  ) => Promise<import('../shared/github-accel-core').GithubAccelConfig>
  ghaccelProbeAll: () => Promise<Array<{ id: string; name: string; target: string; ok: boolean; latencyMs: number | null; status?: number; error?: string }>>
  ghaccelGitStatus: () => Promise<{
    available: boolean
    entries: Array<{ key: string; subsection: string; value: string }>
    active: boolean
    activeSubsection?: string
    managed: boolean
    backup: string | null
    message?: string
  }>
  ghaccelGitEnable: (prefix: string) => Promise<ReturnType<ElectronAPI['ghaccelGitStatus']>>
  ghaccelGitDisable: () => Promise<ReturnType<ElectronAPI['ghaccelGitStatus']>>
  /** 端到端自检（只读）：git / 代理 / SSH 443 / hosts 四项 */
  ghaccelSelfCheck: () => Promise<GhaccelSelfCheckRow[]>
  /** 最近一次巡检结果（标题栏挂载时拉取，不必等下一次推送） */
  ghaccelHealthStatus: () => Promise<GhaccelHealthStatus>
  ghaccelSsh443Status: () => Promise<{ applied: boolean; path: string; exists: boolean; message?: string }>
  ghaccelSsh443Apply: () => Promise<{ applied: boolean; path: string; exists: boolean }>
  ghaccelSsh443Remove: () => Promise<{ applied: boolean; path: string; exists: boolean }>
  ghaccelOpenExternal: (url: string) => Promise<void>
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
  ghaccelHostsPick: () => Promise<Array<{ domain: string; ip: string | null; latencyMs: number | null; candidates: number; error?: string }>>
  ghaccelHostsApply: (entries?: { domain: string; ip: string }[]) => Promise<ReturnType<ElectronAPI['ghaccelHostsStatus']>>
  ghaccelHostsRemove: () => Promise<ReturnType<ElectronAPI['ghaccelHostsStatus']>>
  ghaccelSaveOptions: (patch: { lastProxyPrefix?: string }) =>
    Promise<import('../shared/github-accel-core').GithubAccelConfig>
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
  sftpStat: (sshSessionId: string, remotePath: string) => Promise<{ size: number; isDirectory: boolean; modified: number }>
  sftpRename: (sshSessionId: string, oldPath: string, newPath: string) => Promise<void>
  sftpDelete: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpUpload: (transferId: string, sshSessionId: string, localPath: string, remotePath: string) => Promise<void>
  sftpDownload: (transferId: string, sshSessionId: string, remotePath: string, localPath: string) => Promise<void>
  sftpCancel: (transferId: string) => Promise<void>
  sftpReadFile: (sshSessionId: string, remotePath: string) => Promise<EditorFileContent>
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
