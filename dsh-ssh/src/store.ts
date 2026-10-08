import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { dataFile, expandHome } from './dsh-home.ts'
import type { HostEntry } from './protocol.ts'

const FILE = 'ckun-dsh-ssh.json'

interface StoreFile {
  version: 1
  hosts: HostEntry[]
}

/**
 * The host registry: one JSON file under the DSH home (~/.dsh/ckun-dsh-ssh.json),
 * written atomically with best-effort 0600. Passwords passphrases live here for
 * M1 (documented); moving them to the DSH credentials service is tracked work.
 */
export class HostStore {
  private readonly file: string
  private data: StoreFile

  constructor() {
    this.file = dataFile(FILE)
    this.data = this.load()
  }

  private load(): StoreFile {
    try {
      const raw = JSON.parse(readFileSync(this.file, 'utf8')) as Partial<StoreFile>
      return { version: 1, hosts: Array.isArray(raw.hosts) ? (raw.hosts as HostEntry[]) : [] }
    } catch {
      return { version: 1, hosts: [] }
    }
  }

  private save(): void {
    mkdirSync(dirname(this.file), { recursive: true })
    const tmp = this.file + '.tmp'
    writeFileSync(tmp, JSON.stringify(this.data, null, 2), { mode: 0o600 })
    try {
      chmodSync(tmp, 0o600)
    } catch {
      /* best effort (Windows) */
    }
    renameSync(tmp, this.file)
  }

  /** All hosts (alias-sorted). Secrets included — caller projects. */
  list(): HostEntry[] {
    return [...this.data.hosts].sort((a, b) => a.alias.localeCompare(b.alias))
  }

  get(alias: string): HostEntry | undefined {
    return this.data.hosts.find((h) => h.alias === alias)
  }

  /** Insert or update one host; blank secrets keep the stored values (edit flow). */
  upsert(input: Omit<HostEntry, 'createdAt' | 'updatedAt'>): HostEntry {
    const now = Date.now()
    const existing = this.get(input.alias)
    const next: HostEntry = { ...input, createdAt: existing?.createdAt ?? now, updatedAt: now }
    if (existing) {
      if (!next.password && existing.password) next.password = existing.password
      if (!next.passphrase && existing.passphrase) next.passphrase = existing.passphrase
      Object.assign(existing, next)
    } else {
      this.data.hosts.push(next)
    }
    this.save()
    return next
  }

  remove(alias: string): boolean {
    const before = this.data.hosts.length
    this.data.hosts = this.data.hosts.filter((h) => h.alias !== alias)
    const removed = this.data.hosts.length < before
    if (removed) this.save()
    return removed
  }

  /**
   * Import ~/.ssh/config Host blocks that are not already configured.
   * ProxyJump hosts get a tag; IdentityFile is mapped to key auth.
   */
  importSshConfig(): { imported: number; skipped: number } {
    const cfgPath = join(expandHome('~'), '.ssh', 'config')
    let text: string
    try {
      text = readFileSync(cfgPath, 'utf8')
    } catch {
      return { imported: 0, skipped: 0 }
    }
    /** One parsed ~/.ssh/config Host block (proxyJump has no HostEntry counterpart in M1). */
    interface SshConfigBlock extends Partial<Omit<HostEntry, 'tags'>> {
      tags?: string[]
      proxyJump?: string
    }
    let imported = 0
    let skipped = 0
    let current: SshConfigBlock | undefined
    const commit = () => {
      if (current?.host && current.alias) {
        if (this.get(current.alias)) {
          skipped++
          return
        }
        this.upsert({
          alias: current.alias,
          host: current.host,
          port: current.port ?? 22,
          user: current.user ?? 'root',
          auth: current.keyPath ? 'key' : 'password',
          keyPath: current.keyPath ? expandHome(current.keyPath) : undefined,
          environment: current.environment,
          tags: current.tags ?? [],
          description: `imported from ~/.ssh/config${current.proxyJump ? ` (jump: ${current.proxyJump})` : ''}`,
        })
        imported++
      }
      current = undefined
    }
    const re = /^\s*(\w[\w-]*)\s+(.+?)\s*$/
    for (const rawLine of text.split('\n')) {
      const line = rawLine.trim()
      if (line === '' || line.startsWith('#')) continue
      const m = line.match(re)
      if (!m) continue
      const [, key, value] = m
      if (key === 'Host') {
        commit()
        // skip wildcard/alias-less blocks
        if (value.includes('*') || value.includes('?')) continue
        current = { alias: value, host: value }
      } else if (current) {
        if (key === 'HostName' && !current.host) current.host = value
        else if (key === 'Port') current.port = Number(value)
        else if (key === 'User') current.user = value
        else if (key === 'IdentityFile') current.keyPath = value.split(/\s+/)[0]
        else if (key === 'ProxyJump') current.proxyJump = value
        else if (key === 'SetEnv' && value.startsWith('SSH_HOST_ENV=')) current.environment = value.slice('SSH_HOST_ENV='.length)
      }
    }
    commit()
    return { imported, skipped }
  }
}
