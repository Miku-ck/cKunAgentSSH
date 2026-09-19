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

/** SSH 快捷命令 */

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
  /** gave-up 阶段：exhausted=3 次均失败 | config-missing=连接配置已删除 | cancelled=重连被取消（如断开后连接配置被删除，tab 仍在） */
  reason?: 'exhausted' | 'config-missing' | 'cancelled'
}

interface ElectronAPI {
  windowMinimize: () => Promise<void>
  windowMaximize: () => Promise<void>
  windowClose: () => Promise<void>
  onBackendStatus: (callback: (data: { status: string; port?: number; error?: string }) => void) => () => void
  getBackendPort: () => Promise<number>
  getBackendToken: () => Promise<string>
  fsReadDir: (dirPath: string) => Promise<FileEntry[]>
  fsReadFile: (filePath: string) => Promise<string>
  fsReadFileBase64: (filePath: string) => Promise<{ base64: string; size: number }>
  fsWriteFile: (filePath: string, content: string) => Promise<void>
  fsReadFileForEditor: (filePath: string) => Promise<EditorFileContent>
  fsSelectFolder: () => Promise<string | null>
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
  fsGetSkillsDir: () => Promise<string>
  fsRmRF: (targetPath: string) => Promise<void>
  fsListFilesRecursive: (dirPath: string) => Promise<string[]>
  fsExists: (filePath: string) => Promise<boolean>
  fsStat: (filePath: string) => Promise<{ isDirectory: boolean; isFile: boolean; size: number; modified: number }>
  fsResolveProgram: (name: string) => Promise<string | null>
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
  onSshData: (callback: (data: { id: string; data: string }) => void) => () => void
  // 重连修复：willReconnect=true 表示意外断开且主进程将自动重连 —
  // 渲染层保留 tab 等待重连（不 removeSession）；false/缺省走原关闭清理流程。
  // 用户主动断开（关 tab/断开按钮）不会发出本事件
  onSshClose: (callback: (data: { id: string; willReconnect?: boolean }) => void) => () => void
  // 重连修复：重连状态变化（自动/手动重连进度，驱动终端系统行与状态区显示）
  onSshReconnect: (callback: (data: SshReconnectStatus) => void) => () => void
  // s1 修复：主机密钥验证事件（首次连接通知 + 密钥变更警告）
  onHostKeyNew: (callback: (data: { hostKey: string; fingerprint: string }) => void) => () => void
  onHostKeyMismatch: (callback: (data: { hostKey: string; expected: string; got: string }) => void) => () => void
  sshListConnections: () => Promise<SSHConnectionRecord[]>
  sshSaveConnection: (conn: SSHConnectionRecord) => Promise<SSHConnectionRecord>
  sshDeleteConnection: (id: string) => Promise<void>
  sshDuplicateConnection: (id: string) => Promise<SSHConnectionRecord>
  sshTestConnection: (config: Omit<SSHConnectionRecord, 'tags' | 'createdAt' | 'updatedAt'>) => Promise<SSHTestResult>
  sshMigrateLegacy: (legacy: any[]) => Promise<number>
  // Skills
  listSkills: (mode?: string) => Promise<any[]>
  updateSkillEnabled: (id: string, enabled: boolean, mode?: string) => Promise<{ id: string; enabled: boolean }>
  deleteSkill: (id: string) => Promise<{ deleted: boolean }>
  syncBuiltinSkills: (skills: any[]) => Promise<{ upserted: number; orphans_removed: number; total: number }>
  // C-1：技能 ZIP 解压写入（主进程边界收容，防 zip-slip 越界写）
  skillsMkdir: (dirPath: string) => Promise<void>
  skillsWriteFile: (filePath: string, base64: string) => Promise<void>

  // SFTP
  sftpOpen: (sshSessionId: string) => Promise<void>
  sftpClose: (sshSessionId: string) => Promise<void>
  sftpList: (sshSessionId: string, remotePath: string) => Promise<SftpEntry[]>
  sftpMkdir: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpMkdirRecursive: (sshSessionId: string, remotePath: string) => Promise<void>
  sftpExists: (sshSessionId: string, remotePath: string) => Promise<boolean>
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
