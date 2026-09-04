<#
.SYNOPSIS
    Windows task runner -- the PowerShell equivalent of the Makefile.

.DESCRIPTION
    Calls .venv\Scripts\python.exe directly, so you never have to activate the
    virtualenv (and never hit the Activate.ps1 execution-policy prompt).

.EXAMPLE
    .\scripts\tasks.ps1 install
    .\scripts\tasks.ps1 run
    .\scripts\tasks.ps1 sweep -Duration 20

.NOTES
    If PowerShell refuses to run this file, allow scripts for this session only:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'install', 'test', 'run', 'smoke', 'load', 'sweep', 'open', 'demo', 'lint', 'clean')]
    [string]$Task = 'help',

    [string]$Url = 'http://127.0.0.1:8000',
    [int]$Port = 8000,
    [int]$Duration = 15,
    [int]$MaxTokens = 64,
    [string]$Concurrency = '1,2,4,8,16,32',
    [string]$Rps = '5,10,20,40'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

# The project needs 3.10+. `py -3` and the Microsoft Store `python` can both resolve to
# an older interpreter, so probe explicitly and verify the version before using one.
function Test-PythonVersion {
    param([string]$Exe, [string[]]$Prefix)
    try {
        $out = & $Exe @($Prefix + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')) 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $parsed = [version]("$out".Trim())
        if ($parsed -ge [version]'3.10') { return $parsed }
    }
    catch { }
    return $null
}

function Resolve-BasePython {
    $candidates = @(
        @{ Exe = 'py'; Prefix = @('-3.13') },
        @{ Exe = 'py'; Prefix = @('-3.12') },
        @{ Exe = 'py'; Prefix = @('-3.11') },
        @{ Exe = 'py'; Prefix = @('-3.10') },
        @{ Exe = 'py'; Prefix = @('-3') },
        @{ Exe = 'python'; Prefix = @() },
        @{ Exe = 'python3'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $version = Test-PythonVersion -Exe $candidate.Exe -Prefix $candidate.Prefix
        if ($version) {
            $candidate.Version = $version
            return $candidate
        }
    }
    throw @'
No Python 3.10+ found. Install one from https://www.python.org/downloads/
(tick "Add python.exe to PATH"), then re-run:  .\scripts\tasks.ps1 install
'@
}

function Get-VenvVersion {
    if (-not (Test-Path $VenvPython)) { return $null }
    try {
        $out = & $VenvPython -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [version]("$out".Trim()) }
    }
    catch { }
    return $null
}

function Assert-Venv {
    if (-not (Test-Path $VenvPython)) {
        throw 'Virtualenv missing. Run:  .\scripts\tasks.ps1 install'
    }
}

# Explicit array argument (not ValueFromRemainingArguments): PowerShell would try to
# bind a leading "-m" as a parameter name otherwise.
function Invoke-Py {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    Assert-Venv
    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) { throw "python $($Arguments -join ' ') exited with code $LASTEXITCODE" }
}

