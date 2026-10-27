/// <reference types="node" />
/**
 * 「全仓不许再写 X」这类守卫的共用工具。
 *
 * vitest 对 .css 的 `?raw` 返回空模块（见 vitest.config.ts 的 css.include 说明），
 * 所以扫描源码只能走 node:fs 直读；几个守卫各抄一份 walk 会立刻分叉。
 */
import { readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, relative } from 'node:path'

/** 仓库的 src 目录（本文件在 src/styles 下）。 */
export const SRC = fileURLToPath(new URL('..', import.meta.url))

/** 递归列出 src 下所有 .vue/.css 的绝对路径。 */
export function walk(dir: string = SRC): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      if (name === 'node_modules') continue
      out.push(...walk(full))
    } else if (name.endsWith('.vue') || name.endsWith('.css')) {
      out.push(full)
    }
  }
  return out
}

/** 相对 src 的斜杠路径，断言失败时 `.vue/.css` 名字一眼能认。 */
export function rel(file: string): string {
  return relative(SRC, file).replace(/\\/g, '/')
}
