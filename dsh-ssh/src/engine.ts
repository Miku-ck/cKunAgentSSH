import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import net from 'node:net'
import { dirname } from 'node:path'
import ssh2mod from 'ssh2'
import type { Client as ClientType, ConnectConfig, SFTPWrapper } from 'ssh2'

// ssh2 is CJS; its named ESM re-exports are unreliable, so destructure the default.
const { Client } = ssh2mod as unknown as { Client: new () => ClientType }
import { KnownHosts } from './knownhosts.ts'
import { expandHome } from './dsh-home.ts'
import type { HostStore } from './store.ts'
import type { EnvProfile, ExecResult, HostEntry, HostSummary, RemoteDirEntry, StateSnapshot, TestResult, TunnelInfo, TunnelState } from './protocol.ts'
import { fingerprint } from './knownhosts.ts'

interface PooledConn {
  alias: string
  client: ClientType
  state: 'connecting' | 'open' | 'closed'
  profile?: EnvProfile
}

/** An ssh error that carries the fingerprint of the presented host key. */
export interface SshKeyError extends Error {
  fingerprint?: string
}

interface LiveTunnel {
  info: TunnelInfo
  close: () => void
}

export interface EngineOptions {
  maxOutputBytes: () => number
  keepaliveMs: () => number
}

/**
 * The SSH engine: a per-host persistent ssh2 connection pool plus exec / PTY
 * shell / SFTP / tunnel operations, with the plugin's host-key pin policy
 * enforced on every connect.
 */
export class SshEngine {
  private readonly pool = new Map<string, PooledConn>()
  private readonly tunnels = new Map<string, LiveTunnel>()
  private readonly knownHosts: KnownHosts
  private disposed = false

  private readonly store: HostStore
  private readonly opts: EngineOptions

  constructor(store: HostStore, opts: EngineOptions) {
    this.store = store
    this.opts = opts
    this.knownHosts = new KnownHosts()
  }

  /** Project the registry onto the safe wire shape. */
  listHosts(query?: string): HostSummary[] {
    const q = query?.toLowerCase()
    return this.store
      .list()
      .filter((h) => !q || [h.alias, h.host, h.user, h.description ?? '', h.tags.join(' ')].join(' ').toLowerCase().includes(q))
      .map((h) => this.summarize(h))
  }

  private summarize(h: HostEntry): HostSummary {
    return {
      alias: h.alias,
      host: h.host,
      port: h.port,
      user: h.user,
      auth: h.auth,
      keyPath: h.keyPath,
      environment: h.environment,
      tags: h.tags,
      description: h.description,
      pinned: this.knownHosts.pinned(h.host, h.port || 22),
      connected: this.pool.get(h.alias)?.state === 'open',
      createdAt: h.createdAt,
      updatedAt: h.updatedAt,
    }
  }

  stateSnapshot(): StateSnapshot {
    const connections: StateSnapshot['connections'] = {}
    for (const [alias, conn] of this.pool) connections[alias] = conn.state
    return { connections, tunnels: [...this.tunnels.values()].map((t) => t.info) }
  }

  /** True while the transport is usable (ssh2 keeps the socket on `_sock`). */
  private isAlive(client: ClientType): boolean {
    const sock = (client as unknown as { _sock?: { destroyed?: boolean } })._sock
    return sock !== undefined && sock.destroyed !== true
  }

  /**
   * Close one pooled connection. The next connect() re-handshakes and
   * re-verifies the host key. GUI "断开连接 / 重新校验" uses this.
   */
  disconnect(alias: string): void {
    const entry = this.pool.get(alias)
    if (!entry) return
    this.pool.delete(alias)
    entry.state = 'closed'
    try {
      entry.client.destroy()
    } catch {
      /* ignore */
    }
  }

