// 一键打包入口：自动注入 electron-builder 在中国网络/本机环境所需的配置，
// 再调用 electron-builder，避免每次手动设置环境变量。
const { spawn, spawnSync } = require('child_process')
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
// 直接以本 node 运行 electron-builder 的 CLI 入口（v26 package.json 的 bin 就是 ./cli.js），
// 不再走 node_modules/.bin 垫片：Windows 垫片是 .cmd，Node ≥18.20/20.12/21.7 出于
// CVE-2024-27980 禁止无 shell 地派生 .cmd，逼着开 shell:true —— 而 shell:true 会把全部
// 参数拼成一行交给 cmd.exe 解释，路径/透传参数里的元字符就是注入面。v26 的 .cmd 垫片
// 内容也仅是 `node "<…>/electron-builder/cli.js" %*`，改为 process.execPath + argv 数组 +
// shell:false 语义完全等价，还少一层 cmd.exe。POSIX 垫片同理省掉。
const builderEntry = path.join(root, 'node_modules', 'electron-builder', 'cli.js')

// 4) 内嵌 Python 运行时探测：按 runtime/README.md 的契约认那枚解释器可执行文件
//    （Windows runtime/python.exe，POSIX runtime/bin/python3），而不是「除 README.md 外还有
//    别的文件」—— 一个误落进来的日志/临时文件会让构建谎报「已就绪」，把只装着杂物的
//    resources/python-runtime/ 打进包。运行时解析侧本来就会 existsSync(python.exe) 兜底，
//    所以错的只是这条构建日志与这次多余拷贝。
//    ⚠️ POSIX 的路径必须与运行时解析侧一字不差：electron/backend-manager.ts 读的是
//    resources/python-runtime/bin/python3，而 from: runtime → to: python-runtime 是整目录
//    平移，所以构建期要认的也是 runtime/bin/python3。此前这里探的是 runtime/python3，
//    两边永远对不上 —— 探到的那次拷贝在包里没人能找到，没探到的那次又白打警告，
//    mac/linux 目标等于从来没有内嵌运行时。
const fs = require('fs')
const runtimeDir = path.join(root, 'runtime')
// electron-builder v26 的 dir 产物目录名（本机实装 app-builder-lib/out/platformPackager.js
// computeAppOutDir）：`${buildConfigurationKey}${getArchSuffix(arch)}${MAC ? '' : '-unpacked'}`，
// 其中 buildConfigurationKey 是 win/mac/linux，getArchSuffix 默认 arch（x64）为空串、
// 否则 `-<arch>`。即 win-unpacked / win-arm64-unpacked、linux-unpacked / linux-arm64-unpacked、
// mac / mac-arm64。此前对非 win 平台一律拼 `${process.platform}-unpacked`，darwin 会得到
// electron-builder 从不产出的 darwin-unpacked，mac 上清理残骸/定位产物永远扑空。
// （arch 仅取 process.arch 能出现的 x64/arm64；armv7l/universal 不在此脚本的处理面内。）
const platformBuildDirName = (platform = process.platform, arch = process.arch) => {
  const key = platform === 'win32' ? 'win' : platform
  const archSuffix = arch === 'x64' ? '' : `-${arch}`
  return platform === 'darwin' ? `mac${archSuffix}` : `${key}${archSuffix}-unpacked`
}
// dir 目标下 electron-builder 先把 Electron 运行时解到暂存目录 `${output}/产物目录.tmp`，
// 全部成功后才改名成正式产物目录。中途失败（实测 2026-10-23：electron 下载/解包阶段 EBUSY）
// 会把一个近乎空的 .tmp 壳留在 dist/ 里，而下一轮构建开机就要 unlink 它——只要残骸里任何一个
// 文件被外部句柄锁住，后续每一轮打包都会在同一处失败（失败会无限传染，直到手动清理）。
// 所以非零退出时顺手清掉暂存残骸；删不动（仍被锁）就显式警告，把根因摆在明处而不是留给下轮猜。
const unpackedStaging = () => path.join(root, 'dist', platformBuildDirName() + '.tmp')
const cleanupStaging = () => {
  const dir = unpackedStaging()
  if (!fs.existsSync(dir)) return
  try {
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 300 })
    console.log(`[package] 已清理失败构建残骸：${dir}`)
  } catch (e) {
    console.warn(
      `[package] 残骸清理失败（${e.message}）：${dir} 仍被其它进程占用，` +
        '下一轮打包会因 EBUSY 在同一处失败，请重启相关占用进程/机器后手动删除该目录',
    )
  }
}
const runtimeProbe = isWin
  ? path.join(runtimeDir, 'python.exe')
  : path.join(runtimeDir, 'bin', 'python3')
