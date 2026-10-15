/**
 * JsonFileStore — JSON 数组持久化骨架的单一实现源（ARCH-007 收编）
 *
 * 此前 ssh-store / tunnel-store / gateway-store 三份逐字同构的骨架各自维护：
 * 写队列串行化（防并发读改写互相覆写丢更新）、tmp+rename 原子写（含 Windows
 * EPERM/EACCES/EBUSY 重试一次后回退直写）、损坏文件隔离（.corrupt-<ts>.bak，
 * rename 失败退回复制）、读失败上抛（E-2：吞成空表会让下一次写以空数组覆写全库）。
 * 修一处漏两处的成本已实际发生过 —— 本类只收编这四块纯骨架，域逻辑（字段加解密、
 * 记录校验、strip）留在各 store。
 *
 * 刻意不 import electron：文件路径由调用方解析后传入，本类可脱离 Electron 单测。
 * 有效性判据默认「对象且 id 为 string」（三店同款，外部编辑/非原子写残留的防御）。
 */
import { existsSync } from 'fs'
import { readFile, writeFile, rename, mkdir, unlink } from 'fs/promises'
import { dirname } from 'path'

export interface JsonFileStoreOptions<T> {
  /** 存储文件绝对路径（调用方用 app.getPath('userData') 解析） */
  path: string
  /** 日志前缀，如 '[SSHStore]' */
  logTag: string
  /** 域内资源名，用于读失败的向上抛文案，如 'SSH 连接配置' */
  label: string
  /** 记录有效性判据；缺省为「对象且 id 为 string」 */
  isValidRecord?: (r: unknown) => r is T
}

export class JsonFileStore<T extends { id: string }> {
  private readonly path: string
  private readonly logTag: string
  private readonly label: string
  private readonly isValid: (r: unknown) => r is T
  /** 读-改-写串行化队列：所有写操作串在同一 promise 链上，前一个失败不阻断后续 */
  private writeQueue: Promise<unknown> = Promise.resolve()

  constructor(opts: JsonFileStoreOptions<T>) {
    this.path = opts.path
    this.logTag = opts.logTag
    this.label = opts.label
    this.isValid = opts.isValidRecord ?? defaultIsValid
  }

  /** 把写操作串入队列；前一个操作失败不阻断后续（各自重试语义不变） */
  enqueue<R>(op: () => Promise<R>): Promise<R> {
    const run = this.writeQueue.then(op, op)
    // 吞掉队列链本身的错误，防止前一个操作的 reject 污染后续操作的链
    this.writeQueue = run.then(() => undefined, () => undefined)
    return run
  }

  /**
   * 读取全量记录。readFile 失败【向上抛】（E-2：按空表返回会让随后的
   * save/delete 以空数组为基线覆写磁盘，用户全部记录被静默永久抹掉）；
   * 写路径让错误自然上抛（IPC reject，用户重试即可）。
   * JSON 损坏（非数组 / 解析失败）隔离原文件后按空表返回，保留现场供恢复。
   */
  async readAll(): Promise<T[]> {
    if (!existsSync(this.path)) return []
    let raw: string
    try {
      raw = await readFile(this.path, 'utf-8')
    } catch (e) {
      throw new Error(
        `读取${this.label}失败（文件可能被占用或权限不足，请重试）: ${e instanceof Error ? e.message : String(e)}`,
      )
    }
    try {
      const arr = JSON.parse(raw)
      if (!Array.isArray(arr)) {
        console.warn(`${this.logTag} store 文件内容不是数组，已隔离损坏文件`)
        await this.quarantine(raw)
        return []
      }
      const valid = arr.filter(this.isValid)
      if (valid.length !== arr.length) {
        console.warn(`${this.logTag} 含 ${arr.length - valid.length} 条无 id 损坏条目，已跳过`)
      }
      return valid
    } catch (e) {
      // 损坏文件隔离到 .corrupt-<ts>.bak：原实现 JSON.parse 失败即静默返回 []，
      // 随后的 save()/delete() 会以空数组为基线覆写磁盘且无任何日志线索
      console.error(`${this.logTag} ${this.label}损坏（JSON 解析失败），已隔离原文件:`, e)
      await this.quarantine(raw)
      return []
    }
  }

  /** 原子写入：先写临时文件再 rename 替换。Windows 上 rename 到已存在目标可能
   * EPERM/EACCES/EBUSY（杀毒/索引服务短暂占用）：等待 200ms 重试一次，仍失败
   * 回退直写目标文件 —— 丢原子性但保证持久化。 */
  async writeAll(records: T[]): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true })
    const tmpPath = this.path + '.tmp'
    await writeFile(tmpPath, JSON.stringify(records, null, 2), 'utf-8')
    try {
      await rename(tmpPath, this.path)
    } catch (err: any) {
      if (err && (err.code === 'EPERM' || err.code === 'EACCES' || err.code === 'EBUSY')) {
        await new Promise(r => setTimeout(r, 200))
        try {
          await rename(tmpPath, this.path)
          return
        } catch {
          console.warn(`${this.logTag} rename 重试仍失败，回退到直接写入`)
          await writeFile(this.path, JSON.stringify(records, null, 2), 'utf-8')
          try { await unlink(tmpPath) } catch { /* ignore */ }
        }
      } else {
        throw err
      }
    }
  }

  /** 把损坏的 store 文件移走（或复制）为带时间戳的 .bak，保留现场供恢复 */
  private async quarantine(raw: string): Promise<void> {
    try {
      const bak = `${this.path}.corrupt-${Date.now()}.bak`
      try {
        await rename(this.path, bak)
      } catch {
        // rename 失败（文件被占用等）：退回复制保留原文件
        await writeFile(bak, raw, 'utf-8')
      }
      console.warn(`${this.logTag} 损坏文件已备份到: ${bak}`)
    } catch (e) {
      console.warn(`${this.logTag} 备份损坏文件失败:`, e)
    }
  }
}

function defaultIsValid<T extends { id: string }>(r: unknown): r is T {
  return !!r && typeof r === 'object' && typeof (r as { id?: unknown }).id === 'string'
}
