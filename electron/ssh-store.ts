/**
 * SSH Store — 主进程连接配置持久化
 * 敏感字段(password/passphrase)使用 safeStorage 加密后存储
 */
import { app, safeStorage } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink } from 'fs/promises'
import { existsSync } from 'fs'

export interface SSHConnectionRecord {
  id: string
  name: string
  host: string
  port: number
  username: string
  authType: 'password' | 'privateKey' | 'agent'
  password?: string
  privateKeyPath?: string
  passphrase?: string
  /** 备注(单行文字,最多 10 字). 旧数据可能没有此字段 */
  note?: string
  tags: string[]
  readyTimeout?: number
  keepaliveInterval?: number
  lastConnectedAt?: number
  createdAt: number
  updatedAt: number
}

/** 磁盘上的形态:敏感字段为加密后的 base64 字符串 */
interface SSHConnectionRecordOnDisk {
  id: string
  name: string
  host: string
  port: number
  username: string
  authType: 'password' | 'privateKey' | 'agent'
  passwordEnc?: string
  privateKeyPath?: string
  passphraseEnc?: string
  /** 备注,旧记录可能缺失 */
  note?: string
  tags: string[]
  readyTimeout?: number
  keepaliveInterval?: number
  lastConnectedAt?: number
  createdAt: number
  updatedAt: number
}

function getStorePath(): string {
  return join(app.getPath('userData'), 'ssh-connections.json')
}

function encrypt(plain?: string): string | undefined {
  if (!plain) return undefined
  if (!safeStorage.isEncryptionAvailable()) {
    // 加密不可用：拒绝存储密码，返回空字符串标记（不落盘明文）。
    // 调用方应在保存前检查 isEncryptionAvailable 并提示用户改用私钥认证。
    return ''
  }
  return 'enc:' + safeStorage.encryptString(plain).toString('base64')
}

/** safeStorage 加密服务是否可用（供主进程/渲染进程提前检测并提示用户） */
export function isEncryptionAvailable(): boolean {
  return safeStorage.isEncryptionAvailable()
}

function decrypt(stored?: string): string | undefined {
  if (!stored) return undefined
  if (stored.startsWith('enc:')) {
    if (!safeStorage.isEncryptionAvailable()) return undefined
    try {
      return safeStorage.decryptString(Buffer.from(stored.slice(4), 'base64'))
    } catch {
      return undefined
    }
  }
  if (stored.startsWith('plain:')) {
    try {
      return Buffer.from(stored.slice(6), 'base64').toString('utf-8')
    } catch {
      return undefined
    }
  }
  return undefined
}

function toDisk(rec: SSHConnectionRecord): SSHConnectionRecordOnDisk {
  return {
    id: rec.id,
    name: rec.name,
    host: rec.host,
    port: rec.port,
    username: rec.username,
    authType: rec.authType,
    passwordEnc: encrypt(rec.password),
    privateKeyPath: rec.privateKeyPath,
    passphraseEnc: encrypt(rec.passphrase),
    note: rec.note,
    tags: rec.tags || [],
    readyTimeout: rec.readyTimeout,
    keepaliveInterval: rec.keepaliveInterval,
    lastConnectedAt: rec.lastConnectedAt,
    createdAt: rec.createdAt,
    updatedAt: rec.updatedAt,
  }
}

function fromDisk(rec: SSHConnectionRecordOnDisk): SSHConnectionRecord {
  return {
    id: rec.id,
    name: rec.name,
    host: rec.host,
    port: rec.port,
    username: rec.username,
    authType: rec.authType,
    password: decrypt(rec.passwordEnc),
    privateKeyPath: rec.privateKeyPath,
    passphrase: decrypt(rec.passphraseEnc),
    note: rec.note,
    tags: rec.tags || [],
    readyTimeout: rec.readyTimeout,
    keepaliveInterval: rec.keepaliveInterval,
    lastConnectedAt: rec.lastConnectedAt,
    createdAt: rec.createdAt,
    updatedAt: rec.updatedAt,
  }
}