  /**
   * Get (or lazily create) the pooled connection for a host.
   * `allowTofu` permits pinning a first-seen host key — the GUI pin flow passes
   * true; agent-originated connects must pass false (fail closed).
   */
  async connect(alias: string, allowTofu: boolean): Promise<ClientType> {
    const existing = this.pool.get(alias)
    // Never hand out a dead transport: between the socket dying and the
    // 'close' event landing, state may still read 'open'.
    if (existing && existing.state === 'open' && this.isAlive(existing.client)) return existing.client
    if (existing) this.pool.delete(alias)
    const host = await this.store.getWithSecrets(alias)
    if (!host) throw new Error(`unknown host: ${alias}（先用 ssh_list 查看已配置主机）`)
    if (!allowTofu && !this.knownHosts.pinned(host.host, host.port || 22)) {
      throw new Error(
        `host key for ${alias} (${host.host}:${host.port || 22}) is not pinned yet. ` +
          `Pin it first via the SSH panel (新增主机/测试连接 -> 信任指纹), or re-run with an explicit user decision.`,
      )
    }
    const client = new Client()
    const entry: PooledConn = { alias, client, state: 'connecting' }
    this.pool.set(alias, entry)
    try {
      await this.openClient(client, host, allowTofu)
      entry.state = 'open'
      client.on('close', () => {
        const cur = this.pool.get(alias)
        if (cur === entry) {
          entry.state = 'closed'
          this.pool.delete(alias)
        }
      })
      client.on('error', () => {
        /* surfaced through the openClient promise; pool entry is dropped there */
      })
      return client
    } catch (err) {
      this.pool.delete(alias)
      try {
        client.end()
      } catch {
        /* ignore */
      }
      throw err
    }
  }

  /**
   * Complete the key exchange with an UNPINNED host just to learn its key,
   * then refuse. Key exchange precedes authentication, so no credentials are
   * needed. This is what feeds the GUI "trust fingerprint" dialog.
   */
  private captureHostKey(host: HostEntry): Promise<{ fingerprint: string }> {
    const port = host.port || 22
    return new Promise((resolve, reject) => {
      const client = new Client()
      let fp: string | undefined
      let settled = false
      const cfg: ConnectConfig = {
        host: host.host,
        port,
        username: host.user,
        readyTimeout: 15000,
        hostVerifier: (raw: Buffer, cb: (ok: boolean) => void) => {
          fp = fingerprint(raw)
          return cb(false)
        },
      }
      client.on('error', (err: Error) => {
        if (settled) return
        settled = true
        try {
          client.end()
        } catch {
          /* ignore */
        }
        if (fp) resolve({ fingerprint: fp })
        else reject(new Error(`ssh error: ${err.message}`))
      })
      client.on('ready', () => {
        if (settled) return
        settled = true
        if (fp) resolve({ fingerprint: fp })
        else reject(new Error('host key not presented'))
      })
      client.connect(cfg)
    })
  }

