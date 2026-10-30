#Requires -Version 7.0
<#
.SYNOPSIS
  ghspeed - GitHub 公共加速通道一键探测与切换工具（国内）
.DESCRIPTION
  动态探测公共加速通道（镜像前缀）的可用性与延迟，支持：
    ghspeed.ps1                    探测所有通道，实时输出探测明细与可用性排行（结果缓存 30 分钟）
    ghspeed.ps1 -Refresh           强制重新探测
    ghspeed.ps1 -Clone <owner/repo 或完整链接> [-Run]  拼出/执行加速克隆命令
    ghspeed.ps1 -Auto              一键持久加速：将 git insteadOf 指向最快通道（push 不受影响）
    ghspeed.ps1 -AutoOff           移除 git 中的加速 insteadOf 配置
    ghspeed.ps1 -Clean             清理所有 GitHub 加速冗余配置（git config / hosts / 缓存）
    ghspeed.ps1 -Prefix <前缀>     临时补充一个自定义通道一起探测
  缓存文件: $env:TEMP\ghspeed-cache.json
#>
[CmdletBinding(DefaultParameterSetName = 'probe')]
param(
    [Parameter(ParameterSetName = 'clone')] [string]$Clone,
    [Parameter(ParameterSetName = 'clone')] [switch]$Run,
    [Parameter(ParameterSetName = 'auto')]  [switch]$Auto,
    [Parameter(ParameterSetName = 'auto')]  [switch]$AutoOff,
    [Parameter(ParameterSetName = 'clean')] [switch]$Clean,
    [string]$Prefix,
    [switch]$Refresh,
    [switch]$Json,
    [int]$TimeoutSec = 6
)

$ErrorActionPreference = 'SilentlyContinue'
$CacheFile = Join-Path $env:TEMP 'ghspeed-cache.json'
# 探测目标：GitHub 上的小文件（约 1KB），走各镜像前缀
$ProbeTarget = 'https://raw.githubusercontent.com/torvalds/linux/master/README'

# ---------- 内置候选通道（失效是常态，列表随时可增删） ----------
$BuiltIn = @(
    'https://ghfast.top/',
    'https://gh-proxy.com/',
    'https://gh.llkk.cc/',
    'https://ghproxy.net/',
    'https://ghproxy.cc/',
    'https://gh.ddlc.top/',
    'https://slink.ltd/',
    'https://ghproxy.cn/'
)

function Get-CandidatePrefixes {
    param([string]$Extra)
    $list = [System.Collections.Generic.List[string]]::new()
    # 动态补充：从公共聚合接口拉取活跃通道（拉不到就静默降级到内置列表）
    try {
        $r = Invoke-RestMethod -Uri 'https://ghproxy.link/api/v1/sites' -TimeoutSec ([Math]::Min($TimeoutSec, 5))
        foreach ($s in $r) {
            $u = if ($s.url) { $s.url } elseif ($s.site) { $s.site } else { $null }
            if ($u -and -not $u.EndsWith('/')) { $u += '/' }
            if ($u) { $list.Add($u) }
        }
    } catch { }
    $BuiltIn | ForEach-Object { $list.Add($_) }
    if ($Extra) {
        if (-not $Extra.EndsWith('/')) { $Extra += '/' }
        $list.Add($Extra)
    }
    # 去重
    $list.ToArray() | Select-Object -Unique
}

function Test-Channel {
    param([string]$Prefix, [int]$Timeout)
    $u = $Prefix + $ProbeTarget
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri $u -Method Get -TimeoutSec $Timeout -MaximumRedirection 3 -UseBasicParsing
        $sw.Stop()
        if ($resp.StatusCode -eq 200 -and $resp.RawContentLength -gt 0) {
            [pscustomobject]@{ Prefix = $Prefix; OK = $true; Ms = [int]$sw.ElapsedMilliseconds; Size = $resp.RawContentLength }
        } else {
            [pscustomobject]@{ Prefix = $Prefix; OK = $false; Ms = [int]$sw.ElapsedMilliseconds; Size = 0 }
        }
    } catch {
        $sw.Stop()
        [pscustomobject]@{ Prefix = $Prefix; OK = $false; Ms = [int]$sw.ElapsedMilliseconds; Size = 0 }
    }
}

