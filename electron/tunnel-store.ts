/**
 * Tunnel Store — SSH 隧道规则持久化（tunnels.json，与 ssh-connections.json 同级）
 *
 * 设计要点（spec: docs/superpowers/specs/2026-10-08-ssh-tunnel-design.md）：
 * - 规则独立于 SSHConnectionRecord 持久化，以 connectionId 弱引用连接配置，
 *   连接被删时启动报 config-missing（TunnelManager.start 兜底），零迁移。
 * - 无凭据字段，无需 safeStorage 加密。
 * - 骨架照抄 ssh-store.ts：写队列串行化 / tmp+rename 原子写 / 损坏文件隔离。
 */
import { app } from 'electron'
import { join, dirname } from 'path'
import { readFile, writeFile, rename, mkdir, unlink } from 'fs/promises'
import { existsSync } from 'fs'

export type TunnelType = 'local' | 'remote' | 'dynamic'
/** 数据面：借 SSH 通道，或走自家 TLS 网关。缺省视为 'ssh'，所以存量规则零迁移。 */
export type TunnelVia = 'ssh' | 'gateway'

export interface TunnelRule {
  id: string
  /** 展示名，可空（UI 显示自动名「L 8080 → db:5432」式） */
  name: string
  /** 引用 SSHConnectionRecord.id（弱引用：连接被删 → 启动报 config-missing） */
  connectionId: string
  /** 引用 GatewayRecord.id（仅 via='gateway'） */
  gatewayId?: string
  via?: TunnelVia
  type: TunnelType
  /**
   * local:   本机 listenPort → (经SSH) → dstHost:dstPort
   * remote:  远端 bindPort   → (经SSH回) → 本机 dstHost:dstPort
   * dynamic: 本机 listenPort 起 SOCKS5，目标由客户端 CONNECT 指定
   */
  /** 默认 127.0.0.1，仅 local 可自定义；dynamic 强制 127.0.0.1（安全边界：不对局域网开放） */
  listenHost?: string
  listenPort?: number
  /** 默认 127.0.0.1，仅 remote；0.0.0.0 需 UI 显式确认风险 */
  remoteBindHost?: string
  bindPort?: number
  dstHost?: string
  dstPort?: number
  /** 应用启动后自动拉起（V1 默认 false，字段先留） */
  autoStart: boolean
  createdAt: number
  updatedAt: number
}

function getStorePath(): string {
  return join(app.getPath('userData'), 'tunnels.json')
}

