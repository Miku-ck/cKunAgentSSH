// 清理 electron-builder 打包产物（dist / dist-*）和中间配置
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

const targets = [
  ...readdirSync(root).filter((n) => isArtifactDir(n) && isDir(join(root, n))),
  // package.js 生成的临时构建配置
  '.electron-builder.runtime.yml',
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
