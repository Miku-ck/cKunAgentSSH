/**
 * SSH Store — 主进程连接配置持久化
 * 敏感字段(password/passphrase)使用 safeStorage 加密后存储
 */
import { app, safeStorage } from 'electron'
import { join } from 'path'
import { JsonFileStore } from './utils/json-file-store'

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
  /** S4-03：保存结果里的凭据告警。'encryption-unavailable' = 系统密钥环不可用，
   *  本次提交的 password/passphrase 没有落盘（encrypt() 拒绝写明文）。
   *  随 IPC 回传给渲染层提示用户 —— 此前这个事实只存在于主进程 console.warn。 */
  credentialNotice?: 'encryption-unavailable'
}

/**
 * 磁盘上的形态：只有两个敏感字段改名并加密，其余与内存形态逐字段相同。
 * 原先这里手抄了第二份 15 字段清单 —— 加字段只改一处就会静默分叉（toDisk
 * 少写即丢数据、且类型不报错），故派生而非复制。
 */
interface SSHConnectionRecordOnDisk extends Omit<SSHConnectionRecord, 'password' | 'passphrase'> {
  passwordEnc?: string
  passphraseEnc?: string
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
   * ARCH-007 收编：读-改-写串行化队列 / tmp+rename 原子写 / 损坏文件隔离 /
   * 读失败上抛四块骨架统一走 JsonFileStore（原为本类内联实现，与 tunnel/
   * gateway store 逐字同构）。写操作经 this.enqueue 串入队列的语义不变。
   */
  private readonly file = new JsonFileStore<SSHConnectionRecordOnDisk>({
    path: getStorePath(),
    logTag: '[SSHStore]',
    label: 'SSH 连接配置',
  })

  /** 把写操作串入队列；前一个操作失败不阻断后续（各自重试语义不变） */
  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    return this.file.enqueue(op)
  }
  /**
   * 列表（读展示路径）：E-2 修复 — file.readAll 现在会把读取失败向上抛
   * （防止空表覆写全库）。列表展示允许降级：catch 后返回 [] 并记日志，不崩 UI。
   * 写路径（save/delete/duplicate/touchLastConnected/migrateLegacy）让错误
   * 自然上抛，由 IPC reject 提示用户重试。
   */
  async list(): Promise<SSHConnectionRecord[]> {
    try {
      const records = await this.file.readAll()
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
    // S4-03 修复：encrypt() 在 safeStorage 不可用时返回 ''（不落明文，行为本身正确），
    // 但这个事实此前只写进主进程的 console.warn —— 渲染层收到的是「保存成功」，
    // 用户以为密码已存好，下次连接直接 USERAUTH_FAILURE。本文件头部立的
    // 「调用方须先检查 isEncryptionAvailable 并提示改用私钥认证」契约没人实现，
    // 现由保存动作本身把事实随记录回传。
    const credentialDropped =
      !safeStorage.isEncryptionAvailable() && Boolean(input.password || input.passphrase)
    if (credentialDropped) {
      console.warn('[SSHStore] safeStorage 加密不可用，密码/passphrase 将不持久化（每次连接需重新输入或改用私钥认证）')
    }
    const records = await this.file.readAll()
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
      // 审查R2修复：返回值以合并后的磁盘记录为准 — 原样返回 input spread 的 record
      // 不含被保留的磁盘凭据，main.ts stripCredentials 算出的 hasPassword/
      // hasPassphrase 恒为 false，前端（src/stores/ssh.ts）据此覆盖本地条目，
      // 编辑弹窗误显示"未存密码"。fromDisk 解密还原真实凭据（仅主进程内使用，
      // IPC 回传前由 stripCredentials 剥离，与 list() 同语义）。
      record = fromDisk(disk)
    } else {
      record = { ...input, id: input.id || genId(), createdAt: now, updatedAt: now }
      records.push(toDisk(record))
    }

    await this.file.writeAll(records)
    return credentialDropped
      ? { ...record, credentialNotice: 'encryption-unavailable' as const }
      : record
  }

  /** 按 ID 读取单条记录（含解密后的凭据；仅主进程内部使用，不直接暴露给渲染进程） */
  async get(id: string): Promise<SSHConnectionRecord | null> {
    const records = await this.file.readAll()
    const rec = records.find(r => r.id === id)
    return rec ? fromDisk(rec) : null
  }

  async delete(id: string): Promise<void> {
    return this.enqueue(() => this.doDelete(id))
  }

  private async doDelete(id: string): Promise<void> {
    const records = await this.file.readAll()
    reencryptPlainOnDisk(records)
    const filtered = records.filter(r => r.id !== id)
    if (filtered.length !== records.length) {
      await this.file.writeAll(filtered)
    }
  }

  async duplicate(id: string): Promise<SSHConnectionRecord> {
    return this.enqueue(() => this.doDuplicate(id))
  }

  private async doDuplicate(id: string): Promise<SSHConnectionRecord> {
    const records = await this.file.readAll()
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
    await this.file.writeAll(records)
    return clone
  }

  /** 更新最后连接时间(连接成功后调用) */
  async touchLastConnected(id: string): Promise<void> {
    return this.enqueue(() => this.doTouchLastConnected(id))
  }

  private async doTouchLastConnected(id: string): Promise<void> {
    const records = await this.file.readAll()
    reencryptPlainOnDisk(records)
    const idx = records.findIndex(r => r.id === id)
    if (idx !== -1) {
      records[idx].lastConnectedAt = Date.now()
      await this.file.writeAll(records)
    }
  }

  /** 从渲染进程 localStorage 迁移旧数据(一次性) */
  async migrateLegacy(legacy: any[]): Promise<number> {
    if (!Array.isArray(legacy) || legacy.length === 0) return 0
    return this.enqueue(() => this.doMigrateLegacy(legacy))
  }

  private async doMigrateLegacy(legacy: any[]): Promise<number> {
    const records = await this.file.readAll()
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
      await this.file.writeAll(records)
    }
    return migrated
  }
}
