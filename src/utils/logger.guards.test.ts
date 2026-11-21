/**
 * 裸 console 防回潮守卫（P2-1 收口）。
 *
 * 渲染层日志已收敛到 utils/logger.ts（统一 [tag] 前缀、DEBUG 开关、且不改底层
 * console 方法以保持主进程落盘行为）。本测试防止新代码继续散落裸 console.*：
 * 允许极少数刻意的例外（本文件自身、logger.ts 实现、测试文件），其余必须走 logger。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SRC_ROOT = fileURLToPath(new URL('..', import.meta.url))

function collectSourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      out.push(...collectSourceFiles(full))
    } else if (/\.(ts|vue)$/.test(name) && !/\.(test|spec)\.ts$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

describe('渲染层裸 console 收敛', () => {
  it('生产源码中不残留 console.warn/error（这些必须走 logger）', () => {
    const offenders: string[] = []
    for (const file of collectSourceFiles(SRC_ROOT)) {
      if (file.replace(/\\/g, '/').endsWith('/utils/logger.ts')) continue  // 实现本身必然用 console
      const src = readFileSync(file, 'utf8')
      // 去掉行注释与块注释里的 "console" 提及，只数真实调用
      const code = src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\/\/.*$/gm, '')
      if (/console\.(warn|error)\s*\(/.test(code)) {
        offenders.push(file.replace(SRC_ROOT, ''))
      }
    }
    expect(offenders).toEqual([])
  })

  it('logger.ts 用一对一 console 方法映射（不改级别语义）', () => {
    const src = readFileSync(new URL('./logger.ts', import.meta.url), 'utf-8')
    expect(src).toContain('console.info(prefix, ...args)')
    expect(src).toContain('console.warn(prefix, ...args)')
    expect(src).toContain('console.error(prefix, ...args)')
    expect(src).toContain('console.debug(prefix, ...args)')
  })
})