  private openClient(client: ClientType, host: HostEntry, allowTofu: boolean): Promise<void> {
    const port = host.port || 22
    return new Promise((resolve, reject) => {
      /** Fingerprint of the key the server just presented (for error enrichment). */
      let offeredFp: string | undefined
      const cfg: ConnectConfig = {
        host: host.host,
        port,
        username: host.user,
        keepaliveInterval: Math.max(5000, this.opts.keepaliveMs()),
        keepaliveCountMax: 4,
        readyTimeout: 15000,
        hostVerifier: (raw: Buffer, cb: (ok: boolean) => void) => {
          const v = this.knownHosts.verify(host.host, port, raw)
          if (v.status === 'matched') return cb(true)
          if (v.status === 'new') {
            offeredFp = v.fingerprint
            if (allowTofu) {
              this.knownHosts.pin(host.host, port, raw)
              return cb(true)
            }
            return cb(false)
          }
          // mismatch: remember the presented key so the GUI can offer a re-pin
          offeredFp = v.actual
          return cb(false)
        },
      }
      if (host.auth === 'key') {
        const keyPath = expandHome(host.keyPath || '~/.ssh/id_ed25519')
        try {
          cfg.privateKey = readFileSync(keyPath, 'utf8')
        } catch (e) {
          return reject(new Error(`cannot read key file ${keyPath}: ${(e as Error).message}`))
        }
        if (host.passphrase) cfg.passphrase = host.passphrase
      } else if (host.auth === 'password') {
        cfg.password = host.password ?? ''
      } else {
        const sock = process.env.SSH_AUTH_SOCK
        if (!sock) return reject(new Error('system ssh-agent unavailable (SSH_AUTH_SOCK is not set on this machine)'))
        cfg.agent = sock
      }
      let settled = false
      const fail = (err: Error) => {
        if (!settled) {
          settled = true
          reject(err)
        }
      }
      client.on('ready', () => {
        if (!settled) {
          settled = true
          resolve()
        }
      })
      client.on('error', (err: Error) => {
        const e = new Error(
          /verification failed/i.test(err.message)
            ? `host key rejected: ${err.message}（主机密钥与固定值不符——可能是密钥轮换，也可能存在中间人；请在 SSH 面板测试连接核对指纹）`
            : `ssh error: ${err.message}`,
        )
        if (offeredFp) {
          const ke: SshKeyError = e
          ke.fingerprint = offeredFp
        }
        fail(e)
      })
      client.on('keyboard-interactive', (_name, _instructions, _lang, _prompts, finish) => {
        fail(new Error('interactive authentication is not supported; use password or key auth'))
        finish([])
      })
      client.connect(cfg)
    })
  }

  /**
   * GUI "test connection":
   *  - pinned host (or pin=true): real connect, TOFU pins first-seen keys,
   *    then an environment profile is collected;
   *  - unpinned + pin=false: capture the presented key via a refused handshake
   *    and hand its fingerprint to the trust dialog (the only TOFU entry point).
   */
  async testHost(alias: string, pin: boolean): Promise<TestResult> {
    const host = this.store.get(alias)
    if (!host) return { ok: false, error: `unknown host: ${alias}` }
    const port = host.port || 22
    const alreadyPinned = this.knownHosts.pinned(host.host, port)
    try {
      if (!pin && !alreadyPinned) {
        const { fingerprint: fp } = await this.captureHostKey(host)
        return {
          ok: false,
          error: `主机密钥未固定（${host.host}:${port}）。核对下方指纹后信任。`,
          needsPin: true,
          fingerprint: fp,
        }
      }
      await this.connect(alias, pin)
      const profile = await this.profile(alias)
      return { ok: true, profile, fingerprint: this.knownHosts.fingerprintOf(host.host, port) }
    } catch (err) {
      const e = err as SshKeyError
      const isKeyRejection = /verification failed/i.test(e.message)
      return {
        ok: false,
        error: e.message,
        // a rejected key (unpinned or mismatched) is re-pinnable from the dialog
        needsPin: !pin && (isKeyRejection || /not pinned/i.test(e.message)),
        fingerprint: e.fingerprint ?? (pin ? this.knownHosts.fingerprintOf(host.host, port) : undefined),
      }
    }
  }