function Get-ChannelRanking {
    param([switch]$NoCache, [switch]$Quiet)
    if (-not $NoCache -and (Test-Path $CacheFile)) {
        $cache = Get-Content $CacheFile -Raw | ConvertFrom-Json
        $age = ((Get-Date) - [datetime]$cache.Time).TotalMinutes
        if ($age -lt 30 -and @($cache.Results | Where-Object OK).Count -gt 0) {
            return $cache.Results
        }
    }
    $prefixes = Get-CandidatePrefixes -Extra $Prefix
    Write-Host "探测 $($prefixes.Count) 个通道中（超时 ${TimeoutSec}s）..." -ForegroundColor DarkGray
    # 并发探测：每个通道一个独立 PowerShell 线程，边完成边实时输出明细
    $results = [System.Collections.Generic.List[object]]::new()
    $jobs = [System.Collections.Generic.List[object]]::new()
    $total = @($prefixes).Count
    $done = 0
    foreach ($p in $prefixes) {
        $ps = [powershell]::Create()
        [void]$ps.AddScript({
            param($prefix, $target, $timeout)
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            try {
                $resp = Invoke-WebRequest -Uri ($prefix + $target) -Method Get -TimeoutSec $timeout -MaximumRedirection 3 -UseBasicParsing
                $sw.Stop()
                [pscustomobject]@{ Prefix = $prefix; OK = ($resp.StatusCode -eq 200); Ms = [int]$sw.ElapsedMilliseconds; Reason = '' }
            } catch {
                $sw.Stop()
                $msg = ($_.Exception.Message -split "`n")[0]
                if ($msg -match '超时|timed out|Timeout') { $msg = '超时' }
                elseif ($msg -match '403|Forbidden') { $msg = '403 被拒' }
                elseif ($msg -match '404') { $msg = '404 不支持' }
                elseif ($msg -match '无法解析|name or service|No such host') { $msg = 'DNS 解析失败' }
                elseif ($msg -match '连接|Connection|refused|unreachable') { $msg = '连接失败' }
                if ($msg.Length -gt 50) { $msg = $msg.Substring(0, 50) + '...' }
                [pscustomobject]@{ Prefix = $prefix; OK = $false; Ms = [int]$sw.ElapsedMilliseconds; Reason = $msg }
            }
        }).AddArgument($p).AddArgument($ProbeTarget).AddArgument($TimeoutSec)
        $jobs.Add(@{ PS = $ps; Handle = $ps.BeginInvoke() })
    }
    while ($jobs.Count -gt 0) {
        $handles = @($jobs | ForEach-Object { $_.Handle.AsyncWaitHandle })
        $idx = [System.Threading.WaitHandle]::WaitAny($handles, 300)
        if ($idx -ge 0 -and $idx -lt $handles.Count) {
            $j = $jobs[$idx]
            $out = $j.PS.EndInvoke($j.Handle)
            $j.PS.Dispose()
            $jobs.RemoveAt($idx)
            # EndInvoke 返回 PSDataCollection，取第一个元素确保是单个结果对象
            $r = @($out)[0]
            $results.Add($r)
            $done++
            if (-not $Quiet) {
                $name = $r.Prefix -replace '^https?://', ''
                if ($r.OK) {
                    Write-Host ("  [{0,2}/{1,2}] ✔ 可用   {2,-26} {3,5}ms" -f $done, $total, $name, $r.Ms) -ForegroundColor Green
                } else {
                    $reason = if ($r.Reason) { $r.Reason } else { '无响应' }
                    Write-Host ("  [{0,2}/{1,2}] ✘ 失败   {2,-26} {3}" -f $done, $total, $name, $reason) -ForegroundColor DarkGray
                }
            }
        }
    }
    $results = $results | Sort-Object -Property @{Expression = 'OK'; Descending = $true }, Ms
    @{ Time = (Get-Date).ToString('o'); Results = $results } | ConvertTo-Json -Depth 3 | Set-Content $CacheFile -Encoding utf8
    return $results
}

function Get-BestChannel {
    param([switch]$NoCache, [switch]$Quiet)
    $rank = Get-ChannelRanking -NoCache:$NoCache -Quiet:$Quiet
    $best = $rank | Where-Object OK | Select-Object -First 1
    if (-not $best) {
        Write-Host '所有公共通道均不可用！建议：' -ForegroundColor Red
        Write-Host '  1. 稍后重试（公共站高峰期常全挂）' 
        Write-Host '  2. -Prefix 补充新通道: ghspeed.ps1 -Prefix https://新通道域名/ -Refresh'
        Write-Host '  3. 改用本地代理: git config --global http.https://github.com.proxy http://127.0.0.1:端口'
        return $null
    }
    return $best
}

