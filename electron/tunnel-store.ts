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
import { join } from 'path'
import { JsonFileStore } from './utils/json-file-store'
import { isPort, portRangeError } from '../shared/port-core'

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
   * local:   本机 listenPort → ( SSH) → dstHost:dstPort
   * remote:  远端 bindPort   → ( SSH回) → 本机 dstHost:dstPort
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

/** 数据面身份：-R 查重按它比，SSH 连接与 TLS 网关各自独立 */
const planeOf = (r: TunnelRule): string => (r.via === 'gateway' ? `gw:${r.gatewayId ?? ''}` : `ssh:${r.connectionId ?? ''}`)
const planeLabel = (r: TunnelRule): string => (r.via === 'gateway' ? '网关' : '连接')

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
    const port = rule.listenPort
    if (!isPort(port)) throw new Error(portRangeError('本地监听'))
  }
  if (rule.type === 'local' || rule.type === 'remote') {
    if (typeof rule.dstHost !== 'string' || !rule.dstHost.trim()) throw new Error('必须填写目标主机')
    if (!isPort(rule.dstPort)) throw new Error(portRangeError('目标'))
  }
  if (rule.type === 'remote') {
    if (!isPort(rule.bindPort)) throw new Error(portRangeError('远端绑定'))
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
      // 跨数据面的服务端占用由启动时报错兜底；同一条数据面上的同绑定端点在这里拦截。
      // 判据必须含数据面身份：两条网关规则若只比 connectionId，两边都是 undefined，
      // 会把「不同网关上的同一个端口」误判成重复（那两台机器互不相干）。
      if (planeOf(other) === planeOf(rule) && `${(other.remoteBindHost || '127.0.0.1').toLowerCase()}:${other.bindPort}` === bindKey) {
        throw new Error(`远端绑定 ${bindKey} 已被同一条${planeLabel(rule)}的其他隧道使用`)
      }
    }
  }
}

export class TunnelStore {
  /** 骨架收编（ARCH-007）：写队列/原子写/损坏隔离/读失败上抛统一走 JsonFileStore */
  private readonly file = new JsonFileStore<TunnelRule>({
    path: getStorePath(),
    logTag: '[TunnelStore]',
    label: '隧道规则',
  })

  /** 读-改-写串行化（防并发写互相覆写丢更新），委托 JsonFileStore 队列 */
  private enqueue<T>(op: () => Promise<T>): Promise<T> {
    return this.file.enqueue(op)
  }

  async list(): Promise<TunnelRule[]> {
    try {
      return await this.file.readAll()
    } catch (e) {
      console.error('[TunnelStore] list 读取失败，降级为空列表:', e)
      return []
    }
  }

  async get(id: string): Promise<TunnelRule | null> {
    const records = await this.file.readAll()
    return records.find(r => r.id === id) ?? null
  }

  async save(input: TunnelRule): Promise<TunnelRule> {
    return this.enqueue(() => this.doSave(input))
  }

  private async doSave(input: TunnelRule): Promise<TunnelRule> {
    const records = await this.file.readAll()
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
    await this.file.writeAll(records)
    return record
  }

  async delete(id: string): Promise<void> {
    return this.enqueue(() => this.doDelete(id))
  }

  private async doDelete(id: string): Promise<void> {
    const records = await this.file.readAll()
    const filtered = records.filter(r => r.id !== id)
    if (filtered.length !== records.length) {
      await this.file.writeAll(filtered)
    }
  }
}
