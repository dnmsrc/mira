# Start the neural TTS sidecar (CosyVoice2-0.5B) in the mira-tts conda env.
# The app talks to it over http://127.0.0.1:8790 and falls back to edge-tts
# automatically when it is not running.
# ASCII only on purpose: Windows PowerShell 5.1 reads non-BOM files as ANSI.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$py = "C:\Users\QQ\miniconda3\envs\mira-tts\python.exe"
$server = Join-Path $root "tools\tts\serve_neural.py"
$model = Join-Path $root "models\modelscope\models\iic--CosyVoice2-0.5B\snapshots\master"

if (-not (Test-Path $py)) {
    Write-Host "[tts] mira-tts env not found; neural voice disabled (edge-tts fallback)"
    exit 0
}
if (-not (Test-Path $server)) {
    Write-Host "[tts] serve_neural.py not found; neural voice disabled"
    exit 0
}
if (-not (Test-Path $model)) {
    Write-Host "[tts] CosyVoice2 weights not found at $model; neural voice disabled"
    exit 0
}

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

Set-Location $root
Write-Host "[tts] starting neural sidecar on 127.0.0.1:8790 (first load ~15 s)"
& $py $server --host 127.0.0.1 --port 8790
