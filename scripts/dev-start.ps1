<#
Glyph development launcher (Windows PowerShell).

What it does:
  1. checks the things needed to run (python env / frontend deps / .env)
  2. opens a window running the backend on 127.0.0.1:8080
  3. waits until the backend actually answers, then opens the frontend window on 5174
  4. waits for the frontend, then opens the browser

Why it exists:
  doing it by hand means two terminals, four commands, and guessing when things are ready.

Usage (from the glyph\ folder):
  .\scripts\dev-start.ps1
  .\scripts\dev-start.ps1 -NoBrowser
  .\scripts\dev-start.ps1 -Force
#>

[CmdletBinding()]
param(
    [switch] $NoBrowser,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

$root         = Split-Path -Parent $PSScriptRoot
$python       = Join-Path $root '.venv\Scripts\python.exe'
$frontendDir  = Join-Path $root 'web-desktop2'
$Ipv4Loopback = '127.0.0.1'
$Ipv6Loopback = '::1'
$backendPort  = 8080
$frontendPort = 5174
$backendUrl   = "http://127.0.0.1:$backendPort"
$frontendUrl  = "http://localhost:$frontendPort"

function Write-Step([string] $text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok([string] $text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Note([string] $text) { Write-Host "    $text" -ForegroundColor Yellow }

# 探测端口用 Test-NetConnection，而不是自建 TcpClient：
#   自建 TcpClient 探 ::1 时在本机 .NET 上始终失败（连上了也报 socket 未连接），
#   结果是"服务其实已经在跑"被误判成没起来（2026-10-14 真实踩到）。
#   Test-NetConnection 对 IPv4 / IPv6 都准，慢 1-2 秒，只在启动时用几次，可以接受。
# 两个回环都试：Vite 默认只绑 IPv6 的 ::1，后端 uvicorn 绑 IPv4 的 127.0.0.1。
function Test-Tcp([string] $Address, [int] $Port) {
    $probe = Test-NetConnection -ComputerName $Address -Port $Port `
        -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
    return [bool] $probe
}

function Test-Port([int] $Port) {
    return ((Test-Tcp $Ipv4Loopback $Port) -or (Test-Tcp $Ipv6Loopback $Port))
}

function Test-OurBackend([string] $Base) {
    try {
        $resp = Invoke-WebRequest -Uri "$Base/api/config" -UseBasicParsing -TimeoutSec 3
        # 用单引号包串、里面不出现双引号：PS 5.1 对这种嵌套引号的解析很挑（2026-10-14 踩到）
        return $resp.Content.Contains('"model"')
    } catch {
        return $false
    }
}

function Stop-Port([int] $Port) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Note "stopping old process on $Port : $($proc.ProcessName) (PID $($proc.Id))"
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 600
}

function Wait-Port([int] $Port, [int] $Seconds, [string] $What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Write-Step 'checking the environment'

if (-not (Test-Path $python)) {
    Write-Host ''
    Write-Host "Python env not found at: $python" -ForegroundColor Red
    Write-Host 'Run these first, from the glyph folder:' -ForegroundColor Red
    Write-Host '  py -3.12 -m venv .venv' -ForegroundColor Red
    Write-Host '  .venv\Scripts\python -m pip install ".[dev]" ".[qwen]" ".[grade]" ".[web]"' -ForegroundColor Red
    Write-Host ''
    exit 1
}
Write-Ok 'python env ready'

$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Note 'no .env found, copied one from .env.example'
    Write-Note 'open glyph\.env and put your own API key in it, otherwise the model will not run'
} else {
    Write-Ok '.env present'
}

if (-not (Test-Path (Join-Path $frontendDir 'node_modules'))) {
    Write-Note 'frontend deps missing, running npm install (the first time takes a few minutes)'
    Push-Location $frontendDir
    try { npm install } finally { Pop-Location }
}
Write-Ok 'frontend deps ready'

Write-Step 'checking the ports'

$reuseBackend = $false
if (Test-Port $backendPort) {
    if ((Test-OurBackend $backendUrl) -or (Test-OurBackend "http://localhost:$backendPort")) {
        Write-Note "a Glyph backend is already running on $backendPort, keeping it"
        $reuseBackend = $true
    } elseif ($Force) {
        Stop-Port $backendPort
    } else {
        Write-Host ''
        Write-Host "Port $backendPort is taken by something that is not this backend." -ForegroundColor Red
        Write-Host 'Close it yourself, or re-run with -Force to stop it for you.' -ForegroundColor Red
        Write-Host ''
        exit 1
    }
}

$reuseFrontend = $false
if (Test-Port $frontendPort) {
    if ($Force) {
        Stop-Port $frontendPort
    } else {
        Write-Note "something is already listening on $frontendPort, assuming it is the frontend"
        $reuseFrontend = $true
    }
}

if (-not $reuseBackend) {
    Write-Step "starting the backend ($backendUrl)"
    $cmd = "[Console]::Title = 'Glyph backend $backendPort'; Set-Location '$root'; & '$python' -m uvicorn web.server:app --host 127.0.0.1 --port $backendPort"
    Start-Process powershell -ArgumentList '-NoExit', '-Command', $cmd | Out-Null

    if (-not (Wait-Port $backendPort 60 'backend')) {
        Write-Host ''
        Write-Host 'The backend did not come up within 60 seconds.' -ForegroundColor Red
        Write-Host "Look at the window titled 'Glyph backend $backendPort' for the error." -ForegroundColor Red
        Write-Host ''
        exit 1
    }
    Write-Ok 'backend is up'
}

if (-not $reuseFrontend) {
    Write-Step "starting the frontend ($frontendUrl)"
    $cmd = "[Console]::Title = 'Glyph frontend $frontendPort'; Set-Location '$frontendDir'; npm run dev -- --strictPort"
    Start-Process powershell -ArgumentList '-NoExit', '-Command', $cmd | Out-Null

    if (-not (Wait-Port $frontendPort 90 'frontend')) {
        Write-Host ''
        Write-Host 'The frontend did not come up within 90 seconds.' -ForegroundColor Red
        Write-Host "Look at the window titled 'Glyph frontend $frontendPort' for the error." -ForegroundColor Red
        Write-Host ''
        exit 1
    }
    Write-Ok 'frontend is up'
}

if (-not $NoBrowser) {
    Write-Step 'opening the browser'
    Start-Process $frontendUrl
}

Write-Host ''
Write-Host "Ready. Open this address: $frontendUrl" -ForegroundColor Green
Write-Host 'Closing those two console windows stops everything, or run .\scripts\dev-stop.ps1' -ForegroundColor DarkGray
Write-Host ''