function Wait-ForReady {
    param([string]$BaseUrl, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            if ((Invoke-WebRequest -Uri "$BaseUrl/readyz" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
                return $true
            }
        }
        catch { }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Show-Help {
    Write-Host ''
    Write-Host 'Usage: .\scripts\tasks.ps1 <task> [options]' -ForegroundColor Cyan
    Write-Host ''
    $tasks = @(
        @('install', 'create .venv and install runtime + dev dependencies'),
        @('run', 'start the server (mock backend) on -Port'),
        @('test', 'run the test suite'),
        @('smoke', 'stream one completion and print TTFT / ITL'),
        @('load', 'single-level load test at concurrency 16'),
        @('sweep', 'closed-loop concurrency sweep -> results\sweep.json'),
        @('open', 'open-loop arrival-rate sweep -> results\open.json'),
        @('demo', 'boot server, stream, overload it, show counters, stop'),
        @('lint', 'ruff check'),
        @('clean', 'remove .venv, caches and results')
    )
    foreach ($entry in $tasks) { Write-Host ('  {0,-8} {1}' -f $entry[0], $entry[1]) }
    Write-Host ''
    Write-Host 'Options: -Url -Port -Duration -MaxTokens -Concurrency -Rps' -ForegroundColor DarkGray
    Write-Host ''
}

$previousLocation = Get-Location
Set-Location $Root
try {
    switch ($Task) {

        'help' { Show-Help }

        'install' {
            $existing = Get-VenvVersion
            if ($existing -and $existing -lt [version]'3.10') {
                Write-Host "Existing .venv is Python $existing (need 3.10+); recreating..." -ForegroundColor Yellow
                Remove-Item -Recurse -Force '.venv'
                $existing = $null
            }
            if (-not $existing) {
                $base = Resolve-BasePython
                Write-Host "Creating .venv with $($base.Exe) $($base.Prefix -join ' ') (Python $($base.Version))..." -ForegroundColor Cyan
                & $base.Exe @($base.Prefix + @('-m', 'venv', '.venv'))
                if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
            }
            else {
                Write-Host "Reusing existing .venv (Python $existing)." -ForegroundColor DarkGray
            }
            Invoke-Py @('-m', 'pip', 'install', '--upgrade', 'pip')
            Invoke-Py @('-m', 'pip', 'install', '-r', 'requirements-dev.txt')
            Write-Host 'Done. Next:  .\scripts\tasks.ps1 test' -ForegroundColor Green
        }

        'test' { Invoke-Py @('-m', 'pytest', '-q') }

        'lint' { Invoke-Py @('-m', 'ruff', 'check', 'llmserve', 'loadtest', 'tests') }

        'run' {
            if (-not $env:LLMSERVE_BACKEND) { $env:LLMSERVE_BACKEND = 'mock' }
            $env:LLMSERVE_PORT = "$Port"
            Write-Host "Serving the $env:LLMSERVE_BACKEND backend on http://127.0.0.1:$Port  (Ctrl+C to stop)" -ForegroundColor Cyan
            Invoke-Py @('-m', 'llmserve')
        }

        'smoke' {
            Invoke-Py @('scripts\stream_client.py', '--base-url', $Url, '--max-tokens', '32')
        }

        'load' {
            Invoke-Py @('-m', 'loadtest', '--base-url', $Url, '--concurrency', '16',
                '--duration', "$Duration", '--max-tokens', "$MaxTokens")
        }

        'sweep' {
            Invoke-Py @('-m', 'loadtest', '--base-url', $Url, '--concurrency', $Concurrency,
                '--duration', "$Duration", '--max-tokens', "$MaxTokens",
                '--json', 'results\sweep.json')
        }

        'open' {
            Invoke-Py @('-m', 'loadtest', '--base-url', $Url, '--rps', $Rps,
                '--duration', "$Duration", '--max-tokens', "$MaxTokens",
                '--json', 'results\open.json')
        }

        'demo' {
            Assert-Venv
            $env:LLMSERVE_BACKEND = 'mock'
            $env:LLMSERVE_PORT = "$Port"
            $env:LLMSERVE_MAX_CONCURRENT_REQUESTS = '4'
            $env:LLMSERVE_MAX_QUEUE_SIZE = '8'
            $env:LLMSERVE_QUEUE_TIMEOUT_S = '5'
            $env:LLMSERVE_LOG_JSON = 'false'

            Write-Host '== starting server (4 slots, queue 8) ==' -ForegroundColor Cyan
            $server = Start-Process -FilePath $VenvPython -ArgumentList '-m', 'llmserve' -PassThru -NoNewWindow
            try {
                if (-not (Wait-ForReady -BaseUrl $Url)) { throw 'server did not become ready in time' }
                Write-Host 'ready.' -ForegroundColor Green

                Write-Host "`n== streaming one completion ==" -ForegroundColor Cyan
                Invoke-Py @('scripts\stream_client.py', '--base-url', $Url, '--max-tokens', '32')

                Write-Host "`n== open-loop overload: watch shed rate and TTFT ==" -ForegroundColor Cyan
                Invoke-Py @('-m', 'loadtest', '--base-url', $Url, '--rps', '5,20,60',
                    '--duration', '8', '--max-tokens', '48')

                Write-Host "`n== admission control counters ==" -ForegroundColor Cyan
                Invoke-RestMethod -Uri "$Url/stats" | ConvertTo-Json -Depth 5
            }
            finally {
                if ($server -and -not $server.HasExited) {
                    Write-Host "`nstopping server..." -ForegroundColor DarkGray
                    Stop-Process -Id $server.Id -Force
                }
            }
        }

        'clean' {
            foreach ($path in @('.venv', '.pytest_cache', '.ruff_cache', 'results')) {
                if (Test-Path $path) { Remove-Item -Recurse -Force $path }
            }
            Get-ChildItem -Path $Root -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            Write-Host 'cleaned.' -ForegroundColor Green
        }
    }
}
finally {
    Set-Location $previousLocation
}
