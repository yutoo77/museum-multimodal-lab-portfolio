$ErrorActionPreference = 'Stop'
# Copyright (C) 2026 yutoo77
# SPDX-License-Identifier: AGPL-3.0-only

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$destination = Join-Path $projectRoot 'data\demo'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$output = Join-Path $destination 'fictional_stardust_announcement.wav'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.SelectVoice('Microsoft Haruka Desktop')
    $speaker.SetOutputToWaveFile($output)
    $speaker.Speak('架空の展示、星くずドームの最終案内は、午後三時四十分です。')
} finally {
    $speaker.Dispose()
}
Write-Host $output
