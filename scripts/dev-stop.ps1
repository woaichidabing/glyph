<#
Glyph development stopper (Windows PowerShell).

What it does:
  stops whatever is listening on the backend port (8080) and the frontend port (5174).
  It only targets processes that are actually listening on those two ports.

Why it exists:
  the launcher opens two console windows; if you lose track of them, the ports stay busy
  and the next start fails.

Usage (from the glyph\ folder):
  .\scripts\dev-stop.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$backendPort  = 8080
$frontendPort = 5174

function Write-Step([string] $text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Note([string] $text) { Write-Host "    $text" -ForegroundColor Yellow }

function Stop-Port([int] $Port) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Note "nothing is listening on $Port"
        return
    }
    foreach ($c in $conns) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        Write-Note "stopping $($proc.ProcessName) (PID $($proc.Id)) on port $Port"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Step 'stopping the backend'
Stop-Port $backendPort

Write-Step 'stopping the frontend'
Stop-Port $frontendPort

Start-Sleep -Milliseconds 500

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host 'Note: this stops the servers, not the console windows they were started in.' -ForegroundColor DarkGray
Write-Host 'You can close those windows now.' -ForegroundColor DarkGray
Write-Host ''
