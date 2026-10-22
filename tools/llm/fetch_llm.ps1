# Fetch the two things this repo intentionally does NOT ship:
#   1) llama.cpp Windows CUDA build (llama-server.exe + DLLs) -> tools\llm\llama\
#   2) Qwen3.8-4B Q4_K_M GGUF weights                       -> models\llm\
#
# The app runs WITHOUT these (Mock mode is a full code path). This script is only
# for "real model mode". It is idempotent: whatever already exists is skipped.
#
#   powershell -ExecutionPolicy Bypass -File .\tools\llm\fetch_llm.ps1
#
# Why Qwen3.8-4B: it is a qwen35 hybrid (Gated DeltaNet) model, so it needs a
# RECENT llama.cpp build - older builds fail to load the architecture. Override
# the build with $env:LLAMA_BUILD if you want a different one.
#
# Behind the GFW? huggingface.co is often unreachable; hf-mirror.com is the
# default here. Override with $env:HF_ENDPOINT.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads non-BOM files as ANSI.

$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$llmDir = Join-Path $root "tools\llm"
$llamaD = Join-Path $llmDir "llama"
$modelD = Join-Path $root "models\llm"
$exe    = Join-Path $llamaD "llama-server.exe"
$model  = Join-Path $modelD "Qwen3.8-4B-Q4_K_M.gguf"

$build   = if ($env:LLAMA_BUILD) { $env:LLAMA_BUILD } else { "b11729" }
$hf      = if ($env:HF_ENDPOINT) { $env:HF_ENDPOINT.TrimEnd("/") } else { "https://hf-mirror.com" }
$zipUrl  = "https://github.com/ggml-org/llama.cpp/releases/download/$build/llama-$build-bin-win-cuda-12.4-x64.zip"
$dllUrl  = "https://github.com/ggml-org/llama.cpp/releases/download/$build/cudart-llama-bin-win-cuda-12.4-x64.zip"
$ggufUrl = "$hf/empero-ai/Qwen3.8-4B-Distill-GGUF/resolve/main/Qwen3.8-4B-Q4_K_M.gguf"

New-Item -ItemType Directory -Force -Path $llmDir, $modelD | Out-Null

function Get-File($url, $out, $what) {
    if (Test-Path $out) {
        Write-Host "[fetch] $what already present, skipped"
        return
    }
    Write-Host "[fetch] downloading $what"
    Write-Host "        $url"
    $tmp = "$out.part"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        Move-Item -Force $tmp $out
    } catch {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        throw "download failed: $url`n$_"
    }
}

# ---------------------------------------------------------------- llama.cpp ---
if (Test-Path $exe) {
    Write-Host "[fetch] llama.cpp already present: $exe"
} else {
    $lzip = Join-Path $llmDir "llama-$build-bin-win-cuda-12.4-x64.zip"
    Get-File $zipUrl $lzip "llama.cpp $build (~65 MB)"

    $stage = Join-Path $llmDir "_stage"
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    Write-Host "[fetch] extracting llama.cpp ..."
    Expand-Archive -Path $lzip -DestinationPath $stage -Force

    # The archive layout has changed between builds: locate llama-server.exe
    # instead of assuming a fixed path, then copy its whole folder over.
    $found = Get-ChildItem -Path $stage -Recurse -Filter "llama-server.exe" |
        Select-Object -First 1
    if (-not $found) { throw "llama-server.exe was not found inside $lzip" }

    if (Test-Path $llamaD) { Remove-Item -Recurse -Force $llamaD }
    New-Item -ItemType Directory -Force -Path $llamaD | Out-Null
    Copy-Item -Path (Join-Path $found.Directory.FullName "*") -Destination $llamaD -Recurse -Force
    Remove-Item -Recurse -Force $stage
    Write-Host "[fetch] llama.cpp ready: $llamaD"

    # CUDA runtime DLLs ship as a separate package; without them llama-server
    # dies with "cudart64_12.dll was not found".
    try {
        $dllzip = Join-Path $llmDir "cudart-llama-bin-win-cuda-12.4-x64.zip"
        Get-File $dllUrl $dllzip "CUDA runtime DLLs (~16 MB)"
        Expand-Archive -Path $dllzip -DestinationPath $llamaD -Force
        Write-Host "[fetch] CUDA runtime DLLs merged into $llamaD"
    } catch {
        Write-Host "[fetch] WARNING: could not fetch the CUDA DLL package." -ForegroundColor Yellow
        Write-Host "        $_" -ForegroundColor Yellow
        Write-Host "        If llama-server then fails on a missing cudart/bublas DLL, install the" -ForegroundColor Yellow
        Write-Host "        CUDA 12.4 runtime, or use a CPU-only llama.cpp build instead." -ForegroundColor Yellow
    }
}

# ------------------------------------------------------------------ weights ---
Get-File $ggufUrl $model "Qwen3.8-4B-Q4_K_M.gguf (~2.8 GB)"

Write-Host ""
Write-Host "[fetch] done."
Write-Host "  weights : $model"
Write-Host "  runtime : $exe"
Write-Host ""
Write-Host "  next, in two terminals:"
Write-Host "    powershell -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1"
Write-Host "    `$env:LLM_MODE='llama'; powershell -ExecutionPolicy Bypass -File .\start.ps1"
