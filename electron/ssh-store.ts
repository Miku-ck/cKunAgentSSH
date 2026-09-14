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
  /** 目标环境标记('test' | 'prod'). 驱动生产环境高风险命令的审计与审批强化. 旧数据默认 'test' */
  environment?: 'test' | 'prod'
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
  /** 目标环境标记('test' | 'prod'),旧记录缺失时默认 'test' */
  environment?: 'test' | 'prod'
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
    environment: rec.environment || 'test',
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
    environment: rec.environment || 'test',
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
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // E-2 修复：readFile 失败（含瞬态 EBUSY/EACCES）【向上抛】，不再按空表返回。
    // 原实现失败后返回 []，随后的 save/delete/touchLastConnected 会以空数组为
    // 基线覆写磁盘文件 — 用户全部 SSH 连接配置（含加密凭据）被静默永久抹掉。
    // 写路径让错误自然上抛（IPC reject，用户重试即可）；读展示路径（list）在
    // 调用方 catch 后降级为空列表，不崩 UI。
    throw new Error(`读取 SSH 连接配置失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) {
      console.warn('[SSHStore] store 文件内容不是数组，已隔离损坏文件')
      await quarantineCorruptFile(path, raw)
      return []
    }
    return arr as SSHConnectionRecordOnDisk[]
  } catch (e) {
    // 审查 M-8：损坏文件隔离到 .corrupt-<ts>.bak。原实现 JSON.parse 失败即静默
    // 返回 []，随后的 save()/delete() 会以空数组为基线覆写磁盘文件 — 用户全部
    // SSH 连接配置（含加密凭据）被静默永久抹掉，且无任何日志线索
    console.error('[SSHStore] store 文件损坏（JSON 解析失败），已隔离原文件:', e)
    await quarantineCorruptFile(path, raw)
    return []
  }
}

/** 把损坏的 store 文件移走（或复制）为带时间戳的 .bak，保留现场供恢复 */
async function quarantineCorruptFile(path: string, raw: string): Promise<void> {
  try {
    const bak = `${path}.corrupt-${Date.now()}.bak`
    try {
      await rename(path, bak)
    } catch {
      // rename 失败（文件被占用等）：退回复制保留原文件
      await writeFile(bak, raw, 'utf-8')
    }
    console.warn(`[SSHStore] 损坏文件已备份到: ${bak}`)
  } catch (e) {
    console.warn('[SSHStore] 备份损坏文件失败:', e)
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

/**
 * 审查修复（P2）：就地重加密磁盘记录里的 plain: 旧格式字段。
 * 旧存储形态 plain: 仅为 base64 混淆（等效明文），safeStorage 可用时
 * 在任意一次写操作前将其升级为 enc:，消除明文落盘窗口。
 */
function reencryptPlainOnDisk(records: SSHConnectionRecordOnDisk[]): void {
  if (!safeStorage.isEncryptionAvailable()) return
  for (const rec of records) {
    for (const field of ['passwordEnc', 'passphraseEnc'] as const) {
      const v = rec[field]
      if (v && v.startsWith('plain:')) {
        try {
          const plain = Buffer.from(v.slice(6), 'base64').toString('utf-8')
          rec[field] = 'enc:' + safeStorage.encryptString(plain).toString('base64')
        } catch {
          // 解码失败：保留原值，不阻断写操作
        }
      }
    }
  }
}

export class SSHStore {
  /**
   * 审查修复（P2）：读-改-写串行化队列。原实现 save/delete/duplicate/
   * touchLastConnected/migrateLegacy 各自独立"读全量 → 改 → 写全量"，
   * 两个写操作并发时会以各自的旧基线互相覆写（丢更新）。
   * 参照 known-hosts 的 savePromise 链模式：所有写操作串在同一 promise 链上。
   */
  private writeQueue: Promise<unknown> = Promise.resolve()

  /** 把写操作串入队列；前一个操作失败不阻断后续（各自重试语义不变） */
  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(op, op)
    // 吞掉队列链本身的错误，防止前一个操作的 reject 污染后续操作的链
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }
  /**
   * 列表（读展示路径）：E-2 修复 — readAllFromDisk 现在会把读取失败向上抛
   * （防止空表覆写全库）。列表展示允许降级：catch 后返回 [] 并记日志，不崩 UI。
   * 写路径（save/delete/duplicate/touchLastConnected/migrateLegacy）让错误
   * 自然上抛，由 IPC reject 提示用户重试。
   */
  async list(): Promise<SSHConnectionRecord[]> {
    try {
      const records = await readAllFromDisk()
      return records.map(fromDisk)
    } catch (e) {
      console.error('[SSHStore] list 读取失败，降级为空列表:', e)
      return []
    }
  }

  async save(input: SSHConnectionRecord): Promise<SSHConnectionRecord> {
    return this.enqueue(() => this.doSave(input))
  }

  private async doSave(input: SSHConnectionRecord): Promise<SSHConnectionRecord> {
    if (!safeStorage.isEncryptionAvailable() && (input.password || input.passphrase)) {
      console.warn('[SSHStore] safeStorage 加密不可用，密码/passphrase 将不持久化（每次连接需重新输入或改用私钥认证）')
    }
    const records = await readAllFromDisk()
    reencryptPlainOnDisk(records)
    const now = Date.now()

    let record: SSHConnectionRecord
    const idx = records.findIndex(r => r.id === input.id)
    if (idx !== -1) {
      const existing = records[idx]
      record = { ...input, updatedAt: now, createdAt: existing.createdAt }
      const disk = toDisk(record)
      // 修复（凭据静默擦除）：渲染进程拿不到已保存的密码（ssh:listConnections
      // 已剥离明文凭据，E10），编辑弹窗的密码框恒为空，保存时携带 password:''。
      // 原实现整条覆写 → encrypt('') = undefined → 已存的加密密码被静默抹掉，
      // 下次连接认证失败，用户被迫反复重新输入密码。现在：认证方式未变且
      // 新值为空 → 保留磁盘上已有的加密凭据（"留空 = 保持不变"）。
      if (input.authType === existing.authType) {
        if (!disk.passwordEnc && existing.passwordEnc) disk.passwordEnc = existing.passwordEnc
        if (!disk.passphraseEnc && existing.passphraseEnc) disk.passphraseEnc = existing.passphraseEnc
      }
      records[idx] = disk
    } else {
      record = { ...input, id: input.id || genId(), createdAt: now, updatedAt: now }
      records.push(toDisk(record))
    }

    await writeAllToDisk(records)
    return record
  }

  /** 按 ID 读取单条记录（含解密后的凭据；仅主进程内部使用，不直接暴露给渲染进程） */
  async get(id: string): Promise<SSHConnectionRecord | null> {
    const records = await readAllFromDisk()
    const rec = records.find(r => r.id === id)
    return rec ? fromDisk(rec) : null
  }

  async delete(id: string): Promise<void> {
    return this.enqueue(() => this.doDelete(id))
  }

  private async doDelete(id: string): Promise<void> {
    const records = await readAllFromDisk()
    reencryptPlainOnDisk(records)
    const filtered = records.filter(r => r.id !== id)
    if (filtered.length !== records.length) {
      await writeAllToDisk(filtered)
    }
  }

  async duplicate(id: string): Promise<SSHConnectionRecord> {
    return this.enqueue(() => this.doDuplicate(id))
  }

  private async doDuplicate(id: string): Promise<SSHConnectionRecord> {
    const records = await readAllFromDisk()
    reencryptPlainOnDisk(records)
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
    return this.enqueue(() => this.doTouchLastConnected(id))
  }

  private async doTouchLastConnected(id: string): Promise<void> {
    const records = await readAllFromDisk()
    reencryptPlainOnDisk(records)
    const idx = records.findIndex(r => r.id === id)
    if (idx !== -1) {
      records[idx].lastConnectedAt = Date.now()
      await writeAllToDisk(records)
    }
  }

  /** 从渲染进程 localStorage 迁移旧数据(一次性) */
  async migrateLegacy(legacy: any[]): Promise<number> {
    if (!Array.isArray(legacy) || legacy.length === 0) return 0
    return this.enqueue(() => this.doMigrateLegacy(legacy))
  }

  private async doMigrateLegacy(legacy: any[]): Promise<number> {
    const records = await readAllFromDisk()
    reencryptPlainOnDisk(records)
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
