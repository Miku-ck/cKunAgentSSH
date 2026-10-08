/**
 * Shared wire types for the @ckun/dsh-ssh host/client surface.
 * This module is bundled into BOTH halves — keep it dependency-free.
 */

export const API = {
  hosts: '/api/ckun-ssh/hosts',
  test: '/api/ckun-ssh/test',
  exec: '/api/ckun-ssh/exec',
  ls: '/api/ckun-ssh/ls',
  download: '/api/ckun-ssh/download',
  upload: '/api/ckun-ssh/upload',
  tunnel: '/api/ckun-ssh/tunnel',
  state: '/api/ckun-ssh/state',
  terminal: '/api/ckun-ssh/terminal',
} as const

export type SshAuthKind = 'password' | 'key' | 'agent'

/** One configured host (the store form; may carry secrets — never send as-is). */
export interface HostEntry {
  alias: string
  host: string
  port: number
  user: string
  auth: SshAuthKind
  keyPath?: string
  password?: string
  passphrase?: string
  /** Environment tag: 'prod' | 'staging' | 'dev' | custom. 'prod' is security-significant. */
  environment?: string
  tags: string[]
  description?: string
  createdAt: number
  updatedAt: number
}

/** Host projection safe for the wire (no secrets). */
export interface HostSummary {
  alias: string
  host: string
  port: number
  user: string
  auth: SshAuthKind
  environment?: string
  tags: string[]
  description?: string
  /** Whether the host key is pinned in this plugin's known-hosts store. */
  pinned: boolean
  /** Whether the connection pool currently holds an open channel. */
  connected: boolean
  createdAt: number
  updatedAt: number
}

/** Remote environment profile (target-machine fingerprint for the agent). */
export interface EnvProfile {
  alias: string
  hostname: string
  user: string
  homeDir?: string
  distro?: string
  init?: string
  pkgManager?: string
  kernel?: string
  uptime?: string
}

/** Outcome of one GUI "test connection" (optionally pinning a new host key). */
export interface TestResult {
  ok: boolean
  error?: string
  /** Fingerprint of the presented host key (always present when the key was offered). */
  fingerprint?: string
  /** True when the connection failed because the key is not pinned yet. */
  needsPin?: boolean
  profile?: EnvProfile
}

/** Outcome of one non-interactive remote command. */
export interface ExecResult {
  success: boolean
  exitCode: number | null
  timedOut: boolean
  stdout: string
  stderr: string
  durationMs: number
  truncated?: boolean
  error?: string
}

export type TunnelState = 'open' | 'closing' | 'closed'

export interface TunnelInfo {
  id: string
  alias: string
  localPort: number
  remoteHost: string
  remotePort: number
  state: TunnelState
}

export interface RemoteDirEntry {
  name: string
  path: string
  type: 'dir' | 'file' | 'link'
  size: number
  mtime: number
  mode: string
}

/** Live engine snapshot for the GUI status bar. */
export interface StateSnapshot {
  connections: Record<string, 'open' | 'connecting' | 'closed'>
  tunnels: TunnelInfo[]
}

/** Terminal WebSocket frames: server -> browser. */
export type TerminalServerFrame =
  | { type: 'ready'; sessionId: string; alias: string }
  | { type: 'output'; data: string }
  | { type: 'exit'; code: number | null; error?: string }

/** Terminal WebSocket frames: browser -> server. */
export type TerminalClientFrame =
  | { type: 'input'; data: string }
  | { type: 'resize'; cols: number; rows: number }
  | { type: 'close' }
