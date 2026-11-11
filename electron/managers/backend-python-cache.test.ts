/**
 * 解释器缓存模块：读-写-清三态 + 损坏文件按无缓存处理（与 desktop-prefs 同口径：
 * 缓存永远不该把启动打崩，也不该把脏值喂给 spawn）。
 */
import { mkdtempSync, rmSync, writeFileSync, existsSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearPythonCache,
  loadPythonCache,
  PYTHON_CACHE_FILE,
  savePythonCache,
  type PythonLaunch,
} from './backend-python-cache'

let dir = ''
beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'ckun-py-cache-')) })
afterEach(() => { rmSync(dir, { recursive: true, force: true }) })

/** savePythonCache 是异步原子写：轮询等待最多 1s 直到能解析出合法缓存值
 * （不能拿"文件存在"当信号——脏文件覆盖用例里文件从一开始就存在；
 * 同步 spin 也会把写链饿死，必须让出事件循环） */
async function waitForCache(d: string): Promise<PythonLaunch | null> {
  for (let i = 0; i < 20; i++) {
    const v = loadPythonCache(d)
    if (v) return v
    await new Promise((r) => setTimeout(r, 50))
  }
  return null
}

describe('backend-python-cache', () => {
  it('缺失 → null；round-trip 保真；等待异步写完成', async () => {
    expect(loadPythonCache(dir)).toBeNull()
    savePythonCache(dir, { cmd: 'python', prefixArgs: ['-3'] })
    savePythonCache(dir, (() => {}) as unknown as PythonLaunch) // 非法值不落盘（内部校验）
    expect(await waitForCache(dir)).toEqual({ cmd: 'python', prefixArgs: ['-3'] })
    clearPythonCache(dir)
    expect(existsSync(join(dir, PYTHON_CACHE_FILE))).toBe(false)
    expect(() => clearPythonCache(dir)).not.toThrow() // 幂等
  })
  it('损坏 JSON / 脏结构 → null 且可被覆盖', async () => {
    writeFileSync(join(dir, PYTHON_CACHE_FILE), '{oops')
    expect(loadPythonCache(dir)).toBeNull()
    writeFileSync(join(dir, PYTHON_CACHE_FILE), JSON.stringify({ cmd: 42, prefixArgs: 'x' }))
    expect(loadPythonCache(dir)).toBeNull()
    // 脏文件不阻碍覆盖：合法值写入后 round-trip 保真
    savePythonCache(dir, { cmd: 'python3', prefixArgs: [] })
    expect(await waitForCache(dir)).toEqual({ cmd: 'python3', prefixArgs: [] })
  })
})
