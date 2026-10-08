import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { dataFile, expandHome } from './dsh-home.ts'
import type { HostEntry } from './protocol.ts'
import { createCredentialVault, type CredentialVault, type HostSecrets } from './vault.ts'

const FILE = 'ckun-dsh-ssh.json'

interface StoreFile {
  version: 1
  hosts: HostEntry[]
}

/**
 * The host registry: one JSON file under the DSH home (~/.dsh/ckun-dsh-ssh.json),
 * written atomically with best-effort 0600. When the DSH credentials service is
 * available, passwords/passphrases live in credential records (`ckun-ssh/<id>`
 * grant records) and the JSON file carries no secrets; otherwise the secrets
 * fall back to this 0600 file (the M1 path, still supported).
 */
export class HostStore {
  private readonly file: string
  private readonly vault: CredentialVault
  private data: StoreFile

  constructor(vault?: CredentialVault) {
    this.file = dataFile(FILE)
    this.vault = vault ?? createCredentialVault(undefined)
    this.data = this.load()
  }

  private load(): StoreFile {
    let raw: string
    try {
      raw = readFileSync(this.file, 'utf8')
    } catch (err) {
      // A missing file is a fresh install. Any other read failure (EBUSY/EPERM from
      // Windows file locks or AV scanning) is NOT: returning an empty registry here
      // means the next upsert() rewrites the file from that empty state and the whole
      // registry — plus every pointer into a credential record — is gone. Same rule
      // the Electron gateway store documents for its own load.
      if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') return { version: 1, hosts: [] }
      throw err
    }
    // Corrupt JSON propagates for the same reason: an empty registry that gets saved
    // back over the file is indistinguishable from the user deleting every host.
    const parsed = JSON.parse(raw) as Partial<StoreFile>
    return { version: 1, hosts: Array.isArray(parsed.hosts) ? (parsed.hosts as HostEntry[]) : [] }
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

  /**
   * Entry with effective secrets resolved (credential vault first, legacy JSON
   * as fallback). Connection paths must use this; summaries can use get().
   */
  async getWithSecrets(alias: string): Promise<HostEntry | undefined> {
    const base = this.get(alias)
    if (!base) return undefined
    if (!this.vault.available) return base
    const secrets = await this.vault.read(alias)
    const merged: HostEntry = { ...base }
    if (secrets.password) merged.password = secrets.password
    if (secrets.passphrase) merged.passphrase = secrets.passphrase
    return merged
  }

  /** Insert or update one host; blank secrets keep the stored values (edit flow). */
  async upsert(input: Omit<HostEntry, 'createdAt' | 'updatedAt'>): Promise<HostEntry> {
    const now = Date.now()
    const existing = this.get(input.alias)
    const merged: HostSecrets = {
      password: input.password || existing?.password || undefined,
      passphrase: input.passphrase || existing?.passphrase || undefined,
    }
    const next: HostEntry = {
      ...input,
      password: undefined,
      passphrase: undefined,
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    }
    if (!this.vault.available) {
      next.password = merged.password
      next.passphrase = merged.passphrase
    }
    if (existing) {
      Object.assign(existing, next)
    } else {
      this.data.hosts.push(next)
    }
    this.save()
    if (this.vault.available) {
      if (merged.password || merged.passphrase) await this.vault.save(input.alias, merged)
      else await this.vault.remove(input.alias)
    }
    return { ...next, ...merged }
  }

  async remove(alias: string): Promise<boolean> {
    const before = this.data.hosts.length
    this.data.hosts = this.data.hosts.filter((h) => h.alias !== alias)
    const removed = this.data.hosts.length < before
    if (removed) this.save()
    if (removed && this.vault.available) {
      try {
        await this.vault.remove(alias)
      } catch {
        /* record cleanup is best effort */
      }
    }
    return removed
  }

  /**
   * Import ~/.ssh/config Host blocks that are not already configured.
   * ProxyJump hosts get a tag; IdentityFile is mapped to key auth.
   */
  async importSshConfig(): Promise<{ imported: number; skipped: number }> {
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
    const commit = async () => {
      if (current?.host && current.alias) {
        if (this.get(current.alias)) {
          skipped++
          return
        }
        await this.upsert({
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
        await commit()
        // skip wildcard/alias-less blocks
        if (value.includes('*') || value.includes('?')) continue
        current = { alias: value, host: value }
      } else if (current) {
        // HostName must win over the `Host` pattern: `current` is seeded with
        // host = alias, so a "only if still unset" guard can never be true and every
        // block like `Host work / HostName 10.0.0.5` imports pointing at the alias.
        if (key === 'HostName') current.host = value
        else if (key === 'Port') {
          const port = Number(value)
          // Number('abc') is NaN and the `?? 22` fallback downstream only catches
          // null/undefined — a NaN port reaches ssh2 as an unusable value.
          current.port = Number.isInteger(port) && port > 0 && port <= 65535 ? port : 22
        } else if (key === 'User') current.user = value
        else if (key === 'IdentityFile') current.keyPath = value.split(/\s+/)[0]
        else if (key === 'ProxyJump') current.proxyJump = value
        else if (key === 'SetEnv' && value.startsWith('SSH_HOST_ENV=')) current.environment = value.slice('SSH_HOST_ENV='.length)
      }
    }
    await commit()
    return { imported, skipped }
  }
}
