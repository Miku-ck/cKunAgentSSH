/**
 * vitest 启动器（Windows 盘符大小写 workaround）。
 *
 * 上游 bug vitest-dev/vitest#11872：CLI 以小写盘符（d:/…）启动时，vitest 会
 * 加载两份自身 runtime（worker state 归属错位），所有测试文件在 describe()
 * 处报 "TypeError: Cannot read properties of undefined (reading 'config')"。
 * Git Bash / 部分 shell 会用小写盘符传 bin 路径，踩中与否只取决于 argv 里
 * bin 路径的大小写，与 cwd 无关。
 *
 * 解法：用 realpathSync.native 把 vitest.mjs 归一成系统真实大小写（D:\…）
 * 再重新拉起 CLI，其余参数原样透传。非 Windows 没有该 bug，直接透传。
 */
import { spawnSync } from 'node:child_process'
import { realpathSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import process from 'node:process'

const cli = fileURLToPath(new URL('../node_modules/vitest/vitest.mjs', import.meta.url))

if (process.platform !== 'win32') {
  await import(pathToFileURL(cli).href)
} else {
  const canonical = realpathSync.native(cli)
  const { status } = spawnSync(process.execPath, [...process.execArgv, canonical, ...process.argv.slice(2)], {
    stdio: 'inherit',
  })
  process.exit(status ?? 1)
}
