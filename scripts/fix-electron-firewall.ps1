# 为 Electron 桥接放行回环入站连接（需管理员权限）
#
# 背景：本机实测 Windows 防火墙对回环（127.0.0.1）TCP 入站连接按"可执行文件"放行。
#   - python.exe / node.exe 有 Allow 规则  -> 其监听端口可被连接
#   - electron.exe 无任何规则              -> 其监听端口连接超时
# 后果：Electron bridge（定时任务无头远程执行所依赖）端口处于 LISTENING 但永远连不上，
#       后端报 [BRIDGE_PORT_CLOSED] / 探活超时。
#
# 用法：右键"以管理员身份运行 PowerShell"，然后执行本脚本。
#
#   pwsh -File scripts\fix-electron-firewall.ps1
#
# 卸载：Remove-NetFirewallRule -DisplayName "cKunAgentSSH Electron Bridge (loopback inbound)"

[CmdletBinding()]
param(
    # 允许显式指定 electron.exe / 打包后可执行文件路径
    [string]$ProgramPath = ""
)

$ErrorActionPreference = "Stop"
$RuleName = "cKunAgentSSH Electron Bridge (loopback inbound)"

# ── 权限检查 ────────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[FAIL] 需要管理员权限。请以管理员身份重新打开 PowerShell 后重试。" -ForegroundColor Red
    exit 1
}

# ── 定位目标可执行文件 ──────────────────────────────
if (-not $ProgramPath) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\node_modules\electron\dist\electron.exe"),   # 开发模式
        (Join-Path $PSScriptRoot "..\dist\win-unpacked\cKunAgentSSH.exe"),        # 打包模式
        (Join-Path $PSScriptRoot "..\dist\win-unpacked\ElectronSSH.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $ProgramPath = (Resolve-Path $c).Path; break }
    }
}

if (-not $ProgramPath) {
    Write-Host "[FAIL] 未找到 electron 可执行文件，请用 -ProgramPath 显式指定。" -ForegroundColor Red
    exit 1
}
Write-Host "[INFO] 目标程序: $ProgramPath"

# ── 幂等创建规则 ────────────────────────────────────
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[SKIP] 规则已存在，先删除重建以确保参数正确。"
    $existing | Remove-NetFirewallRule
}

New-NetFirewallRule `
    -DisplayName $RuleName `
    -Description "允许 cKunAgentSSH 的 Electron 主进程在回环上接受后端连接（仅 127.0.0.1）" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalAddress 127.0.0.1 `
    -RemoteAddress 127.0.0.1 `
    -Program $ProgramPath `
    -Profile Any `
    -Enabled True | Out-Null

Write-Host "[OK] 防火墙规则已添加。" -ForegroundColor Green

# ── 回读确认 ────────────────────────────────────────
Get-NetFirewallRule -DisplayName $RuleName |
    Select-Object DisplayName, Enabled, Direction, Action, Profile |
    Format-List

Write-Host "下一步：完整重启应用，然后确认 electron-bridge.log 中出现 'self-probe OK'。" -ForegroundColor Cyan
