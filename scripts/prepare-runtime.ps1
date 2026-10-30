<#
.SYNOPSIS
    制备内嵌 Python 运行时（仓库根 runtime/），供 npm run package 打进 resources/python-runtime/。

.DESCRIPTION
    本脚本是「内嵌解释器」唯一的制备入口：本地与 CI 的 check-windows job 都调它，
    否则会出现"CI 能出包、本机出不了"这种只能靠猜的两套逻辑。步骤：
      1. 从 astral-sh/python-build-standalone 取 install_only_stripped 的可重定位 CPython
         （自带 python.exe + Lib/，不需要像 Windows embeddable 那样手改 ._pth 再塞 pip）；
      2. 解压到 runtime/ —— 落位口径必须与两处一字不差：
         electron/backend-manager.ts resolvePythonCommand 的 bundled 分支、scripts/package.js 的探测；
      3. pip install -r backend/requirements.txt（与 CI 跑后端测试用的是同一份清单）；
      4. 用 checked-hash 模式预编译 site-packages 字节码 —— electron-builder 拷贝会重写源文件
         mtime，mtime 模式的 .pyc 会整批失效，代价是目标机每次启动重新编译
         （与 backend/__pycache__ 同一教训，见 electron-builder.yml 的注释）；
      5. 冒烟导入 + 写 runtime/VERSION（解释器版本 / 上游资产名 / requirements 指纹）。

    幂等：runtime/python.exe 已存在时跳过下载解压，只重跑依赖与字节码，可反复执行。
    runtime/ 已被 .gitignore 忽略，是纯构建产物，不含任何用户数据。

.PARAMETER PythonVersion
    期望的 Python 小版本（默认 3.11）。与 .pyc ABI、CI 的 setup-python 保持同档。

.PARAMETER ReleaseTag
    固定上游 release tag（默认 latest，按资产名通配匹配）。
    写死小版本号会在上游发版后悄悄 404，所以默认走 latest。

.PARAMETER IndexUrl
    pip 源，默认沿用 PIP_INDEX_URL 环境变量（国内网络用）。

.PARAMETER AssetUrl
    直接给出上游 tar.gz 的下载地址（镜像/代理），跳过 api.github.com 解析。

.PARAMETER Archive
    已下载好的本地 tar.gz 路径。内网/离线环境下用它，完全不碰网络。

.PARAMETER Force
    删除并重建 runtime/（重新下载解压）。依赖与字节码重跑不需要它。

.PARAMETER SkipDeps
    只准备解释器，不装依赖。排障用，产出的包不可发布。

.PARAMETER SkipSiteCompile
    不预编译 site-packages 字节码。省体积，代价是首启多花一次编译（约 MB 换百毫秒级）。

.NOTES
    GH_TOKEN 存在时用于 GitHub API 鉴权：裸调 api.github.com 按 IP 限流，
    共享 runner 上会随机红，而失败点看起来像"上游没资产"。
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.11',
    [string]$ReleaseTag = 'latest',
    [string]$IndexUrl = $env:PIP_INDEX_URL,
    [string]$AssetUrl = '',
    [string]$Archive = '',
    [switch]$Force,
    [switch]$SkipDeps,
    [switch]$SkipSiteCompile
)

$ErrorActionPreference = 'Stop'
# 进度条在管道/日志里既刷屏又把请求拖慢一个量级
$ProgressPreference = 'SilentlyContinue'

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Path {
    param([string]$Path, [string]$Hint)
    if (-not (Test-Path -LiteralPath $Path)) { throw "缺少 $Path。$Hint" }
}

# 原生命令统一出口：非零退出立刻失败并回显完整命令行。
# 没有这一层时，"pip 装了一半"或"tar 解压中断"会被之后的 Test-Path 掩盖成别的错误。
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList)
    Write-Host "    $Exe $($ArgList -join ' ')" -ForegroundColor DarkGray
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { throw "$Exe 退出码 $LASTEXITCODE：$($ArgList -join ' ')" }
}

if ($env:OS -ne 'Windows_NT') {
    # electron-builder.yml 已移除 mac/linux 目标（交付口径 Windows-only），这里不做 POSIX 分支
    throw 'prepare-runtime.ps1 只处理 Windows 交付物'
}

$root         = Split-Path -Parent $PSScriptRoot
$runtimeDir   = Join-Path $root 'runtime'
$pythonExe    = Join-Path $runtimeDir 'python.exe'
$sitePackages = Join-Path $runtimeDir 'Lib\site-packages'
$requirements = Join-Path $root 'backend\requirements.txt'
$versionFile  = Join-Path $runtimeDir 'VERSION'

