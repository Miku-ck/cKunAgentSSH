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
const child = spawn(builder, [...builderConfigArgs, ...builderPassThroughArgs], { stdio: 'inherit', shell: isWin })
child.on('exit', (code) => process.exit(code === null ? 1 : code))
child.on('error', (err) => {
  console.error('无法启动 electron-builder：', err.message)
  process.exit(1)
})