<#
.SYNOPSIS
  Drives the 90-second demo, one beat at a time, pausing between so you can record.

.DESCRIPTION
  Starts the server on the mock backend (no GPU needed), then walks the beats:

    1. one streaming request                       -> 15-30s
    2. traffic ramped past capacity, live          -> 30-60s
    3. sweep + chart                               -> 60-80s

  Each beat waits for Enter first, so you can start recording, hit Enter, and get
  a clean take. Ctrl-C at any point; the server is stopped on the way out.

.EXAMPLE
  .\demo\run_demo.ps1
  .\demo\run_demo.ps1 -Beat 2          # just the overload beat
  .\demo\run_demo.ps1 -NoPause         # unattended, for checking it all works
#>
[CmdletBinding()]
param(
  [ValidateSet(0, 1, 2, 3)] [int] $Beat = 0,
  [int]    $Port        = 8000,
  [int]    $Concurrency = 8,
  # 16, not the shipped default of 64. At ~4 req/s drain, a 64-deep queue is a
  # ~15-second wait -- the demo spends its whole budget watching a progress bar.
  # 16 keeps the worst wait near 4s, and the shed behaviour is identical.
  [int]    $QueueSize   = 16,
  [switch] $NoPause
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$python = if (Test-Path '.\.venv\Scripts\python.exe') { '.\.venv\Scripts\python.exe' } else { 'python' }
$base   = "http://127.0.0.1:$Port"
$server = $null

function Say([string]$text) { Write-Host "`n$text`n" -ForegroundColor Cyan }
function Beat([string]$title) {
  Write-Host ''
  Write-Host ('-' * 72) -ForegroundColor DarkGray
  Write-Host "  $title" -ForegroundColor White
  Write-Host ('-' * 72) -ForegroundColor DarkGray
  if (-not $NoPause) { Read-Host '  press Enter when you are recording' | Out-Null }
}

function Start-Server {
  Say "starting server: $Concurrency concurrent, queue $QueueSize, mock backend"
  $env:LLMSERVE_BACKEND                 = 'mock'
  $env:LLMSERVE_MAX_CONCURRENT_REQUESTS = "$Concurrency"
  $env:LLMSERVE_MAX_QUEUE_SIZE          = "$QueueSize"
  $env:LLMSERVE_LOG_JSON                = 'false'
  $env:LLMSERVE_PORT                    = "$Port"

  $script:server = Start-Process -FilePath $python -ArgumentList '-m', 'llmserve' `
                                 -PassThru -NoNewWindow
  foreach ($i in 1..40) {
    Start-Sleep -Milliseconds 250
    try {
      if ((Invoke-WebRequest "$base/readyz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
        Write-Host '  ready' -ForegroundColor Green; return
      }
    } catch { }
  }
  throw "server did not become ready on $base"
}

function Stop-Server {
  if ($script:server -and -not $script:server.HasExited) {
    Say 'stopping server (graceful drain)'
    Stop-Process -Id $script:server.Id -ErrorAction SilentlyContinue
  }
}

try {
  Start-Server

  if ($Beat -in 0, 1) {
    Beat 'BEAT 1  (0:15-0:30)  one request, tokens streaming back'
    & $python .\scripts\stream_client.py --base-url $base --max-tokens 48
  }

  if ($Beat -in 0, 2) {
    Beat 'BEAT 2  (0:30-1:00)  ramp past capacity -- 200s continue, excess gets 429'
    & $python -m demo.live_traffic --base-url $base --ramp 4,8,16,32 --hold 7
  }

  if ($Beat -in 0, 3) {
    Beat 'BEAT 3  (1:00-1:20)  sweep, then the chart'
    # Measured in-process rather than through `python -m loadtest`, because the
    # HTTP harness currently mis-measures under overload: _open_loop chains
    # relative sleeps (drifts, under-offers) and run_phase's wall clock includes
    # the post-arrival drain, so every rate is divided by an inflated window.
    # Same AdmissionController and MockEngine either way -- see demo/README.md.
    & $python -m demo.measure_inprocess --rps 2,4,6,8,12,16,24,32 `
        --duration 16 --warmup 4 --max-concurrent $Concurrency `
        --max-queue-size $QueueSize --json demo\out\sweep.json
    & $python -m demo.chart demo\out\sweep.json -o demo\out\chart.html
    $chart = Join-Path $root 'demo\out\chart.html'
    Say "opening $chart"
    Start-Process $chart
  }

  Say 'done. Beat 4 is the architecture diagram (demo\architecture.svg) and the takeaway.'
}
finally {
  Stop-Server
  Pop-Location
}