  /** Run one non-interactive command on the remote host. */
  async exec(alias: string, command: string, timeoutMs = 60000, signal?: AbortSignal): Promise<ExecResult> {
    const t0 = Date.now()
    const base: ExecResult = { success: false, exitCode: null, timedOut: false, stdout: '', stderr: '', durationMs: 0 }
    const limit = Math.min(Math.max(1000, timeoutMs), 300000)
    return new Promise<ExecResult>((resolve) => {
      let done = false
      let stream: import('ssh2').ClientChannel | undefined
      let timer: NodeJS.Timeout | undefined
      const onAbort = () => {
        try {
          stream?.close()
        } catch {
          /* ignore */
        }
        finish({ error: 'cancelled by caller' })
      }
      const finish = (r: Partial<ExecResult>) => {
        if (done) return
        done = true
        signal?.removeEventListener('abort', onAbort)
        if (timer) clearTimeout(timer)
        resolve({ ...base, ...r, durationMs: Date.now() - t0 })
      }
      signal?.addEventListener('abort', onAbort, { once: true })
      timer = setTimeout(() => {
        try {
          stream?.close()
        } catch {
          /* ignore */
        }
        finish({ timedOut: true, error: `timed out after ${limit}ms` })
      }, limit)
      this.connect(alias, false)
        .then((client) => {
          client.exec(command, (err, ch) => {
            if (err) return finish({ error: err.message })
            stream = ch
            let stdout = ''
            let stderr = ''
            let truncated = false
            const max = this.opts.maxOutputBytes()
            const cap = (acc: string, chunk: string): string => {
              const next = acc + chunk
              if (Buffer.byteLength(next) > max) {
                truncated = true
                return next.slice(0, max) + `\n...[truncated at ${max} bytes]`
              }
              return next
            }
            ch.on('data', (d: Buffer) => {
              if (!truncated) stdout = cap(stdout, d.toString('utf8'))
            })
            ch.stderr.on('data', (d: Buffer) => {
              if (!truncated) stderr = cap(stderr, d.toString('utf8'))
            })
            ch.on('close', (code: number | null) =>
              finish({ success: code === 0, exitCode: code, stdout, stderr, truncated: truncated || undefined }),
            )
            ch.on('error', (e: Error) => finish({ error: e.message }))
          })
        })
        .catch((e: Error) => finish({ error: e.message }))
    })
  }

  /** Target-machine environment profile (distro/init/pkg manager/kernel/uptime). */
  async profile(alias: string): Promise<EnvProfile> {
    const probe =
      'sh -c \'echo "U=$(whoami)"; echo "H=$(hostname 2>/dev/null || uname -n)"; ' +
      '[ -f /etc/os-release ] && . /etc/os-release && echo "D=$PRETTY_NAME"; ' +
      'echo "I=$(ps -p 1 -o comm= 2>/dev/null)"; ' +
      'for pm in dnf yum apt-get apt pacman zypper apk; do command -v $pm >/dev/null 2>&1 && { echo "P=$pm"; break; }; done; ' +
      'echo "K=$(uname -sr 2>/dev/null)"; echo "UP=$(uptime -p 2>/dev/null || uptime)"; echo "HD=$(cd ~ 2>/dev/null && pwd)"\' || true'
    const r = await this.exec(alias, probe, 15000)
    // A transport-level failure must not masquerade as an (empty) profile.
    if (r.error) throw new Error(r.error)
    const get = (k: string): string | undefined => {
      const m = r.stdout.match(new RegExp(`^${k}=(.*)$`, 'm'))
      return m ? m[1].trim() || undefined : undefined
    }
    return {
      alias,
      hostname: get('H') ?? '',
      user: get('U') ?? '',
      homeDir: get('HD'),
      distro: get('D'),
      init: get('I'),
      pkgManager: get('P'),
      kernel: get('K'),
      uptime: get('UP'),
    }
  }

  private async sftp(alias: string): Promise<SFTPWrapper> {
    const client = await this.connect(alias, false)
    return new Promise((resolve, reject) => {
      client.sftp((err, sftp) => (err ? reject(new Error(`sftp: ${err.message}`)) : resolve(sftp)))
    })
  }

  private async sftpMkdirP(sftp: SFTPWrapper, dir: string): Promise<void> {
    const parts: string[] = []
    let acc = ''
    for (const part of dir.split('/').filter(Boolean)) {
      acc += (acc ? '/' : '') + part
      parts.push(acc)
    }
    for (const p of parts) {
      await new Promise<void>((resolve) => {
        sftp.mkdir(p, (err) => {
          if (err && !/exist/i.test(err.message)) return resolve() // only missing is fine
          resolve()
        })
      })
    }
  }