async function readAllFromDisk(): Promise<SSHConnectionRecordOnDisk[]> {
  const path = getStorePath()
  if (!existsSync(path)) return []
  try {
    const raw = await readFile(path, 'utf-8')
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return []
    return arr as SSHConnectionRecordOnDisk[]
  } catch {
    return []
  }
}

async function writeAllToDisk(records: SSHConnectionRecordOnDisk[]): Promise<void> {
  const path = getStorePath()
  await mkdir(dirname(path), { recursive: true })
  // 原子写入：先写临时文件，再 rename 替换，避免写入过程中断导致 json 损坏
  const tmpPath = path + '.tmp'
  await writeFile(tmpPath, JSON.stringify(records, null, 2), 'utf-8')
  // M4 修复：Windows 上 rename 到已存在的目标可能因文件被占用（EPERM/EPERM）失败。
  // 重试一次（短暂等待后），若仍失败则回退到直接写入目标文件——丢失原子性但保证持久化。
  try {
    await rename(tmpPath, path)
  } catch (err: any) {
    if (err && (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY')) {
      // 短暂等待后重试一次（常见于杀毒软件/索引服务短暂占用）
      await new Promise(r => setTimeout(r, 200))
      try {
        await rename(tmpPath, path)
        return
      } catch {
        // 仍失败：回退到直接写入目标文件（非原子，但保证数据不丢）
        console.warn('[SSHStore] rename 重试仍失败，回退到直接写入')
        await writeFile(path, JSON.stringify(records, null, 2), 'utf-8')
        // 清理临时文件
        try { await unlink(tmpPath) } catch { /* ignore */ }
      }
    } else {
      throw err
    }
  }
}

function genId(): string {
  return `conn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export class SSHStore {
  async list(): Promise<SSHConnectionRecord[]> {
    const records = await readAllFromDisk()
    return records.map(fromDisk)
  }

  async save(input: SSHConnectionRecord): Promise<SSHConnectionRecord> {
    if (!safeStorage.isEncryptionAvailable() && (input.password || input.passphrase)) {
      console.warn('[SSHStore] safeStorage 加密不可用，密码/passphrase 将不持久化（每次连接需重新输入或改用私钥认证）')
    }
    const records = await readAllFromDisk()
    const now = Date.now()

    let record: SSHConnectionRecord
    const idx = records.findIndex(r => r.id === input.id)
    if (idx !== -1) {
      record = { ...input, updatedAt: now, createdAt: records[idx].createdAt }
      records[idx] = toDisk(record)
    } else {
      record = { ...input, id: input.id || genId(), createdAt: now, updatedAt: now }
      records.push(toDisk(record))
    }

    await writeAllToDisk(records)
    return record
  }

  async delete(id: string): Promise<void> {
    const records = await readAllFromDisk()
    const filtered = records.filter(r => r.id !== id)
    if (filtered.length !== records.length) {
      await writeAllToDisk(filtered)
    }
  }

  async duplicate(id: string): Promise<SSHConnectionRecord> {
    const records = await readAllFromDisk()
    const src = records.find(r => r.id === id)
    if (!src) throw new Error(`Connection ${id} not found`)

    const srcRec = fromDisk(src)
    const now = Date.now()
    const clone: SSHConnectionRecord = {
      ...srcRec,
      id: genId(),
      name: `${srcRec.name} 副本`,
      lastConnectedAt: undefined,
      createdAt: now,
      updatedAt: now,
    }
    records.push(toDisk(clone))
    await writeAllToDisk(records)
    return clone
  }

  /** 更新最后连接时间(连接成功后调用) */
  async touchLastConnected(id: string): Promise<void> {
    const records = await readAllFromDisk()
    const idx = records.findIndex(r => r.id === id)
    if (idx !== -1) {
      records[idx].lastConnectedAt = Date.now()
      await writeAllToDisk(records)
    }
  }

  /** 导出:返回不含密码的 JSON 字符串 */
  async export(ids: string[]): Promise<string> {
    const records = await readAllFromDisk()
    const selected = records
      .filter(r => ids.includes(r.id))
      .map(fromDisk)
      .map(r => ({
        name: r.name,
        host: r.host,
        port: r.port,
        username: r.username,
        authType: r.authType,
        privateKeyPath: r.privateKeyPath,
        tags: r.tags,
        readyTimeout: r.readyTimeout,
        keepaliveInterval: r.keepaliveInterval,
        // 密码/passphrase 不导出,强制用户重新填写
        password: '',
        passphrase: '',
      }))
    return JSON.stringify({ version: 1, exportedAt: Date.now(), connections: selected }, null, 2)
  }

  /** 导入:合并,已存在相同 host+port+username 的跳过 */
  async import(json: string): Promise<{ imported: number; skipped: number }> {
    let data: any
    try {
      data = JSON.parse(json)
    } catch {
      throw new Error('JSON 格式错误')
    }
    const incoming: any[] = Array.isArray(data) ? data : data.connections
    if (!Array.isArray(incoming)) throw new Error('文件结构无效')

    const records = await readAllFromDisk()
    const existing = new Set(records.map(r => `${r.host}:${r.port}:${r.username}`))

    let imported = 0
    let skipped = 0
    const now = Date.now()

    for (const item of incoming) {
      if (!item.host || !item.username) {
        skipped++
        continue
      }
      const key = `${item.host}:${item.port || 22}:${item.username}`
      if (existing.has(key)) {
        skipped++
        continue
      }
      const rec: SSHConnectionRecord = {
        id: genId(),
        name: item.name || `${item.username}@${item.host}`,
        host: item.host,
        port: Math.min(65535, Math.max(1, Number(item.port) || 22)),
        username: item.username,
        authType: item.authType === 'privateKey' || item.authType === 'agent' ? item.authType : 'password',
        password: item.password || undefined,
        privateKeyPath: item.privateKeyPath || undefined,
        passphrase: item.passphrase || undefined,
        tags: Array.isArray(item.tags) ? item.tags : [],
        readyTimeout: typeof item.readyTimeout === 'number' ? item.readyTimeout : undefined,
        keepaliveInterval: typeof item.keepaliveInterval === 'number' ? item.keepaliveInterval : undefined,
        createdAt: now,
        updatedAt: now,
      }
      records.push(toDisk(rec))
      existing.add(key)
      imported++
    }

    if (imported > 0) {
      await writeAllToDisk(records)
    }
    return { imported, skipped }
  }

  /** 从渲染进程 localStorage 迁移旧数据(一次性) */
  async migrateLegacy(legacy: any[]): Promise<number> {
    if (!Array.isArray(legacy) || legacy.length === 0) return 0
    const records = await readAllFromDisk()
    const existing = new Set(records.map(r => `${r.host}:${r.port}:${r.username}`))
    const now = Date.now()
    let migrated = 0

    for (const item of legacy) {
      if (!item.host || !item.username) continue
      const key = `${item.host}:${item.port || 22}:${item.username}`
      if (existing.has(key)) continue
      const rec: SSHConnectionRecord = {
        id: item.id || genId(),
        name: item.name || `${item.username}@${item.host}`,
        host: item.host,
        port: Math.min(65535, Math.max(1, Number(item.port) || 22)),
        username: item.username,
        authType: item.authType === 'privateKey' || item.authType === 'agent' ? item.authType : 'password',
        password: item.password || undefined,
        privateKeyPath: item.privateKeyPath || undefined,
        passphrase: item.passphrase || undefined,
        tags: Array.isArray(item.tags) ? item.tags : [],
        readyTimeout: typeof item.readyTimeout === 'number' ? item.readyTimeout : undefined,
        keepaliveInterval: typeof item.keepaliveInterval === 'number' ? item.keepaliveInterval : undefined,
        createdAt: now,
        updatedAt: now,
      }
      records.push(toDisk(rec))
      existing.add(key)
      migrated++
    }

    if (migrated > 0) {
      await writeAllToDisk(records)
    }
    return migrated
  }
}