const runtimeReady = fs.existsSync(runtimeProbe)
const extraArgs = process.argv.slice(2)
// --allow-missing-runtime 是给"只想看看流水线其余部分跑不跑得通"的人留的逃生口，
// 必须显式写出来 —— 默认路径不再接受缺运行时。
const allowMissingRuntime = extraArgs.includes('--allow-missing-runtime')
const builderPassThroughArgs = extraArgs.filter((a) => a !== '--allow-missing-runtime')
// 只允许传一份配置。两个 `-c` 会被 yargs 收成数组，electron-builder 把数组里的字符串
// 折叠成 `extends = 最后一项`（builder.js:106-120），packager.js:256-258 再把这个字符串
// 当成唯一配置文件读 —— 主配置整份被丢弃：files 白名单、asarUnpack、appId、win、nsis、
// portable 全都不生效，`files: []` 意味着 app.asar 里没有 out/main/index.js，原生 .node
// 也没被解包。而 directories.output 仍是 dist，CI 的 dist/*.exe 上传照样命中，绿色流水线
// 可以把一个装上去谁也起不来的包一路抬进发布物。
// 内嵌运行时改走配置自身的 `extends`（load.js 的 loadParentConfigsRecursively 会把父配置
// 合回来），合并语义由 electron-builder 负责，不再靠参数顺序赌。
let builderConfigArgs = ['-c', 'electron-builder.yml'] // 显式主配置，保证与生成片段合并
if (runtimeReady) {
  console.log('[package] runtime/ 已就绪 → 打包内嵌 Python 运行时 (resources/python-runtime)')
  const snippet = path.join(root, '.electron-builder.runtime.yml')
  fs.writeFileSync(
    snippet,
    "extends: './electron-builder.yml'\n" +
      'extraResources:\n' +
      '  - from: runtime\n' +
      '    to: python-runtime\n' +
      '    filter:\n' +
      "      - '**/*'\n" +
      "      - '!README.md'\n",
  )
  builderConfigArgs = ['-c', snippet]
} else if (!allowMissingRuntime) {
  // 硬失败而不是 warning：AI 侧要跑后端，后端要一个装了 fastapi/uvicorn 的 Python。
  // 以前这里打一行警告照常出包，于是"能出一个装到别人机器上 AI 全灭的包"，
  // 而且从产物上看不出任何异常。取舍理由记在 git history 的 ADR-0002。
  console.error(
    '[package] 缺少内嵌 Python 运行时：' + runtimeProbe + ' 不存在。\n' +
      '  发布包必须自带 Python，否则装到目标机上 AI 侧直接不可用。\n' +
      '  · CI 由「Prepare embedded Python runtime」一步自动准备（见 .github/workflows/ci.yml）\n' +
      '  · 本地准备方式见 runtime/README.md\n' +
      '  · 确实要出无运行时的包（排障/试流水线）：加 --allow-missing-runtime',
  )
  process.exit(1)
} else {
  console.warn(
    '[package] 警告（--allow-missing-runtime）：本次不内嵌 Python 运行时，' +
      '产出的 app 依赖目标机自装 Python（或用 CKUN_PYTHON 指定）。不要把这个包发给用户。',
  )
}
// argv 数组 + shell:false：这里的参数都是独立词元（`-c` 与其后的配置文件路径、
// --allow-missing-runtime、其余透传 argv 原样为单个元素），没有需要 shell 展开的
// glob/管道/引号内多词；配置里的 files/extraResources glob 由 electron-builder
// 自身展开，与 shell 无关。透传参数因此不再有任何被 cmd.exe/sh 解释的机会。
const child = spawn(process.execPath, [builderEntry, ...builderConfigArgs, ...builderPassThroughArgs], { stdio: 'inherit', shell: false })
const version = require(path.join(root, 'package.json')).version
child.on('exit', (code) => {
  if (code !== 0) {
    cleanupStaging()
    process.exit(code === null ? 1 : code)
  }
  // tar-only 出件：解包目录直发。Windows 10+ 自带 tar.exe（bsdtar），
  // POSIX 用系统 tar；找不到即失败退出——绝不出"没有发布物"的绿色流水线。
  const tarBin = process.platform === 'win32' ? 'tar.exe' : 'tar'
  const unpacked = path.join(root, 'dist', platformBuildDirName())
  const artifact = path.join(root, 'dist', `ckunagentssh-${version}-${process.platform}-x64.tar.gz`)
  if (!fs.existsSync(unpacked)) {
    console.error(`[package] 未找到 dir 产物目录：${unpacked}`)
    process.exit(1)
  }
  const r = spawnSync(tarBin, ['-czf', artifact, '-C', unpacked, '.'], { stdio: 'inherit', shell: isWin })
  if (r.error || r.status !== 0) {
    console.error('[package] tar 出件失败：', r.error ? r.error.message : `exit=${r.status}`)
    process.exit(1)
  }
  console.log(`[package] tar 产物：${artifact}`)
  process.exit(0)
})
child.on('error', (err) => {
  console.error('无法启动 electron-builder：', err.message)
  process.exit(1)
})