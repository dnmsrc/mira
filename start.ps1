# Mira scene - one command local start.
#
#   Double-click start.bat   (or: powershell -ExecutionPolicy Bypass -File .\start.ps1)
#
# What it does, in order:
#   1. bootstrap .venv + requirements on first run
#   2. if an app is already answering on the port, just open the browser and exit
#   3. bring up the 4B llama.cpp model if no model is listening, then WAIT for it
#      to answer /health (a bound port only means the process is alive)
#   4. bring up the neural TTS sidecar if its conda env + weights exist, then WAIT
#      for its /health 200 (CosyVoice2 binds early, loads weights ~15s after)
#   5. warn loudly when ComfyUI is holding the GPU (the demo drops to Mock + 10x slower)
#   6. start the app, open the browser ONLY once it is actually usable
#
# Steps 3/4 wait on purpose: talking to the demo before the model and the voice
# are in VRAM gives Mock replies and edge-tts audio, which is the robot voice the
# demo is judged on. Cold start is ~30-40s; after that it is instant.
#
# Switches:
#   -Port 8788     use another port
#   -NoBrowser     do not open the browser
#   -Stop          stop a running instance (app + the model/TTS it started) and exit
#   -FreeGpu       stop ComfyUI (:8188) first so the demo gets the whole card
#
# ASCII only on purpose: Windows PowerShell 5.1 reads non-BOM files as ANSI,
# so any non-ASCII here would be mangled.

[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$FreeGpu
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---------------------------------------------------------------- helpers ----
function Test-Port([int]$p) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $ok = $c.ConnectAsync("127.0.0.1", $p).Wait(400)
        $c.Close()
        return [bool]$ok
    } catch { return $false }
}

function Test-Health([int]$p) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$p/api/health"
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

# Same idea for any sidecar URL: 503 while the weights are still loading.
function Test-Url([string]$url) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $url
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Wait-Url([string]$url, [int]$timeoutSec, [string]$label) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        if (Test-Url $url) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host ("       {0} did not report ready within {1}s" -f $label, $timeoutSec) -ForegroundColor Yellow
    return $false
}

function Stop-ByPort([int]$p, [switch]$OnlyOwned) {
    $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try {
            $proc = Get-Process -Id $c.OwningProcess -ErrorAction Stop
            # -Stop must not kill somebody else's server that happens to share the
            # port (LM Studio on :8080, a friend's uvicorn on :8787, ...). Only the
            # process whose image lives inside this project is ours to kill.
            $path = $null
            try { $path = $proc.Path } catch { }
            if ($OnlyOwned -and $path -and (-not $path.StartsWith($PSScriptRoot))) {
                Write-Host ("  :{0} is held by {1} (pid {2}) outside this project - left alone" -f $p, $proc.ProcessName, $proc.Id) -ForegroundColor Yellow
                continue
            }
            Write-Host ("  stopping pid {0} ({1}) on :{2}" -f $proc.Id, $proc.ProcessName, $p)
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        } catch { }
    }
}

function Get-VramUsedMB {
    try {
        $v = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
        return [int]($v | Select-Object -First 1)
    } catch { return -1 }
}

function Write-Status([string]$name, [bool]$ok, [string]$detail) {
    $mark = if ($ok) { "[ok]" } else { "[--]" }
    Write-Host ("  {0} {1,-12} {2}" -f $mark, $name, $detail)
}

# ------------------------------------------------------------ model launcher ---
# Defined up here (not further down) on purpose: the "already running" branch a
# few lines below must be able to bring the model up too. See Start-Model.
$llmScript = Join-Path $PSScriptRoot "tools\llm\start_llm.ps1"
$modelFile = Join-Path $PSScriptRoot "models\llm\Qwen3.8-4B-Q4_K_M.gguf"