# ---------- 主流程 ----------
if ($Clean) {
    Write-Host '== 清理 GitHub 加速冗余配置 ==' -ForegroundColor Cyan
    # 1. git config 中的 insteadOf / pushInsteadOf
    $cfg = git config --global --get-regexp 'url\..*insteadof' 2>$null
    if ($cfg) {
        foreach ($line in $cfg) {
            $key = ($line -split ' ')[0]
            git config --global --unset-all $key
            Write-Host "已移除 git 配置: $key" -ForegroundColor Yellow
        }
    } else { Write-Host 'git config 无 insteadOf 加速残留' }
    # 2. git config 中指向 github 的 http/https proxy
    $px = git config --global --get-regexp '(http|https)\.https://github\.com.*proxy' 2>$null
    if ($px) {
        foreach ($line in $px) {
            $key = ($line -split ' ')[0]
            git config --global --unset-all $key
            Write-Host "已移除 git 代理配置: $key" -ForegroundColor Yellow
        }
    }
    # 3. hosts 中 GitHub 加速残留（需管理员权限，先备份）
    $hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
    $bad = Select-String -Path $hostsPath -Pattern 'github|ghproxy|ghfast|fastly' -ErrorAction SilentlyContinue |
        Where-Object { $_.Line -and -not $_.Line.Trim().StartsWith('#') }
    if ($bad) {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($isAdmin) {
            Copy-Item $hostsPath "$hostsPath.ghspeed.bak" -Force
            $keep = Get-Content $hostsPath | Where-Object { $_ -notmatch 'github|ghproxy|ghfast|fastly' -or $_.Trim().StartsWith('#') }
            Set-Content $hostsPath $keep -Encoding ascii
            Write-Host "已清理 hosts 中 $($bad.Count) 条加速残留（备份: hosts.ghspeed.bak）" -ForegroundColor Yellow
        } else { Write-Host "hosts 发现 $($bad.Count) 条残留，需管理员权限运行 -Clean 才能清理" -ForegroundColor DarkYellow }
    } else { Write-Host 'hosts 无加速残留' }
    # 4. 缓存
    if (Test-Path $CacheFile) { Remove-Item $CacheFile -Force; Write-Host '已删除探测缓存' }
    return
}

if ($AutoOff) {
    $cfg = git config --global --get-regexp 'url\..*insteadof' 2>$null
    foreach ($line in $cfg) {
        $key = ($line -split ' ')[0]
        git config --global --unset-all $key
    }
    Write-Host '已移除所有 git 加速 insteadOf 配置' -ForegroundColor Green
    return
}

if ($Auto) {
    $best = Get-BestChannel -Quiet:([bool]$Json)
    if (-not $best) { exit 1 }
    # 先清理旧的加速 insteadOf（保留 pushInsteadOf，避免通道冗余堆积）
    $old = git config --global --get-regexp 'url\..*insteadof' 2>$null
    foreach ($line in $old) {
        $key = ($line -split '\s+')[0]
        if ($key -notmatch '\.pushinsteadof$') {
            git config --global --unset-all $key
        }
    }
    git config --global url."$($best.Prefix)https://github.com/".insteadOf 'https://github.com/'
    # pushInsteadOf 保证 push 仍走官方地址（镜像只读，不能 push）
    git config --global url.'https://github.com/'.pushInsteadOf 'https://github.com/'
    Write-Host "一键加速已生效: 所有 git clone/fetch 自动经 $($best.Prefix) （延迟 $($best.Ms)ms）" -ForegroundColor Green
    Write-Host 'push 不受影响（仍直连官方）。撤销请运行: ghspeed.ps1 -AutoOff' -ForegroundColor DarkGray
    return
}

if ($Clone) {
    $repoUrl = $Clone
    if ($Clone -notmatch '^https?://') {
        $repoUrl = "https://github.com/$($Clone.TrimEnd('/'))"
        if ($Clone -notmatch '/') { Write-Host '格式: -Clone owner/repo 或完整 GitHub URL' -ForegroundColor Red; exit 1 }
    }
    $best = Get-BestChannel -Quiet:([bool]$Json)
    if (-not $best) { exit 1 }
    $cmd = "git clone $($best.Prefix)$repoUrl"
    if ($Json) { @{ clone = $cmd; via = $best.Prefix; ms = $best.Ms } | ConvertTo-Json }
    else { Write-Host $cmd -ForegroundColor Green; Write-Host "# 经 $($best.Prefix)，延迟 $($best.Ms)ms" -ForegroundColor DarkGray }
    if ($Run) {
        Write-Host '开始克隆...' -ForegroundColor DarkGray
        & git clone "$($best.Prefix)$repoUrl"
    }
    return
}

# 默认：探测排行
$rank = Get-ChannelRanking -NoCache:$Refresh -Quiet:([bool]$Json)
$okCount = @($rank | Where-Object OK).Count
Write-Host ''
Write-Host "GitHub 加速通道探测结果（可用 $okCount / $($rank.Count)，探测目标: 1KB 文件）" -ForegroundColor Cyan
if (-not $Json) {
    $rank | Format-Table @(
        @{ Label = '状态'; Expression = { if ($_.OK) { '✔ 可用' } else { '✘ 不可用' } } },
        @{ Label = '延迟'; Expression = { if ($_.OK) { "$($_.Ms)ms" } else { '-' } }; Alignment = 'Right' },
        @{ Label = '通道前缀'; Expression = { $_.Prefix } }
    ) -AutoSize
    $best = $rank | Where-Object OK | Select-Object -First 1
    if ($best) {
        Write-Host "最快通道: $($best.Prefix)（$($best.Ms)ms）" -ForegroundColor Green
        Write-Host '一键持久加速:  ghspeed.ps1 -Auto' -ForegroundColor DarkGray
        Write-Host '加速克隆示例:  ghspeed.ps1 -Clone owner/repo' -ForegroundColor DarkGray
    }
} else {
    $rank | ConvertTo-Json
}
