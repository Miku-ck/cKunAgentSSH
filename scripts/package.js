// 一键打包入口：自动注入 electron-builder 在中国网络/本机环境所需的配置，
// 再调用 electron-builder，避免每次手动设置环境变量。
const { spawn } = require('child_process')
const path = require('path')

const root = path.resolve(__dirname, '..')

// 1) 下载的二进制（nsis/winCodeSign 等）缓存到项目内，避免写入 AppData 受限/混乱
process.env.ELECTRON_BUILDER_CACHE =
  process.env.ELECTRON_BUILDER_CACHE || path.join(root, '.electron-cache')

// 2) 从国内镜像拉取 electron-builder 依赖的二进制，绕开 GitHub 直连慢/失败
process.env.ELECTRON_BUILDER_BINARIES_MIRROR =
  process.env.ELECTRON_BUILDER_BINARIES_MIRROR ||
  'https://npmmirror.com/mirrors/electron-builder-binaries/'

// 3) 让 Node 信任系统已安装的根证书，解决 "unable to verify the first certificate"
if (!process.env.NODE_OPTIONS || !process.env.NODE_OPTIONS.includes('use-system-ca')) {
  process.env.NODE_OPTIONS =
    (process.env.NODE_OPTIONS ? process.env.NODE_OPTIONS + ' ' : '') + '--use-system-ca'
}

const isWin = process.platform === 'win32'
const builder = path.join(root, 'node_modules', '.bin', isWin ? 'electron-builder.cmd' : 'electron-builder')

// 4) 内嵌 Python 运行时探测：仓库根 runtime/ 有实际内容（README.md 占位不算）
//    时追加 extraResources → resources/python-runtime/；否则警告并回退系统 Python。
const runtimeDir = path.join(root, 'runtime')
let runtimeReady = false
try {
  const fs = require('fs')
  runtimeReady =
    fs.existsSync(runtimeDir) &&
    fs
      .readdirSync(runtimeDir)
      .some((name) => name !== 'README.md')
} catch {
  runtimeReady = false
}
const extraArgs = process.argv.slice(2)
const builderConfigArgs = ['-c', 'electron-builder.yml'] // 显式主配置，保证与生成片段合并
if (runtimeReady) {
  console.log('[package] runtime/ 已就绪 → 打包内嵌 Python 运行时 (resources/python-runtime)')
  const fs = require('fs')
  const snippet = path.join(root, '.electron-builder.runtime.yml')
  fs.writeFileSync(
    snippet,
    'extraResources:\n' +
      '  - from: runtime\n' +
      '    to: python-runtime\n' +
      '    filter:\n' +
      "      - '**/*'\n" +
      "      - '!README.md'\n",
  )
  builderConfigArgs.push('-c', snippet)
} else {
  console.warn(
    '[package] 警告：runtime/ 为空 — 本次打包不内嵌 Python 运行时，' +
      '目标机需自装 Python（或依赖 CKUN_PYTHON 指定）。准备方式见 runtime/README.md',
  )
}
const child = spawn(builder, [...builderConfigArgs, ...extraArgs], { stdio: 'inherit', shell: isWin })
child.on('exit', (code) => process.exit(code === null ? 1 : code))
child.on('error', (err) => {
  console.error('无法启动 electron-builder：', err.message)
  process.exit(1)
})