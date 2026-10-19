# Mira scene - one command local start.
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
# Creates .venv on first run, installs requirements, starts the app, opens the browser.
# ASCII only on purpose: Windows PowerShell 5.1 reads non-BOM files as ANSI.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[mira] creating venv..."
    $sys = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $sys) { throw "python not found on PATH; install Python 3.10+ first" }
    & $sys -m venv .venv
}

Write-Host "[mira] installing requirements (skipped if already satisfied)..."
& $py -m pip install -q --disable-pip-version-check -r requirements.txt

$port = if ($env:PORT) { $env:PORT } else { "8787" }
$mode = if ($env:LLM_MODE) { $env:LLM_MODE } else { "mock" }
Write-Host "[mira] http://127.0.0.1:$port   LLM_MODE=$mode"
Write-Host "[mira] mock mode = zero model/network/key; real local LLM: run tools\llm\start_llm.ps1 and start with LLM_MODE=llama"

Start-Process "http://127.0.0.1:$port/" | Out-Null
& $py -m uvicorn app.main:app --host 127.0.0.1 --port $port
