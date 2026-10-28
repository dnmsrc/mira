# Start the local LLM (llama.cpp + Qwen3.8-4B Q4_K_M, OpenAI-compatible).
#   powershell -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1
# Then run the app with LLM_MODE=llama (start.ps1 auto-detects this port).
# ASCII only on purpose: Windows PowerShell 5.1 reads non-BOM files as ANSI.
#
# Why llama.cpp instead of the earlier transformers script: the CUDA build ships its
# own runtime DLLs (cudart/cublas next to the exe), loads in ~5-15s and does not
# depend on the 25GB AniSora python runtime. Same OpenAI /v1 API either way.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$exe = Join-Path $root "tools\llm\llama\llama-server.exe"
$model = Join-Path $root "models\llm\Qwen3.8-4B-Q4_K_M.gguf"
$port = if ($env:LLM_PORT) { $env:LLM_PORT } else { "8080" }

if (-not (Test-Path $exe)) { throw "llama-server not found: $exe" }
if (-not (Test-Path $model)) { throw "model weights missing: $model" }

Write-Host "[llm] llama.cpp + Qwen3-4B (Q4_K_M) -> http://127.0.0.1:$port  (first load ~5-15s)"
Write-Host "[llm] keep this window open; then run start.ps1 in another window"

Push-Location $root
try {
    & $exe -m $model -c 8192 -ngl 99 -fa on --host 127.0.0.1 --port $port --threads 8 --no-webui
} finally {
    Pop-Location
}
