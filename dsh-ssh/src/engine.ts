import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, basename } from 'node:path'
import { Client, SFTPWrapper } from 'ssh2'
import type { ConnectConfig } from 'ssh2'
import { KnownHosts } from './knownhosts.ts'
import { expandHome } from './dsh-home.ts'
import type { HostStore } from './store.ts'
import type { EnvProfile, ExecResult, HostEntry, HostSummary, RemoteDirEntry, StateSnapshot, TestResult, TunnelInfo, TunnelState } from './protocol.ts'

interface PooledConn {
  alias: string
  client: Client
  state: 'connecting' | 'open' | 'closed'
  profile?: EnvProfile
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

  constructor(
    private readonly store: HostStore,
    private readonly opts: EngineOptions,
  ) {
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

  /**
   * Get (or lazily create) the pooled connection for a host.
   * `allowTofu` permits pinning a first-seen host key — the GUI pin flow passes
   * true; agent-originated connects must pass false (fail closed).
   */
  async connect(alias: string, allowTofu: boolean): Promise<Client> {
    const existing = this.pool.get(alias)
    if (existing && existing.state === 'open') return existing.client
    if (existing) this.pool.delete(alias)
    const host = this.store.get(alias)
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

  private openClient(client: Client, host: HostEntry, allowTofu: boolean): Promise<void> {
    const port = host.port || 22
    return new Promise((resolve, reject) => {
      const cfg: ConnectConfig = {
        host: host.host,
        port,
        username: host.user,
        keepaliveInterval: Math.max(5000, this.opts.keepaliveMs()),
        keepaliveCountMax: 4,
        readyTimeout: 15000,
        hostVerifier: (raw, cb) => {
          const v = this.knownHosts.verify(host.host, port, raw)
          if (v.status === 'matched') return cb(true)
          if (v.status === 'new') {
            if (allowTofu) {
              this.knownHosts.pin(host.host, port, raw)
              return cb(true)
            }
            return cb(false)
          }
          return cb(false)
        },
      }
      if (host.auth === 'key') {
        const keyPath = expandHome(host.keyPath || '~/.ssh/id_ed25519')
        try {
          cfg.privateKeys = [readFileSync(keyPath, 'utf8')]
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
      client.on('error', (err: Error) => fail(new Error(`ssh error: ${err.message}`)))
      client.on('verification-required', (err: Error) =>
        fail(new Error(`host key rejected: ${err.message}（密钥未固定或与固定指纹不符；请在 SSH 面板中重新测试连接）`)),
      )
      client.on('keyboard-interactive', (_n, _i, _l, _p, cb) => {
        fail(new Error('interactive authentication is not supported; use password or key auth'))
        cb([])
      })
      client.connect(cfg)
    })
  }

  /** GUI "test connection": connect (optionally pinning) then collect the profile. */
  async testHost(alias: string, pin: boolean): Promise<TestResult> {
    const host = this.store.get(alias)
    if (!host) return { ok: false, error: `unknown host: ${alias}` }
    try {
      const client = await this.connect(alias, pin)
      const profile = await this.profile(alias, client)
      return { ok: true, profile, fingerprint: this.knownHosts.fingerprintOf(host.host, host.port || 22) }
    } catch (err) {
      const message = (err as Error).message
      const isPinIssue = message.includes('not pinned') || message.includes('verification') || message.includes('host key')
      return {
        ok: false,
        error: message,
        needsPin: pin ? false : isPinIssue,
        fingerprint: pin ? this.knownHosts.fingerprintOf(host.host, host.port || 22) : undefined,
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
          files.map((f) => ({
            name: f.name,
            path: remotePath === '/' ? `/${f.name}` : `${remotePath.replace(/\/$/, '')}/${f.name}`,
            type: f.attrs.type === 1 ? 'dir' : f.attrs.type === 2 ? 'link' : 'file',
            size: f.attrs.size ?? 0,
            mtime: (f.attrs.mtime ?? 0) * 1000,
            mode: f.attrs.mode !== undefined ? (0o7777 & f.attrs.mode).toString(8) : '',
          })),
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

  /** Start a local port-forward: 127.0.0.1:localPort -> remoteHost:remotePort through the host. */
  async startTunnel(alias: string, localPort: number, remoteHost: string, remotePort: number): Promise<TunnelInfo> {
    const client = await this.connect(alias, false)
    const info: TunnelInfo = { id: randomUUID().slice(0, 8), alias, localPort, remoteHost, remotePort, state: 'open' }
    await new Promise<void>((resolve, reject) => {
      client.forwardLocal(localPort, remoteHost, remotePort, (err) => (err ? reject(new Error(`forwardLocal :${localPort}: ${err.message}`)) : resolve()))
    })
    this.tunnels.set(info.id, {
      info,
      close: () => {
        info.state = 'closing'
        client.unforwardLocal(localPort, () => {
          info.state = 'closed'
          this.tunnels.delete(info.id)
        })
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
        conn.client.end()
      } catch {
        /* ignore */
      }
    }
    this.pool.clear()
  }
}
