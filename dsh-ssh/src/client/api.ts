/**
 * Browser-side client for the /api/ckun-ssh route family. Plain fetch +
 * WebSocket, same origin (or the DSH Desktop transport loopback hook).
 */
import { API, type HostEntry, type HostSummary, type RemoteDirEntry, type StateSnapshot, type TerminalServerFrame, type TestResult, type ExecResult, type TunnelInfo } from '../protocol.ts'

export class SshApiError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SshApiError'
  }
}

async function readJson<T>(response: Response): Promise<T> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new SshApiError(response.status === 404 ? 'SSH 插件未启用或主机未响应' : `HTTP ${response.status}: 响应不是 JSON`)
  }
  if (!response.ok) {
    const message = typeof body === 'object' && body !== null && typeof (body as { error?: unknown }).error === 'string'
      ? (body as { error: string }).error
      : `HTTP ${response.status}`
    throw new SshApiError(message)
  }
  return body as T
}

function get(path: string): Promise<Response> {
  return fetch(path, { credentials: 'same-origin' })
}

function post(path: string, payload?: unknown): Promise<Response> {
  return fetch(path, {
    credentials: 'same-origin',
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  })
}

/** The SSH API surface the panel components use. */
export interface SshApi {
  listHosts(query?: string): Promise<HostSummary[]>
  saveHost(host: Partial<HostEntry>): Promise<HostSummary>
  deleteHost(alias: string): Promise<boolean>
  testHost(alias: string, pin: boolean): Promise<TestResult>
  exec(alias: string, command: string, timeoutMs?: number): Promise<ExecResult>
  ls(alias: string, path: string): Promise<{ path: string; entries: RemoteDirEntry[] }>
  downloadUrl(alias: string, path: string): string
  upload(alias: string, remotePath: string, name: string, data: Blob): Promise<{ ok: boolean; bytes: number }>
  tunnel(action: string, payload?: Record<string, unknown>): Promise<{ ok: boolean; tunnel?: TunnelInfo; tunnels?: TunnelInfo[] }>
  state(): Promise<StateSnapshot>
}

export function createSshApi(): SshApi {
  return {
    async listHosts(query) {
      const q = query ? `?q=${encodeURIComponent(query)}` : ''
      const body = await readJson<{ hosts: HostSummary[] }>(await get(API.hosts + q))
      return body.hosts
    },
    async saveHost(host) {
      const body = await readJson<{ ok: boolean; host?: HostSummary }>(await post(API.hosts, host))
      if (!body.ok || !body.host) throw new SshApiError('保存失败')
      return body.host
    },
    async deleteHost(alias) {
      const body = await readJson<{ ok: boolean }>(await fetch(API.hosts + `?alias=${encodeURIComponent(alias)}`, { method: 'DELETE', credentials: 'same-origin' }))
      return body.ok
    },
    async testHost(alias, pin) {
      return readJson<TestResult>(await post(API.test, { alias, pin }))
    },
    async exec(alias, command, timeoutMs = 60000) {
      return readJson<ExecResult>(await post(API.exec, { alias, command, timeoutMs }))
    },
    async ls(alias, path) {
      return readJson<{ path: string; entries: RemoteDirEntry[] }>(
        await get(`${API.ls}?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(path)}`),
      )
    },
    downloadUrl(alias, path) {
      return `${API.download}?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(path)}`
    },
    async upload(alias, remotePath, name, data) {
      const url = `${API.upload}?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(remotePath)}&name=${encodeURIComponent(name)}`
      const response = await fetch(url, { method: 'POST', credentials: 'same-origin', body: data })
      return readJson<{ ok: boolean; bytes: number }>(response)
    },
    async tunnel(action, payload) {
      return readJson<{ ok: boolean; tunnel?: TunnelInfo; tunnels?: TunnelInfo[] }>(await post(API.tunnel, { action, ...payload }))
    },
    async state() {
      return readJson<StateSnapshot>(await get(API.state))
    },
  }
}

/** Build the terminal WebSocket URL (Desktop transport hook aware). */
function terminalUrl(alias: string): string {
  const hook = (window as unknown as { __DSH_TRANSPORT__?: { streamBaseUrl?: string } }).__DSH_TRANSPORT__
  const base = typeof hook?.streamBaseUrl === 'string' && hook.streamBaseUrl !== '' ? hook.streamBaseUrl : window.location.origin
  const url = new URL(API.terminal, base)
  url.searchParams.set('alias', alias)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

export interface TerminalSocket {
  send(frame: { type: 'input' | 'resize' | 'close'; data?: string; cols?: number; rows?: number }): void
  close(): void
}

/** Open one terminal WebSocket; frames arrive via onFrame. */
export function openTerminal(alias: string, onFrame: (frame: TerminalServerFrame) => void, onClose: () => void): TerminalSocket {
  const socket = new WebSocket(terminalUrl(alias))
  socket.onmessage = (event: MessageEvent<string>) => {
    try {
      onFrame(JSON.parse(event.data) as TerminalServerFrame)
    } catch {
      /* ignore malformed frames */
    }
  }
  socket.onclose = () => onClose()
  socket.onerror = () => onClose()
  return {
    send(frame) {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame))
    },
    close() {
      try {
        socket.close()
      } catch {
        /* ignore */
      }
    },
  }
}
