import { createHash } from 'node:crypto'
import { chmodSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { dataFile } from './dsh-home.ts'

const FILE = 'ckun-dsh-ssh-knownhosts'

interface KhEntry {
  /** Canonical host pattern: `host` for port 22, `[host]:port` otherwise. */
  hostPattern: string
  /** Raw host key, base64. */
  keyBase64: string
}

export type KeyVerifyResult =
  | { status: 'matched' }
  | { status: 'new'; fingerprint: string }
  | { status: 'mismatch'; expected: string; actual: string }

/**
 * The plugin's own host-key pin store (a private known_hosts).
 *
 * Security model:
 *  - a host key is only ever PINNED by an explicit human act (the GUI
 *    "test connection -> trust fingerprint" flow); agent-initiated connects
 *    NEVER pin (fail closed);
 *  - a key that does not match the pin is always rejected (MITM guard);
 *  - a host with no pin is only reachable through the pinning flow.
 */
export class KnownHosts {
  private readonly file: string
  private entries: KhEntry[] = []

  constructor() {
    this.file = dataFile(FILE)
    this.load()
  }

  private load(): void {
    try {
      const text = readFileSync(this.file, 'utf8')
      this.entries = text
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line !== '' && !line.startsWith('#'))
        .map((line) => {
          const [hostPattern, keyBase64] = line.split(/\s+/)
          return { hostPattern, keyBase64 }
        })
        .filter((e): e is KhEntry => !!e.hostPattern && !!e.keyBase64)
    } catch {
      this.entries = []
    }
  }

  private save(): void {
    mkdirSync(dirname(this.file), { recursive: true })
    const lines = this.entries.map((e) => `${e.hostPattern} ${e.keyBase64}`)
    writeFileSync(this.file, lines.join('\n') + (lines.length > 0 ? '\n' : ''), { mode: 0o600 })
    try {
      chmodSync(this.file, 0o600)
    } catch {
      /* best effort (Windows) */
    }
  }

  /** The host patterns that address (host, port). */
  private patterns(host: string, port: number): string[] {
    return [host, `[${host}]:${port}`]
  }

  private lookup(host: string, port: number): KhEntry | undefined {
    const wanted = new Set(this.patterns(host, port))
    return this.entries.find((e) => wanted.has(e.hostPattern))
  }

  /** Compare the presented raw host key against the pin, if any. */
  verify(host: string, port: number, keyRaw: Buffer): KeyVerifyResult {
    const actual = fingerprint(keyRaw)
    const entry = this.lookup(host, port)
    if (entry === undefined) return { status: 'new', fingerprint: actual }
    if (entry.keyBase64 === keyRaw.toString('base64')) return { status: 'matched' }
    return { status: 'mismatch', expected: fingerprint(Buffer.from(entry.keyBase64, 'base64')), actual }
  }

  /** Record a presented host key as trusted (explicit human act only). */
  pin(host: string, port: number, keyRaw: Buffer): void {
    const pattern = this.patterns(host, port)
    this.entries = this.entries.filter((e) => !pattern.includes(e.hostPattern))
    this.entries.push({ hostPattern: port === 22 ? host : `[${host}]:${port}`, keyBase64: keyRaw.toString('base64') })
    this.save()
  }

  /** Whether (host, port) already has a pinned key. */
  pinned(host: string, port: number): boolean {
    return this.lookup(host, port) !== undefined
  }

  /** The pinned key's fingerprint, for display. */
  fingerprintOf(host: string, port: number): string | undefined {
    const e = this.lookup(host, port)
    return e === undefined ? undefined : fingerprint(Buffer.from(e.keyBase64, 'base64'))
  }
}

/** OpenSSH-style SHA256:<base64> digest of a raw host key. */
export function fingerprint(keyRaw: Buffer): string {
  const digest = createHash('sha256').update(keyRaw).digest('base64').replace(/=+$/, '')
  return `SHA256:${digest}`
}
