$ErrorActionPreference = 'Stop'
# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot 'data\demo-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'このアプリが起動したプロセスの記録はありません。'
    exit 0
}
$record = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ($record.projectRoot -ne $projectRoot) { throw '起動記録の保存先が一致しません。' }
foreach ($candidate in @($record.frontendPid, $record.backendPid, $record.ollamaPid)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $candidate" -ErrorAction SilentlyContinue
    $expectedStart = if ($candidate -eq $record.frontendPid) { $record.frontendStarted } `
        elseif ($candidate -eq $record.backendPid) { $record.backendStarted } else { $record.ollamaStarted }
    $actual = Get-Process -Id $candidate -ErrorAction SilentlyContinue
    if ($expectedStart -and ($null -eq $actual -or $actual.StartTime.ToUniversalTime().ToString('o') -ne $expectedStart)) {
        continue
    }
    $ownedApp = $null -ne $process -and $process.CommandLine -like "*$projectRoot*"
    $ownedOllama = $candidate -eq $record.ollamaPid -and $null -ne $process -and `
        $process.Name -eq 'ollama.exe' -and $process.CommandLine -match '\bserve\b'
    if ($ownedApp -or $ownedOllama) {
        Stop-Process -Id $candidate -ErrorAction SilentlyContinue
    }
}
Remove-Item -LiteralPath $pidFile
Write-Host 'このアプリが所有するプロセスを停止しました。'
