/**
 * Gateway Store — TLS 接入网关记录持久化（gateways.json，与 tunnels.json 同级）
 *
 * 设计（spec: docs/superpowers/specs/2026-10-08-tls-dataplane-design.md §3.2）：
 * - 骨架照抄 tunnel-store.ts：写队列串行化 / tmp+rename 原子写 / 损坏文件隔离。
 * - 与 tunnel-store 的**唯一区别**是这里有口令：照 ssh-store.ts 的口径走 safeStorage，
 *   加密不可用时宁可不落盘也不写明文（ssh-store:271 同一处理由）。
 * - 渲染层只拿 listPublic()（口令字段抹掉），建连时主进程自己 get() 取。
 * - SPKI 指纹直接记在记录上，不另开 known-hosts 文件：这里的身份就是 host:port 一条记录，
 *   拆两个文件只会让「改了地址指纹还留不留」变成需要同步的第二处状态。
 */
import { app, safeStorage } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink } from 'fs/promises'
import { existsSync } from 'fs'

export interface GatewayRecord {
  id: string
  name: string
  host: string
  port: number
  /** CA（PEM）。缺省时信任判定完全交给 trustedSpki */
  caPem?: string
  /** 已确认的服务端 SPKI 指纹（sha256/…）。缺省 = 首次连接走 TOFU */
  trustedSpki?: string
  user: string
  /** 落盘为 enc:base64；listPublic() 里抹掉 */
  password?: string
  autoConnect: boolean
  createdAt: number
  updatedAt: number
}

/** 渲染层可见的形状：口令永不出主进程 */
export type PublicGatewayRecord = Omit<GatewayRecord, 'password'>

function getStorePath(): string {
  return join(app.getPath('userData'), 'gateways.json')
}

function genId(): string {
  return `gw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function encrypt(plain?: string): string | undefined {
  if (!plain) return undefined
  if (!safeStorage.isEncryptionAvailable()) {
    console.warn('[GatewayStore] safeStorage 不可用，口令不落盘（每次连接需重新输入）')
    return undefined
  }
  return 'enc:' + safeStorage.encryptString(plain).toString('base64')
}

function decrypt(stored?: string): string | undefined {
  if (!stored) return undefined
  if (!stored.startsWith('enc:')) return stored
  if (!safeStorage.isEncryptionAvailable()) return undefined
  try {
    return safeStorage.decryptString(Buffer.from(stored.slice(4), 'base64'))
  } catch (e) {
    console.warn('[GatewayStore] 口令解密失败（系统钥匙串可能已重置）:', e)
    return undefined
  }
}

function isPort(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= 65535
}

export function validateGateway(rec: GatewayRecord, all: GatewayRecord[]): void {
  if (!rec || typeof rec !== 'object') throw new Error('网关记录无效')
  if (typeof rec.name !== 'string' || !rec.name.trim()) throw new Error('必须填写网关名称')
  if (typeof rec.host !== 'string' || !rec.host.trim()) throw new Error('必须填写网关地址')
  if (!isPort(rec.port)) throw new Error('端口需为 1–65535 的整数')
  if (typeof rec.user !== 'string' || !rec.user.trim()) throw new Error('必须填写用户名')
  const key = `${rec.host.trim().toLowerCase()}:${rec.port}`
  for (const other of all) {
    if (other.id === rec.id) continue
    if (`${other.host.toLowerCase()}:${other.port}` === key) throw new Error(`网关 ${key} 已添加过`)
  }
}

async function readAllFromDisk(): Promise<GatewayRecord[]> {
  const path = getStorePath()
  if (!existsSync(path)) return []
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // 与 ssh-store 同口径：读失败必须上抛，否则下一次写会以空表覆写全库
    throw new Error(`读取网关记录失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) {
      await quarantine(raw)
      return []
    }
    const valid = arr.filter(
      (r): r is GatewayRecord => !!r && typeof r === 'object' && typeof (r as { id?: unknown }).id === 'string',
    )
    if (valid.length !== arr.length) console.warn(`[GatewayStore] 含 ${arr.length - valid.length} 条无 id 损坏条目，已跳过`)
    return valid
  } catch {
    console.warn('[GatewayStore] gateways.json 损坏（JSON 解析失败），已隔离原文件')
    await quarantine(raw)
    return []
  }
}