  /** List one remote directory. */
  async ls(alias: string, remotePath: string): Promise<RemoteDirEntry[]> {
    const sftp = await this.sftp(alias)
    return new Promise((resolve, reject) => {
      sftp.readdir(remotePath, (err, files) => {
        if (err) return reject(new Error(`ls ${remotePath}: ${err.message}`))
        resolve(
          files.map((f) => {
            const type: RemoteDirEntry['type'] = f.attrs.isDirectory()
              ? 'dir'
              : f.attrs.isSymbolicLink()
                ? 'link'
                : 'file'
            return {
              name: f.filename,
              path: remotePath === '/' ? `/${f.filename}` : `${remotePath.replace(/\/$/, '')}/${f.filename}`,
              type,
              size: f.attrs.size ?? 0,
              mtime: (f.attrs.mtime ?? 0) * 1000,
              mode: f.attrs.mode !== undefined ? (0o7777 & f.attrs.mode).toString(8) : '',
            }
          }),
        )
      })
    })
  }

  /** Copy a remote file to a local (host-side) path. Returns bytes written. */
  async download(alias: string, remotePath: string, localPath: string): Promise<number> {
    const sftp = await this.sftp(alias)
    mkdirSync(dirname(localPath), { recursive: true })
    await new Promise<void>((resolve, reject) => {
      sftp.fastGet(remotePath, localPath, (err) => (err ? reject(new Error(`download ${remotePath}: ${err.message}`)) : resolve()))
    })
    return statSync(localPath).size
  }

  /** Copy a local (host-side) file to a remote path (parents created). */
  async upload(alias: string, localPath: string, remotePath: string): Promise<number> {
    const sftp = await this.sftp(alias)
    const parent = remotePath.split('/').slice(0, -1).join('/')
    if (parent) await this.sftpMkdirP(sftp, parent)
    await new Promise<void>((resolve, reject) => {
      sftp.fastPut(localPath, remotePath, (err) => (err ? reject(new Error(`upload ${remotePath}: ${err.message}`)) : resolve()))
    })
    return statSync(localPath).size
  }

  /**
   * Start a local port-forward (ssh -L semantics): 127.0.0.1:localPort ->
   * remoteHost:remotePort through the pooled connection. ssh2 has no built-in
   * local forward, so we listen locally and pipe each connection through a
   * direct-tcpip channel (client.forwardOut).
   */
  async startTunnel(alias: string, localPort: number, remoteHost: string, remotePort: number): Promise<TunnelInfo> {
    const client = await this.connect(alias, false)
    const server = net.createServer((socket) => {
      let fwd: import('ssh2').ClientChannel | undefined
      socket.once('error', () => fwd?.destroy())
      client.forwardOut('127.0.0.1', 0, remoteHost, remotePort, (err, stream) => {
        if (err) {
          socket.destroy()
          return
        }
        fwd = stream
        stream.once('error', () => socket.destroy())
        socket.once('close', () => stream.destroy())
        socket.pipe(stream).pipe(socket)
      })
    })
    await new Promise<void>((resolve, reject) => {
      server.once('error', (err) => reject(new Error(`listen 127.0.0.1:${localPort}: ${err.message}`)))
      server.listen(localPort, '127.0.0.1', () => resolve())
    })
    const addr = server.address()
    const boundPort = typeof addr === 'object' && addr !== null ? addr.port : localPort
    const info: TunnelInfo = { id: randomUUID().slice(0, 8), alias, localPort: boundPort, remoteHost, remotePort, state: 'open' }
    this.tunnels.set(info.id, {
      info,
      close: () => {
        info.state = 'closing'
        server.close()
        ;(server as net.Server & { closeAllConnections?: () => void }).closeAllConnections?.()
        info.state = 'closed'
        this.tunnels.delete(info.id)
      },
    })
    return info
  }

  stopTunnel(id: string): TunnelInfo | undefined {
    const t = this.tunnels.get(id)
    if (!t) return undefined
    t.close()
    return t.info
  }

  stopAllTunnels(): void {
    for (const t of [...this.tunnels.values()]) t.close()
  }

  /** Release every connection and tunnel (host shutdown). */
  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.stopAllTunnels()
    for (const conn of this.pool.values()) {
      try {
        conn.client.destroy()
      } catch {
        /* ignore */
      }
    }
    this.pool.clear()
  }
}
