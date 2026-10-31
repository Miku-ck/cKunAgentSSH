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

// 可再生的缓存/临时目录：全部由工具自己建，删掉最坏代价是下次跑慢一点。
// 刻意不含 runtime/（内嵌 Python 发布物，scripts/prepare-runtime.ps1 制备，
// 重备一次要下载几百 MB）与 designs/、.superpowers/ 这类有内容的工作目录。
const cacheTargets = [
  '.pytest_cache',
  '.ruff_cache',
  '.tmp-check', // 手工校验时的临时落地目录，非交付物
  'backend/.pytest_cache',
  'backend/.ruff_cache',
  'backend/__pycache__',
  'gateway/__pycache__',
]

const targets = [
  ...readdirSync(root).filter((n) => isArtifactDir(n) && isDir(join(root, n))),
  // package.js 生成的临时构建配置
  '.electron-builder.runtime.yml',
  ...cacheTargets,
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