# The user may point the app at their own OpenAI-compatible backend instead of the
# bundled 4B:
#     $env:LLAMA_URL="http://127.0.0.1:11434"; $env:LLAMA_MODEL="qwen2.5:7b"
# If that port is already serving, do NOT launch the bundled model as well: it would
# take VRAM for nothing (the app keeps talking to the user's backend either way).
$llmPort = 8080
if ($env:LLAMA_URL) {
    try { $llmPort = ([uri]$env:LLAMA_URL).Port } catch { }
}
$externalLlm = ($llmPort -ne 8080)

function Start-Model {
    Start-Process -FilePath "powershell" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $llmScript `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized | Out-Null
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if ($Port -le 0) {
    if ($env:PORT) { $Port = [int]$env:PORT } else { $Port = 8787 }
}

# ------------------------------------------------------------------ stop -----
if ($Stop) {
    Write-Host "[mira] stopping the app on :$Port"
    Stop-ByPort $Port -OnlyOwned
    Write-Host "[mira] stopping the 4B model on :8080"
    Stop-ByPort 8080 -OnlyOwned
    # llama-server can outlive its listener when killed mid-request: sweep by name,
    # but only the copy that shipped inside this project.
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($PSScriptRoot) } |
        ForEach-Object {
            Write-Host ("  killing leftover {0} pid {1}" -f $_.ProcessName, $_.Id)
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    Write-Host "[mira] stopping the neural TTS on :8790"
    Stop-ByPort 8790 -OnlyOwned
    Write-Host "[mira] done. ComfyUI on :8188 is left alone (close it yourself, or use -FreeGpu)."
    exit 0
}

# --------------------------------------------------------- already running ---
if (Test-Health $Port) {
    Write-Host "[mira] already running on http://127.0.0.1:$Port/ - opening the browser"
    # "The app answers" is NOT "the model answers". A leftover app instance (a dev
    # uvicorn from an earlier session, or one started before the model died) makes
    # this branch exit before the model step, handing the user an interface with no
    # model behind it: every turn then fails with "llama-server connection failed",
    # which reads as "she stopped talking". So check the model before opening.
    if (-not ((Test-Port 1234) -or (Test-Port 8080) -or ($externalLlm -and (Test-Port $llmPort)))) {
        if ((Test-Path $llmScript) -and (Test-Path $modelFile)) {
            Write-Host "[mira] WARNING: app is up but NO model is listening - starting the 4B model ..." -ForegroundColor Yellow
            Start-Model
            if (Wait-Url "http://127.0.0.1:8080/health" 90 "the 4B model") {
                Write-Host "[mira] model ready on :8080"
            } else {
                Write-Host "[mira] model did NOT come up - replies will fall back to Mock" -ForegroundColor Yellow
            }
        } else {
            Write-Host "[mira] no local model available - replies will be scripted Mock" -ForegroundColor Yellow
        }
    }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port/" | Out-Null }
    exit 0
}

if (Test-Port $Port) {
    Write-Host "[mira] port $Port is taken by something that is NOT this app." -ForegroundColor Yellow
    Write-Host "       stop it first, or run: .\start.ps1 -Port 8788"
    exit 1
}

# ---------------------------------------------------------------- bootstrap --
if (-not (Test-Path $py)) {
    Write-Host "[mira] creating .venv ..."
    $sys = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $sys) { throw "python not found on PATH; install Python 3.10+ first" }
    & $sys -m venv .venv
}

Write-Host "[mira] checking python requirements (no-op when already satisfied) ..."
& $py -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "       pip returned $LASTEXITCODE (continuing)" -ForegroundColor Yellow }

$mode = if ($env:LLM_MODE) { $env:LLM_MODE } else { "auto" }

# ------------------------------------------------------------------ model ----
$modelWanted = $false
if ((Test-Port 1234) -or (Test-Port 8080) -or ($externalLlm -and (Test-Port $llmPort))) {
    $modelWanted = $true
} elseif ((Test-Path $llmScript) -and (Test-Path $modelFile)) {
    $modelWanted = $true
    Write-Host "[mira] starting the 4B model on :8080 (first load 5-15s) ..."
    Start-Model
} else {
    Write-Host "[mira] no local model available - the app will use its scripted Mock" -ForegroundColor Yellow
}

# Wait on the URL, not the port: right after Start-Process the child has not
# bound its socket yet, so a Test-Port here loses the race and skips the wait.
$modelReady = $false
if (Test-Port 1234) {
    $modelReady = $true   # LM Studio / other: assume whatever is there is usable
} elseif ($externalLlm -and (Test-Port $llmPort)) {
    $modelReady = $true
    Write-Host "[mira] external LLM on :$llmPort (from LLAMA_URL) - bundled 4B not started"
} elseif ($modelWanted) {
    Write-Host "[mira] waiting for the 4B model to load (first run 5-15s) ..."
    Write-Host "       (leave this window open - do not double-click start.bat again)"
    $modelReady = Wait-Url "http://127.0.0.1:8080/health" 90 "the 4B model"
}

# -------------------------------------------------------------------- tts ----
$neuralScript = Join-Path $PSScriptRoot "tools\tts\start_neural.ps1"
$neuralPy = "C:\Users\QQ\miniconda3\envs\mira-tts\python.exe"
# 2026-10-21 rollback: the default engine now comes from the CARD
# (cards/mira_cafe/card.yaml -> character.voice.engine, currently "edge"), because the
# neural path has two measured defects that are not fixed yet:
#   1) instruct2 speaks the emotion instruction out loud as dialogue (wrong content;
#      evidence in docs/VOICE_LEAK_2026-10-21.md)
#   2) sidecar RTF 2.3 -> audio can never catch up with the subtitles (22s of wall
#      time for 9.2s of speech)
# When the card says edge, do not start the sidecar at all: it saves ~20s of startup
# and ~2GB of VRAM (VRAM pressure also slows the 4B model, which the user reads as
# "it got laggy"). To try neural again: $env:MIRA_TTS_ENGINE='neural' (or 'auto').
$cardEngine = ""
if (Test-Path $py) {
    try {
        $cardEngine = [string](& $py -c "from app import persona; print(getattr(persona, 'VOICE_ENGINE', '') or '')" 2>$null | Select-Object -Last 1)
        $cardEngine = $cardEngine.Trim()
    } catch { $cardEngine = "" }
}
$ttsEngine = if ($env:MIRA_TTS_ENGINE) { $env:MIRA_TTS_ENGINE } elseif ($cardEngine) { $cardEngine } else { "auto" }
$ttsWanted = $false
if ($ttsEngine -eq "edge") {
    Write-Host "[mira] TTS engine = edge (card or env) -> neural sidecar skipped (saves ~20s + 2GB vram)"
} elseif (Test-Port 8790) {
    $ttsWanted = $true
} elseif ((Test-Path $neuralScript) -and (Test-Path $neuralPy)) {
    $ttsWanted = $true
    Write-Host "[mira] starting the neural TTS sidecar on :8790 (first load ~15s; edge-tts meanwhile) ..."
    Start-Process -FilePath "powershell" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $neuralScript `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized | Out-Null
}

