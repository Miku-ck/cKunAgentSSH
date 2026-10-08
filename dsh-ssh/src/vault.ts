import { createHash } from 'node:crypto'
import type { CredentialKey, CredentialRecord } from '@deepseek-ai/dsh-credentials'

/** Per-host SSH secrets. Only non-empty fields are meaningful. */
export interface HostSecrets {
  password?: string
  passphrase?: string
}

/**
 * The credential-record seam as this plugin uses it. Duck-typed on purpose:
 * the runtime object arrives through `ctx.credentials` (provided by the host's
 * credentials provider), so the host bundle never imports the seam package —
 * only its types, which are erased at build time.
 */
interface CredentialsServiceLike {
  readRecord(key: CredentialKey): Promise<CredentialRecord | undefined>
  modifyRecord(
    key: CredentialKey,
    mutate: (current: CredentialRecord | undefined) => Promise<CredentialRecord | undefined>,
  ): Promise<CredentialRecord | undefined>
  deleteRecord(key: CredentialKey): Promise<void>
}

/**
 * Secret storage for host credentials. Backed by DSH credential records
 * (`ckun-ssh/<id>` grant records in the credentials provider, e.g.
 * `~/.dsh/.credentials.yaml`); falls back to "unavailable" when the host has
 * no credentials provider, in which case HostStore keeps secrets in its
 * 0600 JSON file (the M1 path).
 */
export interface CredentialVault {
  readonly available: boolean
  read(alias: string): Promise<HostSecrets>
  /** Replace the stored secrets for a host; an empty secret deletes the record. */
  save(alias: string, secrets: HostSecrets): Promise<void>
  remove(alias: string): Promise<void>
}

/** Owner scope for every record this plugin writes. */
const SCOPE = 'ckun-ssh'

const SEGMENT = /^[a-z][a-z0-9-]*$/

/**
 * Map a user-facing alias to a legal credential-key id segment.
 *
 * When sanitisation is LOSSY the result gets a digest of the raw alias appended.
 * Without that, every alias whose characters all get stripped — any all-CJK alias,
 * e.g. `生产库` and `测试库` — collapses to the single id `host`, and two different
 * machines then read and write ONE credential record: host A's password is offered
 * to host B (a wrong-secret login, or worse, A's secret authenticating on B). The
 * same applies to the >64-char truncation.
 *
 * Healthy ASCII aliases are untouched, so existing records keep their keys —
 * the digest only appears where the old shape was already colliding.
 */
export function secretIdForAlias(alias: string): string {
  const digest = createHash('sha256').update(alias, 'utf8').digest('hex').slice(0, 10)
  let id = alias.toLowerCase().replace(/[^a-z0-9-]+/g, '-')
  id = id.replace(/-+/g, '-').replace(/^-+|-+$/g, '')
  if (!/^[a-z]/.test(id)) id = id ? `h-${id}` : ''
  const truncated = id.length > 64
  if (truncated) id = id.slice(0, 64).replace(/-+$/, '')
  if (!id || !SEGMENT.test(id)) return `host-${digest}`
  return truncated ? `${id}-${digest}` : id
}

/** The grant payload shape this plugin writes (opaque to the seam). */
interface GrantPayload {
  v: 1
  password?: string
  passphrase?: string
}

function toKey(alias: string): CredentialKey {
  const id = secretIdForAlias(alias)
  if (!SEGMENT.test(SCOPE) || !SEGMENT.test(id)) throw new Error(`illegal credential key for alias: ${alias}`)
  return `${SCOPE}/${id}` as unknown as CredentialKey
}

function extract(record: CredentialRecord | undefined): HostSecrets {
  if (!record || record.kind !== 'grant') return {}
  const p = record.payload as GrantPayload | null
  if (!p || p.v !== 1) return {}
  return {
    password: typeof p.password === 'string' && p.password ? p.password : undefined,
    passphrase: typeof p.passphrase === 'string' && p.passphrase ? p.passphrase : undefined,
  }
}

const UNAVAILABLE: CredentialVault = {
  available: false,
  read: async () => ({}),
  save: async () => undefined,
  remove: async () => undefined,
}

/** Wrap `ctx.credentials` (or undefined) as a vault; never throws on a missing service. */
export function createCredentialVault(creds: unknown): CredentialVault {
  const c = creds as Partial<CredentialsServiceLike> | null | undefined
  if (!c || typeof c.readRecord !== 'function' || typeof c.modifyRecord !== 'function' || typeof c.deleteRecord !== 'function') {
    return UNAVAILABLE
  }
  const service = c as CredentialsServiceLike
  return {
    available: true,
    async read(alias: string): Promise<HostSecrets> {
      try {
        return extract(await service.readRecord(toKey(alias)))
      } catch {
        return {}
      }
    },
    async save(alias: string, secrets: HostSecrets): Promise<void> {
      const key = toKey(alias)
      if (!secrets.password && !secrets.passphrase) {
        await service.deleteRecord(key)
        return
      }
      const payload: GrantPayload = { v: 1 }
      if (secrets.password) payload.password = secrets.password
      if (secrets.passphrase) payload.passphrase = secrets.passphrase
      await service.modifyRecord(key, async () => ({ kind: 'grant', payload } satisfies CredentialRecord))
    },
    async remove(alias: string): Promise<void> {
      await service.deleteRecord(toKey(alias))
    },
  }
}
