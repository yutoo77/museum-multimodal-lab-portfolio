param([switch]$NoBrowser)

# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$frontendDir = Join-Path $projectRoot 'frontend'
$dataDir = Join-Path $projectRoot 'data'
$logDir = Join-Path $dataDir 'logs'
$pidFile = Join-Path $dataDir 'demo-processes.json'

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Python 環境がありません。README のインストール手順を先に実行してください。'
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules'))) {
    throw 'Frontend の依存がありません。frontend で npm ci を実行してください。'
}
$nodeCommand = Get-Command node.exe -ErrorAction Stop
$ollamaCommand = Get-Command ollama.exe -ErrorAction Stop
$viteEntry = Join-Path $frontendDir 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteEntry)) { throw 'Vite の起動ファイルがありません。npm ci を実行してください。' }
& $pythonExe -c 'import fastapi, pymupdf, httpx, PIL' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Backend の依存が不足しています。requirements.txt をインストールしてください。' }

$occupied = @()
foreach ($port in @(8765, 5174, 11436)) {
    $probe = New-Object System.Net.Sockets.TcpClient
    try {
        $probe.Connect('127.0.0.1', $port)
        $occupied += $port
    } catch [System.Net.Sockets.SocketException] {
        # A refused local connection means the port is available.
    } finally {
        $probe.Dispose()
    }
}
if ($occupied) {
    throw "使用中のポート: $($occupied -join ', ')。既存プロセスを勝手に停止せず、占有元を確認してください。"
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$ollama = $null
$backend = $null
$frontend = $null
try {
    $previousOllamaHost = $env:OLLAMA_HOST
    $previousKeepAlive = $env:OLLAMA_KEEP_ALIVE
    $previousMaxModels = $env:OLLAMA_MAX_LOADED_MODELS
    $previousParallel = $env:OLLAMA_NUM_PARALLEL
    $previousNoCloud = $env:OLLAMA_NO_CLOUD
    try {
        $env:OLLAMA_HOST = '127.0.0.1:11436'
        $env:OLLAMA_KEEP_ALIVE = '0'
        $env:OLLAMA_MAX_LOADED_MODELS = '1'
        $env:OLLAMA_NUM_PARALLEL = '1'
        $env:OLLAMA_NO_CLOUD = '1'
        $ollama = Start-Process -FilePath $ollamaCommand.Source -ArgumentList @('serve') `
            -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'ollama.out.log') `
            -RedirectStandardError (Join-Path $logDir 'ollama.err.log')
    } finally {
        $env:OLLAMA_HOST = $previousOllamaHost
        $env:OLLAMA_KEEP_ALIVE = $previousKeepAlive
        $env:OLLAMA_MAX_LOADED_MODELS = $previousMaxModels
        $env:OLLAMA_NUM_PARALLEL = $previousParallel
        $env:OLLAMA_NO_CLOUD = $previousNoCloud
    }
    $modelReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $models = Invoke-RestMethod -Uri 'http://127.0.0.1:11436/api/tags' -TimeoutSec 2
            if ($null -ne $models.models) { $modelReady = $true; break }
        } catch { }
        if ($ollama.HasExited) { break }
    }
    if (-not $modelReady) { throw "専用のローカルAIを起動できませんでした。$logDir のログを確認してください。" }

    $previousMmlUrl = $env:MML_OLLAMA_URL
    try {
        $env:MML_OLLAMA_URL = 'http://127.0.0.1:11436'
        $backend = Start-Process -FilePath $pythonExe `
            -ArgumentList @('-m', 'uvicorn', 'museum_multimodal_lab.api:app', '--host', '127.0.0.1', '--port', '8765') `
            -WorkingDirectory (Join-Path $projectRoot 'backend') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'backend.out.log') `
            -RedirectStandardError (Join-Path $logDir 'backend.err.log')
    } finally {
        $env:MML_OLLAMA_URL = $previousMmlUrl
    }
    $frontend = Start-Process -FilePath $nodeCommand.Source `
        -ArgumentList @($viteEntry, '--host', '127.0.0.1', '--port', '5174') `
        -WorkingDirectory $frontendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'frontend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'frontend.err.log')

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $api = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
            $web = Invoke-WebRequest -Uri 'http://127.0.0.1:5174/' -TimeoutSec 2 -UseBasicParsing
            if ($api.status -eq 'ok' -and $web.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        if ($backend.HasExited -or $frontend.HasExited) { break }
    }
    if (-not $ready) { throw "アプリが起動しませんでした。$logDir のログを確認してください。" }

    @{
        ollamaPid = $ollama.Id
        ollamaStarted = $ollama.StartTime.ToUniversalTime().ToString('o')
        backendPid = $backend.Id
        backendStarted = $backend.StartTime.ToUniversalTime().ToString('o')
        frontendPid = $frontend.Id
        frontendStarted = $frontend.StartTime.ToUniversalTime().ToString('o')
        projectRoot = $projectRoot
    } |
        ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
    Write-Host 'Museum Multimodal Lab: http://127.0.0.1:5174/'
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:5174/' }
} catch {
    foreach ($owned in @($frontend, $backend, $ollama)) {
        if ($null -ne $owned -and -not $owned.HasExited) { Stop-Process -Id $owned.Id -ErrorAction SilentlyContinue }
    }
    throw
}
