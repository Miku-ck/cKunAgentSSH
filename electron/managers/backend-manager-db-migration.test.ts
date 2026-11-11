/**
 * S0-08 回归：遗留库迁移必须整族搬走 WAL 边车，且不留半迁移态。
 *
 * 后端跑 `PRAGMA journal_mode=WAL`，已提交事务可以合法地只存在于 `-wal` 里。
 * 原实现只 rename 主 `.db`，SQLite 于是在目标目录新建空 `-wal`，旧目录那份
 * wal 成为孤儿 —— 丢的是 SSH 凭据、agent 记忆、定时任务，而且下次启动
 * `existsSync(targetPath)` 直接早退，把半迁移态永久锁死，用户看不到任何错误。
 */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// getPath 在本组的两个被测点里都不会被调用（只 ensureDbPath 用它），所以桩不必
// 引外部变量 —— vi.mock 的工厂会被提前到 import 之前求值，引用顶层 import 会踩 TDZ。
vi.mock('electron', () => ({
  app: { getPath: () => '', isPackaged: false },
}))

import { BackendManager } from './backend-manager'

let root = ''
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'ckun-db-mig-'))
})
afterEach(() => {
  vi.restoreAllMocks()
})

const move = (family: string[], targetDir: string, targetPath: string) =>
  // moveDbFamily 是编译期 private，运行时可直接取（只为测语义，不是后门）
  (BackendManager as unknown as {
    moveDbFamily: (f: string[], d: string, p: string) => void
  }).moveDbFamily(family, targetDir, targetPath)

describe('legacyDbFamily —— 一次迁移要搬哪些文件', () => {
  it('三件都在就三件一起搬', () => {
    const legacy = join(root, 'ckunagentic.db')
    writeFileSync(legacy, 'main')
    writeFileSync(`${legacy}-wal`, 'wal-contains-committed-tx')
    writeFileSync(`${legacy}-shm`, 'shm')
    expect(BackendManager.legacyDbFamily(legacy)).toEqual([
      legacy, `${legacy}-wal`, `${legacy}-shm`,
    ])
  })

  it('没有边车时只有主库（checkpoint 过的库）', () => {
    const legacy = join(root, 'ckunagentic.db')
    writeFileSync(legacy, 'main')
    expect(BackendManager.legacyDbFamily(legacy)).toEqual([legacy])
  })

  it('绝不列出不存在的文件（否则 copyFileSync 直接抛）', () => {
    const onlyWal = join(root, 'onlywal', 'ckunagentic.db')
    mkdirSync(join(root, 'onlywal'))
    writeFileSync(onlyWal, 'main')
    writeFileSync(`${onlyWal}-wal`, 'wal')
    expect(BackendManager.legacyDbFamily(onlyWal)).toEqual([onlyWal, `${onlyWal}-wal`])
  })
})

describe('moveDbFamily —— 整族搬迁', () => {
  it('wal 里的最新事务跟着走，源文件不再留在原地', () => {
    const legacy = join(root, 'old', 'ckunagentic.db')
    mkdirSync(join(root, 'old'))
    writeFileSync(legacy, 'main')
    writeFileSync(`${legacy}-wal`, 'the-only-copy-of-your-credentials')
    const targetDir = join(root, 'new')
    mkdirSync(targetDir)
    const targetPath = join(targetDir, 'ckunagentic.db')

    move(BackendManager.legacyDbFamily(legacy), targetDir, targetPath)

    expect(readFileSync(join(targetDir, 'ckunagentic.db-wal'), 'utf-8'))
      .toBe('the-only-copy-of-your-credentials')
    expect(existsSync(legacy)).toBe(false)
    expect(existsSync(`${legacy}-wal`)).toBe(false)
    expect(readdirSync(targetDir).filter((f) => f.endsWith('.part'))).toEqual([])
  })

  it('中途拷贝失败时目标主库不出现，也不留下 .part —— 下次启动能重试', () => {
    const legacy = join(root, 'old2', 'ckunagentic.db')
    mkdirSync(join(root, 'old2'))
    writeFileSync(legacy, 'main')
    // 把 wal 做成「目录」：copyFileSync 对它必然抛，用来模拟搬迁中途失败
    mkdirSync(`${legacy}-wal`)
    const targetDir = join(root, 'new2')
    mkdirSync(targetDir)
    const targetPath = join(targetDir, 'ckunagentic.db')

    expect(() => move(BackendManager.legacyDbFamily(legacy), targetDir, targetPath)).toThrow()

    expect(existsSync(targetPath)).toBe(false)
    expect(readdirSync(targetDir)).toEqual([])
    // 源库完好，下一次启动仍会尝试迁移（原实现的 rename-主文件 会把主库搬空）
    expect(existsSync(legacy)).toBe(true)
  })
})
