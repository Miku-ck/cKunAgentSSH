#requires -Version 5.1
<#
.SYNOPSIS
  One-shot Windows release build for cKunAgentSSH.

.DESCRIPTION
  Run in a REAL PowerShell terminal (not an IDE-wrapped shell session):

      powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\release-win.ps1
      npm run release:win

  Options:
      -SkipGates    skip typecheck/tests (troubleshooting only, never ship this build)
      -SkipRuntime  skip runtime preparation when runtime/ is already built

  Outputs:
      dist\win-unpacked\
      dist\ckunagentssh-<version>-win32-x64.tar.gz
      dist\SHA256SUMS.txt
      docs\release-win.log

  Why a real terminal is required: an embedded interpreter launched from a
  sandboxed/AV-wrapped session cannot write backend\__pycache__ (Errno 13),
  and scripts\package.js aborts at its compileall hard gate. The preflight
  below detects this early with an actionable message.

  All output strings are ASCII on purpose: Windows PowerShell 5.1 reads BOM-less
  .ps1 as the system ANSI code page, so non-ASCII literals would be garbled.
#>
[CmdletBinding()]
param(
    [switch]$SkipGates,
    [switch]$SkipRuntime
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logPath = Join-Path $root 'docs\release-win.log'
$logLines = New-Object System.Collections.Generic.List[string]

function Say([string]$msg) {
    $line = '[' + (Get-Date).ToString('HH:mm:ss') + '] ' + $msg
    Write-Host $line
    $logLines.Add($line)
}

function Run-Step([string]$name, [scriptblock]$action) {
    Say ('== ' + $name + ' ==')
    & $action
    if ($LASTEXITCODE -ne 0) {
        throw ($name + ' failed, exit=' + $LASTEXITCODE)
    }
}

try {
    $pkg = Get-Content (Join-Path $root 'package.json') -Raw | ConvertFrom-Json
    Say ('Project : ' + $root)
    Say ('Version : ' + $pkg.version)
    Say ('Node    : ' + (& node --version))

    $py = Join-Path $root 'runtime\python.exe'
    $tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path $py)) {
        throw ('Missing embedded runtime: ' + $py + '. Run: npm run prepare:runtime')
    }
    if (-not (Test-Path $tarExe)) {
        throw ('Missing bsdtar: ' + $tarExe + ' (Windows 10+ ships it in System32)')
    }

    # Preflight: package.js compiles backend bytecode with the embedded
    # interpreter and hard-fails if that write is blocked. Check it BEFORE
    # spending minutes on build + electron-builder.
    Say 'Preflight: embedded python writes bytecode cache'
    $probeOut = & $py -m compileall -q --invalidation-mode checked-hash (Join-Path $root 'backend\config.py') 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw ('Embedded python cannot write backend\__pycache__. Add ' + $root + ' to AV real-time scan exclusions (or use a normal terminal) and retry. Detail: ' + $probeOut.Trim())
    }
    Say 'Preflight OK'

    # Pure-Windows gates: no bash dependency (npm run ci needs Git Bash).
    if (-not $SkipGates) {
        Run-Step 'Typecheck'        { & npm.cmd run typecheck }
        Run-Step 'Frontend tests'   { & npm.cmd run test }
        Run-Step 'Backend tests'    { & npm.cmd run test:backend }
        Run-Step 'Gateway selftest' { & node (Join-Path $root 'gateway\hillgw.mjs') --selftest }
    } else {
        Say 'Gates skipped (-SkipGates). DO NOT SHIP THIS BUILD.'
    }

    if (-not $SkipRuntime) {
        Run-Step 'Prepare embedded runtime' { & npm.cmd run prepare:runtime }
    } else {
        Say 'Runtime preparation skipped (-SkipRuntime).'
    }

    Run-Step 'Production build' { & npm.cmd run build }
    Run-Step 'Package (tar.gz)' { & npm.cmd run package }

    $dist = Join-Path $root 'dist'
    $artifact = Get-ChildItem $dist -Filter '*.tar.gz' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $artifact) {
        throw 'No dist/*.tar.gz produced.'
    }

    $hash = (Get-FileHash $artifact.FullName -Algorithm SHA256).Hash
    $sumLine = $hash + '  ' + $artifact.Name
    [System.IO.File]::WriteAllText((Join-Path $dist 'SHA256SUMS.txt'), $sumLine + "`n")
    Say ('Artifact : ' + $artifact.FullName)
    Say ('Size     : ' + [math]::Round($artifact.Length / 1MB, 2) + ' MB')
    Say ('SHA256   : ' + $hash)

    # Structural check: a green build can still omit the runtime or asar.
    Say 'Verifying archive contents'
    $list = & $tarExe -tzf $artifact.FullName 2>&1 | Out-String
    foreach ($needle in @('resources/app.asar', 'resources/python-runtime/python.exe', 'resources/backend/main.py')) {
        if (-not $list.Contains($needle)) {
            throw ('Archive is missing ' + $needle)
        }
    }
    Say 'Archive OK (app.asar / python-runtime / backend present)'

    Say 'DONE. Next: sign the exe, upload tar.gz + SHA256SUMS.txt, then switch the update feed.'
    Say 'Reminder: code signing is NOT configured yet (see README).'
    $logLines | Out-File -FilePath $logPath -Encoding utf8
    exit 0
}
catch {
    Say ('FAILED: ' + $_.Exception.Message)
    $logLines | Out-File -FilePath $logPath -Encoding utf8
    exit 1
}
