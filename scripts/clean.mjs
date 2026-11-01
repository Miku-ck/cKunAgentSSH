// 清理 1) electron-builder 打包产物（dist / dist-*）与中间配置
//       2) 解释器/工具在本机攒下的测试与 lint 缓存、临时校验目录
import { existsSync, readdirSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'

const root = process.cwd()

// 产物目录按形态识别，不写死名字：electron-builder 的输出目录可以由
// `-c.directories.output=` 指到任意 dist* 名下，本机就攒过 dist-app / dist-app2 /
// dist-final / dist-verify 这些试错残留。写死清单等于每换一次名字就漏下一个
// 300 MB 量级的目录（每个都含完整 electron 运行时）。
const isArtifactDir = (name) =>
  name === 'dist' || name === 'dist-electron' || /^dist-[a-z0-9-]+$/.test(name)

const isDir = (fp) => {
  try {
    return statSync(fp).isDirectory()
  } catch {
    return false
  }
}

// 可再生的缓存/临时目录名：全部由工具自己建，删掉最坏代价是下次跑慢一点。
// 按名字识别而不是写死路径 —— 此前的平铺清单只列了 backend/、gateway/ 两层，
// pytest 一跑就在 backend/agent、backend/routes、backend/tests、scripts/deploy
// 等每个包目录下攒一份 __pycache__，清单永远列不全，等于每次清理都只删掉零头。
const cacheDirNames = /^(?:__pycache__|\.pytest_cache|\.ruff_cache|\.tmp-check|pytest-cache-files-.+)$/

// 不下探的目录：
//   runtime/    内嵌 Python 发布物（scripts/prepare-runtime.ps1 制备，重备一次要
//               下载几百 MB；且 scripts/package.js 会往里预编译 .pyc，见 §10）
//   node_modules 第三方包，不是本项目的产物
//   .git         钩子若碰巧命中同名目录，代价是仓库本身
const pruneDirNames = new Set(['runtime', 'node_modules', '.git'])

// 有内容的工作目录，与缓存不同性质，刻意不碰：
// designs/（UI 概念稿）、.superpowers/（brainstorm 产物）、各 agent 工具自己的目录
const collectCacheDirs = (dir, depth = 0) => {
  if (depth > 8) return []
  const found = []
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return found
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue
    if (pruneDirNames.has(entry.name)) continue
    const fp = join(dir, entry.name)
    if (cacheDirNames.test(entry.name)) {
      found.push(fp)
      continue // 缓存目录整体删除，不必再往里看
    }
    found.push(...collectCacheDirs(fp, depth + 1))
  }
  return found
}

const targets = [
  ...readdirSync(root).filter((n) => isArtifactDir(n) && isDir(join(root, n))),
  // package.js 生成的临时构建配置
  '.electron-builder.runtime.yml',
  ...collectCacheDirs(root).map((fp) => fp.slice(root.length + 1).replaceAll('\\', '/')),
]

let deleted = 0
for (const t of targets) {
  const fp = join(root, t)
  if (existsSync(fp)) {
    try {
      rmSync(fp, { recursive: true, force: true })
      console.log('  removed:', t)
      deleted++
    } catch (e) {
      // 本机实测：杀软会长时间独占 win-unpacked 里的 *.asar（EBUSY 跨 20 分钟不释放），
      // 这里失败不代表脚本写错了 —— 换个时间再跑，或给工作目录加实时扫描排除。
      console.warn('  failed:', t, '-', e.message)
    }
  }
}
console.log(`\n${deleted} target(s) cleaned.`)