$ttsReady = $false
if ($ttsWanted) {
    Write-Host "[mira] waiting for the neural TTS to warm up (~15-20s: weights + one warmup synth) ..."
    $ttsReady = Wait-Url "http://127.0.0.1:8790/health" 120 "the neural TTS"
}

# -------------------------------------------------------------------- gpu ----
if ($FreeGpu -and (Test-Port 8188)) {
    Write-Host "[mira] -FreeGpu: stopping ComfyUI on :8188"
    Stop-ByPort 8188
    Start-Sleep -Seconds 3
}

$vramUsed = Get-VramUsedMB
$gpuWarn = $false
if ($vramUsed -gt 9000) {
    $gpuWarn = $true
    # Do not blame ComfyUI by default: on this machine the status table said
    # "comfyui off" right under a warning that blamed ComfyUI. Check the port.
    $comfyHolding = Test-Port 8188
    Write-Host ""
    Write-Host "  ****************************************************************" -ForegroundColor Yellow
    Write-Host ("  GPU is BUSY: {0} MiB already used by some process." -f $vramUsed) -ForegroundColor Yellow
    Write-Host "  While it holds the card this demo degrades: model -> Mock," -ForegroundColor Yellow
    Write-Host "  first audio 30-40s instead of 3-7s." -ForegroundColor Yellow
    if ($comfyHolding) {
        Write-Host "  ComfyUI is listening on :8188 - close it, or rerun with:" -ForegroundColor Yellow
        Write-Host "      .\start.ps1 -FreeGpu" -ForegroundColor Yellow
    } else {
        Write-Host "  :8188 is NOT listening, so this is some other process." -ForegroundColor Yellow
        Write-Host "  Find it with:  nvidia-smi" -ForegroundColor Yellow
    }
    Write-Host "  ****************************************************************" -ForegroundColor Yellow
    Write-Host ""
}

