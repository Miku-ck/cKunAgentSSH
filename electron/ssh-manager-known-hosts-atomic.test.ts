/**
 * known_hosts 落盘的原子性与"读不出就别写"。
 *
 * 原实现两处都缺：`writeFile` 直写目标文件（进程被杀在写中间就留下半个 JSON），
 * 而加载侧把「文件不存在」和「解析失败」合并成一个 catch —— 损坏后按空表起步，
 * 接下来的首次连接直接 TOFU 重新信任并覆写磁盘。后果不是"记录丢了"这么轻：
 * 主机密钥钉扎静默消失，中间人藏在一次半写后面，而用户看到的是"连接正常"。
 *
 * 同项目的 ssh-store / tunnel-store 早就用 tmp+rename + .corrupt 隔离，这条是
 * 最后一处没跟上同族纪律的持久化路径。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// TEST_DIR 在 body 里才赋值，而 vi.mock 的工厂在 './ssh-manager' 首次 import 'electron'
// 时才执行 —— getPath 的闭包到测试运行时才解析这个常量，时序上安全（顶层常量直接引用才会 TDZ）
const TEST_DIR = join(tmpdir(), 'ckun-known-hosts-test')

vi.mock('electron', () => ({
  BrowserWindow: class {},
  app: {
    getPath: () => TEST_DIR,
    getAppPath: () => TEST_DIR,
  },
}))

import { SSHManager } from './ssh-manager'

type HostsInternals = {
  knownHosts: Map<string, string>
  knownHostsPath: string
  loadKnownHosts: () => Promise<void>
  saveKnownHosts: () => Promise<void>
  knownHostsUnreadable: boolean
}

const internals = (mgr: SSHManager) => mgr as unknown as HostsInternals

let mgr: SSHManager
let hostsPath = ''

beforeEach(() => {
  rmSync(TEST_DIR, { recursive: true, force: true })
  mkdirSync(TEST_DIR, { recursive: true })
  mgr = new SSHManager()
  hostsPath = internals(mgr).knownHostsPath
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  rmSync(TEST_DIR, { recursive: true, force: true })
  vi.restoreAllMocks()
})

describe('known_hosts 损坏不得静默降级为可覆写', () => {
  it('半写损坏：隔离留证、按空表起步，且本次会话拒绝写盘', async () => {
    writeFileSync(hostsPath, '{"h1:22":"SHA256:abc",', 'utf-8') // 典型截断现场

    await internals(mgr).loadKnownHosts()

    expect(internals(mgr).knownHosts.size, '损坏表不得当可信记录用').toBe(0)
    expect(internals(mgr).knownHostsUnreadable).toBe(true)
    const baks = readdirSync(TEST_DIR).filter((f) => f.includes('.corrupt-') && f.endsWith('.bak'))
    expect(baks.length, '损坏原文必须留证').toBe(1)
    expect(readFileSync(join(TEST_DIR, baks[0]), 'utf-8')).toContain('SHA256:abc')

    // 内存里新增一条 TOFU 记录后保存：不许拿子集覆盖磁盘上可能仍存在的旧记录
    internals(mgr).knownHosts.set('h2:22', 'SHA256:def')
    await internals(mgr).saveKnownHosts()

    expect(existsSync(hostsPath), '读不出可信内容时不得写盘').toBe(false)
  })

  it('文件不存在（首次运行）：正常起步，保存能落盘且不留下 .tmp', async () => {
    await internals(mgr).loadKnownHosts()

    expect(internals(mgr).knownHostsUnreadable, 'ENOENT 不是损坏，不该禁写').toBe(false)

    internals(mgr).knownHosts.set('h1:22', 'SHA256:aaa')
    await internals(mgr).saveKnownHosts()

    const saved = JSON.parse(readFileSync(hostsPath, 'utf-8')) as Record<string, string>
    expect(saved['h1:22']).toBe('SHA256:aaa')
    expect(existsSync(hostsPath + '.tmp'), '原子写的临时文件不该残留').toBe(false)
  })

  it('目标目录被删空也要自愈（userData 下不该出现保存失败）', async () => {
    rmSync(TEST_DIR, { recursive: true, force: true })

    internals(mgr).knownHosts.set('h3:22', 'SHA256:ccc')
    await internals(mgr).saveKnownHosts()

    expect(JSON.parse(readFileSync(hostsPath, 'utf-8'))['h3:22']).toBe('SHA256:ccc')
  })

  it('健康文件照常读回', async () => {
    writeFileSync(hostsPath, JSON.stringify({ 'h9:22': 'SHA256:zzz' }), 'utf-8')

    await internals(mgr).loadKnownHosts()

    expect(internals(mgr).knownHosts.get('h9:22')).toBe('SHA256:zzz')
    expect(internals(mgr).knownHostsUnreadable).toBe(false)
    // 结构异常（数组）同样算不可信
    writeFileSync(hostsPath, '[]', 'utf-8')
    const mgr2 = new SSHManager()
    await internals(mgr2).loadKnownHosts()
    expect(internals(mgr2).knownHostsUnreadable).toBe(true)
  })
})
