/**
 * JsonFileStore 骨架行为单测（ARCH-007）。
 *
 * 此前 ssh/tunnel/gateway 三份同构骨架都没有直接测试——收编成单一实现源后，
 * 骨架自身的行为（损坏隔离、无效条目过滤、读失败上抛、写队列不互相污染）
 * 必须有自己的回归钉子。零 electron 依赖，临时目录直接跑真 fs。
 * 每个用例独立的 store 文件，避免时间戳 .bak 计数相互耦合。
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync, mkdirSync, readdirSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { JsonFileStore } from './json-file-store'

interface Rec { id: string; v?: number }

let dir: string
let seq = 0

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), 'jfs-test-'))
})
afterAll(() => { rmSync(dir, { recursive: true, force: true }) })

/** 每用例独立 store 文件，隔离 .corrupt-*.bak 的可见范围 */
function makeStore(label = '测试记录'): { store: JsonFileStore<Rec>; path: string } {
  const path = join(dir, `store-${++seq}.json`)
  return { store: new JsonFileStore<Rec>({ path, logTag: '[JFS-Test]', label }), path }
}
const baksFor = (path: string): string[] =>
  readdirSync(dir).filter(f => f.startsWith(`${path.split(/[\\/]/).pop()}.corrupt-`))

describe('JsonFileStore', () => {
  it('writeAll → readAll 往返一致（落盘形态为缩进 JSON）', async () => {
    const { store, path } = makeStore()
    const recs: Rec[] = [{ id: 'a', v: 1 }, { id: 'b', v: 2 }]
    await store.writeAll(recs)
    expect(await store.readAll()).toEqual(recs)
    expect(readFileSync(path, 'utf-8')).toContain('"id": "a"')
  })

  it('JSON 损坏：隔离为 .corrupt-<ts>.bak 保留原文，按空表返回，随后可正常重写', async () => {
    const { store, path } = makeStore()
    writeFileSync(path, '{broken', 'utf-8')
    expect(await store.readAll()).toEqual([])
    const baks = baksFor(path)
    expect(baks.length).toBe(1)
    expect(readFileSync(join(dir, baks[0]), 'utf-8')).toBe('{broken')
    await store.writeAll([{ id: 'c' }])
    expect(await store.readAll()).toEqual([{ id: 'c' }])
  })

  it('合法数组内混入无 id / null 条目：过滤并保留有效记录', async () => {
    const { store, path } = makeStore()
    writeFileSync(path, JSON.stringify([{ id: 'ok' }, null, { nope: 1 }, { id: 'ok2' }]), 'utf-8')
    expect(await store.readAll()).toEqual([{ id: 'ok' }, { id: 'ok2' }])
  })

  it('非数组 JSON：rename 隔离原文件（移走而非保留）并返回空表', async () => {
    const { store, path } = makeStore()
    writeFileSync(path, '{"not":"array"}', 'utf-8')
    expect(await store.readAll()).toEqual([])
    expect(existsSync(path)).toBe(false)
    expect(baksFor(path).length).toBe(1)
  })

  it('readFile 失败（路径是目录）向上抛且文案含域内 label —— 防空表覆写全库', async () => {
    const dirPath = join(dir, `as-file-${++seq}`)
    mkdirSync(dirPath)
    const s2 = new JsonFileStore<Rec>({ path: dirPath, logTag: '[JFS-Test2]', label: '网关记录' })
    await expect(s2.readAll()).rejects.toThrow('读取网关记录失败')
  })

  it('队列：前一个写操作 reject 不阻断后续（各自重试语义独立）', async () => {
    const { store } = makeStore()
    const seen: string[] = []
    const boom = store.enqueue(async () => { seen.push('first'); throw new Error('first fails') })
    const second = store.enqueue(async () => { seen.push('second'); return 'ok' })
    await expect(boom).rejects.toThrow('first fails')
    await expect(second).resolves.toBe('ok')
    expect(seen).toEqual(['first', 'second'])
  })
})