async function quarantine(raw: string): Promise<void> {
  const path = getStorePath()
  try {
    const bak = `${path}.corrupt-${Date.now()}.bak`
    try {
      await rename(path, bak)
    } catch {
      await writeFile(bak, raw, 'utf-8')
    }
    console.warn(`[GatewayStore] 损坏文件已备份到: ${bak}`)
  } catch (e) {
    console.warn('[GatewayStore] 备份损坏文件失败:', e)
  }
}

async function writeAllToDisk(records: GatewayRecord[]): Promise<void> {
  const path = getStorePath()
  await mkdir(dirname(path), { recursive: true })
  const tmp = path + '.tmp'
  await writeFile(tmp, JSON.stringify(records, null, 2), 'utf-8')
  try {
    await rename(tmp, path)
  } catch (err) {
    const code = (err as { code?: string })?.code
    if (code === 'EPERM' || code === 'EACCES' || code === 'EBUSY') {
      await new Promise(r => setTimeout(r, 200))
      try {
        await rename(tmp, path)
        return
      } catch {
        await writeFile(path, JSON.stringify(records, null, 2), 'utf-8')
        try { await unlink(tmp) } catch { /* ignore */ }
      }
    } else throw err
  }
}

const strip = (r: GatewayRecord): PublicGatewayRecord => {
  const { password: _password, ...rest } = r
  return rest
}

export class GatewayStore {
  private writeQueue: Promise<unknown> = Promise.resolve()

  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(op, op)
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }

  /** 渲染层用：不含口令 */
  async list(): Promise<PublicGatewayRecord[]> {
    try {
      return (await readAllFromDisk()).map(strip)
    } catch (e) {
      console.error('[GatewayStore] list 读取失败，降级为空列表:', e)
      return []
    }
  }

  /** 主进程建连用：含解密后的口令 */
  async getWithSecret(id: string): Promise<GatewayRecord | null> {
    const all = await readAllFromDisk()
    const rec = all.find(r => r.id === id)
    if (!rec) return null
    return { ...rec, password: decrypt(rec.password) }
  }

  async save(input: GatewayRecord): Promise<PublicGatewayRecord> {
    return this.enqueue(async () => {
      const all = await readAllFromDisk()
      const now = Date.now()
      const idx = all.findIndex(r => r.id === input.id)
      const stored: GatewayRecord = {
        ...input,
        id: input.id || genId(),
        host: input.host.trim(),
        name: input.name.trim(),
        createdAt: idx !== -1 ? all[idx].createdAt : now,
        updatedAt: now,
      }
      validateGateway(stored, all)
      // 留空 = 保留原值（与 ssh-store 的凭据口径一致），落盘前才换成密文
      const plain = input.password || (idx !== -1 ? all[idx].password : undefined)
      stored.password = encrypt(plain) ?? (idx !== -1 ? all[idx].password : undefined)
      // 地址或 CA 变了，之前确认的指纹就不该继续作数
      if (idx !== -1 && (all[idx].host !== stored.host || all[idx].port !== stored.port || all[idx].caPem !== stored.caPem)) {
        stored.trustedSpki = undefined
      }
      if (idx !== -1) all[idx] = stored
      else all.push(stored)
      await writeAllToDisk(all)
      return strip(stored)
    })
  }

  /** TOFU 确认后写指纹 */
  async setSpki(id: string, spki: string): Promise<void> {
    return this.enqueue(async () => {
      const all = await readAllFromDisk()
      const rec = all.find(r => r.id === id)
      if (!rec) throw new Error('网关记录不存在')
      rec.trustedSpki = spki
      rec.updatedAt = Date.now()
      await writeAllToDisk(all)
    })
  }

  async delete(id: string): Promise<void> {
    return this.enqueue(async () => {
      const all = await readAllFromDisk()
      const kept = all.filter(r => r.id !== id)
      if (kept.length !== all.length) await writeAllToDisk(kept)
    })
  }
}