# ---------------------------------------------------------------- browser ----
if (-not $NoBrowser) {
    # Wait for the app to answer, then open the browser. Runs detached so the
    # foreground uvicorn below stays interruptible with Ctrl+C.
    $waitCmd = "for(`$i=0;`$i -lt 240;`$i++){ try { `$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:$Port/api/health'; if(`$r.StatusCode -eq 200){ Start-Process 'http://127.0.0.1:$Port/'; break } } catch {} ; Start-Sleep -Milliseconds 500 }"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($waitCmd))
    Start-Process -FilePath "powershell" `
        -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", $b64 | Out-Null
}

# ---------------------------------------------------------------- status -----
Write-Host ""
Write-Host "  ---------------- Mira scene ----------------"
Write-Host ("  app        http://127.0.0.1:{0}/  (starting, browser opens when ready)" -f $Port)
Write-Host ("  llm mode   {0}" -f $mode)
$modelDetail = "none -> scripted Mock"
$modelOk = $false
if (Test-Port 1234) {
    $modelDetail = "strata :1234"
    $modelOk = $true
} elseif ($externalLlm -and (Test-Port $llmPort)) {
    $modelOk = $modelReady
    $lmName = if ($env:LLAMA_MODEL) { $env:LLAMA_MODEL } else { "qwen3.8-4b (default)" }
    $modelDetail = ("external LLM :{0} (LLAMA_URL, model {1})" -f $llmPort, $lmName)
} elseif (Test-Port 8080) {
    $modelOk = $modelReady
    if ($modelOk) { $modelDetail = "llama.cpp 4B :8080 ready" }
    else { $modelDetail = "llama.cpp 4B :8080 bound but NOT ready -> Mock risk" }
}
$ttsDetail = "edge-tts fallback (robotic voice)"
$ttsOk = $false
if ($ttsEngine -eq "edge") {
    # edge IS the intended path right now (see the rollback note above), not a fallback
    $ttsOk = $true
    $ttsDetail = "edge-tts (engine=edge, no sidecar)"
} elseif (Test-Port 8790) {
    $ttsOk = $ttsReady
    if ($ttsOk) { $ttsDetail = "cosyvoice2 :8790 ready" }
    else { $ttsDetail = "cosyvoice2 :8790 still loading -> edge fallback" }
}
$comfyDetail = "off"
if (Test-Port 8188) { $comfyDetail = "on :8188 - competes for the GPU" }
Write-Status "model" $modelOk $modelDetail
Write-Status "tts" $ttsOk $ttsDetail
Write-Status "comfyui" (-not $gpuWarn) $comfyDetail
if ($vramUsed -ge 0) { Write-Host ("  vram       {0} / 12282 MiB" -f $vramUsed) }
Write-Host "  demo notes docs\DEMO_CHECKLIST.md   stop: Ctrl+C  (or .\start.ps1 -Stop)"
Write-Host "  --------------------------------------------"
Write-Host ""

# ------------------------------------------------------------------- serve ---
& $py -m uvicorn app.main:app --host 127.0.0.1 --port $Port
