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
interface QuickCommand {
  id: string
  name: string
  command: string
  color: string
  order: number
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
  /** 备注(单行文字,最多 10 字). 旧数据可能没有此字段,但 tags 数组可作为兜底 */
  note?: string
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

interface ElectronAPI {
  windowMinimize: () => Promise<void>
  windowMaximize: () => Promise<void>
  windowClose: () => Promise<void>
  onBackendStatus: (callback: (data: { status: string; port?: number; error?: string }) => void) => () => void
  sshIsEncryptionAvailable: () => Promise<boolean>
  getBackendPort: () => Promise<number>
  isBackendAlive: () => Promise<boolean>
  authIsEncryptionAvailable: () => Promise<boolean>
  authEncryptString: (plain: string) => Promise<string>
  authDecryptString: (encrypted: string) => Promise<string | null>
  authSetBackendToken: (token: string | null) => Promise<{ ok: boolean }>
  fsReadDir: (dirPath: string) => Promise<FileEntry[]>
  fsReadFile: (filePath: string) => Promise<string>
  fsWriteFile: (filePath: string, content: string) => Promise<void>
  fsReadFileForEditor: (filePath: string) => Promise<EditorFileContent>
  fsSelectFolder: () => Promise<string | null>
  fsSelectFile: () => Promise<string | null>
  fsSelectImage: () => Promise<{ fileName: string; mime: string; name: string } | null>
  fsLoadBackgroundImage: (fileName: string) => Promise<string>
  fsDeleteBackgroundImage: (fileName: string) => Promise<boolean>
  fsGetHomePath: () => Promise<string>
  fsMkdir: (dirPath: string) => Promise<void>
  fsRename: (oldPath: string, newPath: string) => Promise<void>
  fsTrash: (filePath: string) => Promise<void>
  fsWriteBinaryFile: (filePath: string, base64Data: string) => Promise<void>
  fsPathJoin: (...segments: string[]) => Promise<string>
  fsGetSkillsDir: () => Promise<string>
  fsRmRF: (targetPath: string) => Promise<void>
  fsListFilesRecursive: (dirPath: string) => Promise<string[]>
  fsListTextFiles: (dirPath: string) => Promise<string[]>
  fsExists: (filePath: string) => Promise<boolean>
  fsStat: (filePath: string) => Promise<{ isDirectory: boolean; isFile: boolean; size: number; modified: number }>
  fsResolveProgram: (name: string) => Promise<string | null>
  fsZipLocal: (sources: string[], localZipPath: string) => Promise<void>
  fsUnzipLocal: (localZipPath: string, localDestDir: string) => Promise<void>
  ptyCreate: (opts?: { shell?: string; args?: string[]; cwd?: string; cols?: number; rows?: number }) => Promise<{ id: string; shell: string }>
  ptyWrite: (id: string, data: string) => Promise<void>
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
  onSshData: (callback: (data: { id: string; data: string }) => void) => () => void
  onSshClose: (callback: (data: { id: string }) => void) => () => void
  sshListConnections: () => Promise<SSHConnectionRecord[]>
  sshSaveConnection: (conn: SSHConnectionRecord) => Promise<SSHConnectionRecord>
  sshDeleteConnection: (id: string) => Promise<void>
  sshDuplicateConnection: (id: string) => Promise<SSHConnectionRecord>
  sshTestConnection: (config: Omit<SSHConnectionRecord, 'tags' | 'createdAt' | 'updatedAt'>) => Promise<SSHTestResult>
  sshExportConnections: (ids: string[]) => Promise<string>
  sshImportConnections: (json: string) => Promise<{ imported: number; skipped: number }>
  sshMigrateLegacy: (legacy: any[]) => Promise<number>
  // Skills
  listSkills: (mode?: string) => Promise<any[]>
  createSkill: (skill: Record<string, any>) => Promise<any>
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
