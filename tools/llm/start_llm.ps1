# Start the local LLM (Qwen3-4B-Instruct-2507, OpenAI-compatible, no API key).
#   powershell -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1
# Then run the app with LLM_MODE=llama (start.ps1 reads that env var).
# Reuses the CUDA torch shipped with the local AniSora bundle; ASCII only.

$ErrorActionPreference = "Stop"
$root = "D:\interview\mira"
$py = Join-Path $root "models\anisora\wan\runtime\python.exe"
$server = Join-Path $root "tools\llm\serve_qwen.py"
$model = Join-Path $root "models\llm\Qwen3-4B-Instruct-2507"

if (-not (Test-Path $py)) { throw "bundled CUDA runtime not found: $py" }
if (-not (Test-Path $server)) { throw "server script not found: $server" }
if (-not (Test-Path (Join-Path $model "config.json"))) { throw "model weights missing: $model" }

$port = if ($env:LLM_PORT) { $env:LLM_PORT } else { "8081" }
Write-Host "[llm] Qwen3-4B on http://127.0.0.1:$port  (first load ~10-30s)"
& $py $server --model $model --port $port