function genId(): string {
  return `tnl-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function isPort(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= 65535
}

/**
 * 结构校验：save 时强制（渲染层还有一层 UI 校验，这里是最终防线）。
 * 抛 Error（中文文案，IPC 直接透传给用户）。
 */
function validateRule(rule: TunnelRule, all: TunnelRule[]): void {
  if (!rule || typeof rule !== 'object') throw new Error('隧道规则无效')
  if (rule.via === 'gateway') {
    if (typeof rule.gatewayId !== 'string' || !rule.gatewayId) throw new Error('必须选择一个 TLS 网关')
  } else {
    if (typeof rule.connectionId !== 'string' || !rule.connectionId) throw new Error('必须选择一个 SSH 连接')
  }
  if (rule.type !== 'local' && rule.type !== 'remote' && rule.type !== 'dynamic') throw new Error('未知的隧道类型')

  if (rule.type === 'local' || rule.type === 'dynamic') {
    if (!isPort(rule.listenPort)) throw new Error('本地监听端口需为 1–65535 的整数')
  }
  if (rule.type === 'local' || rule.type === 'remote') {
    if (typeof rule.dstHost !== 'string' || !rule.dstHost.trim()) throw new Error('必须填写目标主机')
    if (!isPort(rule.dstPort)) throw new Error('目标端口需为 1–65535 的整数')
  }
  if (rule.type === 'remote') {
    if (!isPort(rule.bindPort)) throw new Error('远端绑定端口需为 1–65535 的整数')
  }

  // 安全边界：dynamic 强制 127.0.0.1（SOCKS 意味着本机任意进程可经此访问远端网段，
  // 不对局域网开放；local 允许自定义监听地址）
  if (rule.type === 'dynamic') rule.listenHost = '127.0.0.1'

  // 查重：同监听端点不允许两条规则（同 id 除外 = 编辑自身）
  const listenKey = `${(rule.listenHost || '127.0.0.1').toLowerCase()}:${rule.listenPort}`
  const bindKey = `${(rule.remoteBindHost || '127.0.0.1').toLowerCase()}:${rule.bindPort}`
  for (const other of all) {
    if (other.id === rule.id) continue
    if (other.type === 'local' || other.type === 'dynamic') {
      if (rule.type !== 'remote' && `${(other.listenHost || '127.0.0.1').toLowerCase()}:${other.listenPort}` === listenKey) {
        throw new Error(`本地监听 ${listenKey} 已被其他隧道使用`)
      }
    }
    if (other.type === 'remote' && rule.type === 'remote') {
      // 跨连接的服务端占用由启动时报错兜底；同连接同绑定端点在这里拦截
      if (other.connectionId === rule.connectionId && `${(other.remoteBindHost || '127.0.0.1').toLowerCase()}:${other.bindPort}` === bindKey) {
        throw new Error(`远端绑定 ${bindKey} 已被同一连接的其他隧道使用`)
      }
    }
  }
}

async function readAllFromDisk(): Promise<TunnelRule[]> {
  const path = getStorePath()
  if (!existsSync(path)) return []
  let raw: string
  try {
    raw = await readFile(path, 'utf-8')
  } catch (e) {
    // 同 ssh-store E-2 口径：读失败向上抛，防止后续写以空表覆写全库
    throw new Error(`读取隧道规则失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`)
  }
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) {
      await quarantineCorruptFile(path, raw)
      return []
    }
    const valid = arr.filter(
      (r): r is TunnelRule =>
        !!r && typeof r === 'object' && typeof (r as { id?: unknown }).id === 'string',
    )
    if (valid.length !== arr.length) {
      console.warn(`[TunnelStore] 含 ${arr.length - valid.length} 条无 id 损坏条目，已跳过`)
    }
    return valid
  } catch {
    console.warn('[TunnelStore] tunnels.json 损坏（JSON 解析失败），已隔离原文件')
    await quarantineCorruptFile(path, raw)
    return []
  }
}

async function quarantineCorruptFile(path: string, raw: string): Promise<void> {
  try {
    const bak = `${path}.corrupt-${Date.now()}.bak`
    try {
      await rename(path, bak)
    } catch {
      await writeFile(bak, raw, 'utf-8')
    }
    console.warn(`[TunnelStore] 损坏文件已备份到: ${bak}`)
  } catch (e) {
    console.warn('[TunnelStore] 备份损坏文件失败:', e)
  }
}

async function writeAllToDisk(records: TunnelRule[]): Promise<void> {
  const path = getStorePath()
  await mkdir(dirname(path), { recursive: true })
  const tmpPath = path + '.tmp'
  await writeFile(tmpPath, JSON.stringify(records, null, 2), 'utf-8')
  // Windows rename 到已存在目标可能 EPERM/EBUSY：重试一次后回退直写（与 ssh-store 同款）
  try {
    await rename(tmpPath, path)
  } catch (err: any) {
    if (err && (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY')) {
      await new Promise(r => setTimeout(r, 200))
      try {
        await rename(tmpPath, path)
        return
      } catch {
        await writeFile(path, JSON.stringify(records, null, 2), 'utf-8')
        try { await unlink(tmpPath) } catch { /* ignore */ }
      }
    } else {
      throw err
    }
  }
}

export class TunnelStore {
  /** 读-改-写串行化（同 ssh-store：防并发写互相覆写丢更新） */
  private writeQueue: Promise<unknown> = Promise.resolve()

  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    const run = this.writeQueue.then(op, op)
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }

  async list(): Promise<TunnelRule[]> {
    try {
      return await readAllFromDisk()
    } catch (e) {
      console.error('[TunnelStore] list 读取失败，降级为空列表:', e)
      return []
    }
  }

  async get(id: string): Promise<TunnelRule | null> {
    const records = await readAllFromDisk()
    return records.find(r => r.id === id) ?? null
  }

  async save(input: TunnelRule): Promise<TunnelRule> {
    return this.enqueue(() => this.doSave(input))
  }

  private async doSave(input: TunnelRule): Promise<TunnelRule> {
    const records = await readAllFromDisk()
    const now = Date.now()
    const idx = records.findIndex(r => r.id === input.id)
    let record: TunnelRule
    if (idx !== -1) {
      record = { ...input, createdAt: records[idx].createdAt, updatedAt: now }
    } else {
      record = { ...input, id: input.id || genId(), createdAt: now, updatedAt: now }
    }
    // 校验在合并后做（skip 自身查重），失败即抛、不落盘
    validateRule(record, records)
    if (idx !== -1) records[idx] = record
    else records.push(record)
    await writeAllToDisk(records)
    return record
  }

  async delete(id: string): Promise<void> {
    return this.enqueue(() => this.doDelete(id))
  }

  private async doDelete(id: string): Promise<void> {
    const records = await readAllFromDisk()
    const filtered = records.filter(r => r.id !== id)
    if (filtered.length !== records.length) {
      await writeAllToDisk(filtered)
    }
  }
}