Assert-Path $requirements '内嵌运行时就是为它准备的，缺清单无法制备'
if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
    throw '需要 tar.exe 解压上游资产（Windows 10+ 自带 bsdtar）'
}
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw '需要 curl.exe 下载上游资产（Windows 10+ 自带）'
}

# 上一次的 VERSION：复用解释器时用来延续上游资产名，避免把"来源"信息抹掉
$previous = $null
if (Test-Path -LiteralPath $versionFile) {
    try { $previous = Get-Content -LiteralPath $versionFile -Raw | ConvertFrom-Json } catch { $previous = $null }
}

$assetName = $null
$releaseUsed = $null

if ($Force -and (Test-Path -LiteralPath $runtimeDir)) {
    Write-Step "重建 runtime/（-Force）"
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}

if ((Test-Path -LiteralPath $pythonExe) -and -not $Force) {
    Write-Step "复用已解压的解释器：$pythonExe"
    if ($previous) { $assetName = $previous.asset; $releaseUsed = $previous.releaseTag }
} else {
    $tmpTar = $null
    $sizeHint = ''
    if ($Archive) {
        # 离线/内网：直接用已下载好的 tar.gz，不碰网络
        Assert-Path $Archive '本地资产路径不存在'
        $tmpTar = (Resolve-Path -LiteralPath $Archive).Path
        if (-not $assetName) { $assetName = Split-Path -Leaf $tmpTar }
        $releaseUsed = "local:$assetName"
        Write-Step "使用本地资产：$tmpTar"
    } else {
        if ($AssetUrl) {
            # 镜像/代理：绕开 api.github.com 与 github.com 的重定向链
            $downloadUrl = $AssetUrl
            if (-not $assetName) { $assetName = [System.IO.Path]::GetFileName(([uri]$AssetUrl).AbsolutePath) }
            $releaseUsed = "custom:$assetName"
        } else {
            Write-Step "解析上游资产（CPython $PythonVersion / $ReleaseTag）"
            if ($ReleaseTag -eq 'latest') {
                $releaseApi = 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
            } else {
                $releaseApi = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$ReleaseTag"
            }
            $headers = @{ 'User-Agent' = 'cKunAgentSSH-prepare-runtime' }
            if ($env:GH_TOKEN) { $headers['Authorization'] = "Bearer $($env:GH_TOKEN)" }
            $release = Invoke-RestMethod -Uri $releaseApi -Headers $headers
            $pattern = "cpython-$PythonVersion.*-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
            $asset = $release.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1
            if (-not $asset) { throw "release $($release.tag_name) 里没有资产 $pattern（上游改了命名？）" }
            $assetName   = $asset.name
            $releaseUsed = $release.tag_name
            $downloadUrl = $asset.browser_download_url
            $sizeHint    = "，$([math]::Round($asset.size / 1MB, 1)) MB"
        }

        # 固定缓存名 = 断点续传的前提：上一次半途失败，这次接着下而不是从 0 开始。
        # 不用 Invoke-WebRequest：它在慢链路上长时间零输出、看起来像卡死，也没法续传
        # （实测 24MB 资产在本机直连 GitHub 时一直没有进展）。
        $tmpTar = Join-Path $env:TEMP ("ckun-" + $assetName)
        Write-Step "下载 $assetName$sizeHint -> $tmpTar"
        $curlArgs = @(
            '-L', '--fail', '--silent', '--show-error',
            # --retry-all-errors：GitHub CDN 在国内常见的是 schannel 复位（curl 56），
            # 它不属于 curl 默认识为"可重试"的几类错误，不加这个参数 --retry 一次都不会生效
            '--retry', '5', '--retry-delay', '3', '--retry-all-errors',
            # 低于 1KB/s 持续 30s 就当链路死了，交给 --retry 续传，不无限挂着
            '--speed-limit', '1024', '--speed-time', '30',
            '-C', '-', '-o', $tmpTar, $downloadUrl
        )
        & curl.exe @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "下载失败（curl 退出码 $LASTEXITCODE）。GitHub 直连不稳时用 -AssetUrl <镜像/代理地址> " +
                  "或 -Archive <本地 tar.gz>；直接重跑本脚本即可续传（-C -）"
        }
    }

    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    # --strip-components=1：上游 tar 顶层是 python/ 一层，去掉后才是 runtime/python.exe
    Invoke-Native -Exe 'tar.exe' -ArgList @('-xzf', $tmpTar, '-C', $runtimeDir, '--strip-components=1')
    Assert-Path $pythonExe '解压后没有 runtime/python.exe（上游资产布局变了？）'
    # 解压确认成功才删缓存：失败时保留，下次 -C - 才能接着续传
    if (-not $Archive -and (Test-Path -LiteralPath $tmpTar)) {
        Remove-Item -LiteralPath $tmpTar -Force -ErrorAction SilentlyContinue
    }
}

$actualVersion = & $pythonExe -c "import sys; print(sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "解释器无法执行：$pythonExe" }
Write-Step "解释器 $pythonExe -> Python $actualVersion"
if (-not $actualVersion.StartsWith($PythonVersion)) {
    # ABI 对不上时 backend/ 的 .pyc 会整批失效，必须在制备阶段就红，而不是等目标机首启变慢
    throw "解释器版本 $actualVersion 与期望 $PythonVersion 不符（请 -Force 重建）"
}

if (-not $SkipDeps) {
    # PEP 668：带 EXTERNALLY-MANAGED 标记的构建（如 uv 托管的 standalone）会拒绝
    # ensurepip/pip 写入。runtime/ 是本脚本自己的构建产物，装依赖正是它的用途 ——
    # 清标记而不是 --break-system-packages，也不去碰任何系统/用户级 Python。
    # 标准位置是 stdlib 目录（Lib\），少数布局放在 purelib（site-packages\），两处都查。
    foreach ($marker in @(
        (Join-Path $runtimeDir 'Lib\EXTERNALLY-MANAGED'),
        (Join-Path $sitePackages 'EXTERNALLY-MANAGED')
    )) {
        if (Test-Path -LiteralPath $marker) {
            Write-Step "移除 EXTERNALLY-MANAGED 标记（该解释器禁止向自身装包）：$marker"
            Remove-Item -LiteralPath $marker -Force
        }
    }
    Invoke-Native -Exe $pythonExe -ArgList @('-m', 'ensurepip', '--upgrade')
    $pipArgs = @(
        '-m', 'pip', 'install',
        '--no-cache-dir',            # 运行时目录会被整体打进产物，pip 缓存纯属额外体积
        '--disable-pip-version-check',
        '--no-compile',              # 字节码交给下面统一用 checked-hash 模式生成
        '-r', $requirements
    )
    if ($IndexUrl) { $pipArgs += @('--index-url', $IndexUrl) }
    Invoke-Native -Exe $pythonExe -ArgList $pipArgs
}

if (-not $SkipSiteCompile) {
    Assert-Path $sitePackages '依赖装完却没有 site-packages：pip 布局异常'
    Write-Step '预编译 site-packages 字节码（checked-hash）'
    try {
        Invoke-Native -Exe $pythonExe -ArgList @(
            '-m', 'compileall', '-q', '-f', '-j', '0',
            '--invalidation-mode', 'checked-hash', $sitePackages
        )
    } catch {
        # 少数包含跨版本语法/不可写目录，编译失败只是回到"首启编译"的老路，不该让发布卡死
        Write-Warning "site-packages 字节码预编译未全部成功（不影响出包，首启会现场编译）：$($_.Exception.Message)"
    }
}

Write-Step '冒烟：后端启动路径依赖的核心包必须可导入'
Invoke-Native -Exe $pythonExe -ArgList @(
    '-c', 'import fastapi, uvicorn, httpx, pydantic, cryptography, openai'
)
# botpy 只在 QQ 渠道启用时导入（backend/bot/qq_manager.py 内 try/except ImportError）：
# 缺了它 QQ 渠道不可用、其余功能正常，所以这里是警告而不是失败
& $pythonExe -c "import botpy" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'botpy 导入失败：目标机上 QQ 渠道将不可用（Agent/终端/微信不受影响）'
}

$reqHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash.ToLower()
$bytecodeMode = 'checked-hash'
if ($SkipSiteCompile) { $bytecodeMode = 'none(pip mtime)' }
$versionPayload = [ordered]@{
    python               = $actualVersion
    releaseTag           = $releaseUsed
    asset                = $assetName
    requirementsSha256   = $reqHash
    sitePackagesBytecode = $bytecodeMode
    generatedAt          = (Get-Date).ToString('o')
}
# 解析层目前只认 python.exe 是否存在；这份文件是给人和排障用的溯源锚点
$versionPayload | ConvertTo-Json | Set-Content -LiteralPath $versionFile -Encoding UTF8

$sizeMb = [math]::Round(((Get-ChildItem -LiteralPath $runtimeDir -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host ''
Write-Host "内嵌运行时就绪：runtime/ ($sizeMb MB, Python $actualVersion)" -ForegroundColor Green
Write-Host "  下一步：npm run package  ->  resources/python-runtime/" -ForegroundColor Green
if ($SkipDeps) { Write-Warning '本次用了 -SkipDeps：包内没有后端依赖，不可发布' }
