#requires -Version 5.1
<#
.SYNOPSIS
    Vocal Separator - block until both services answer, then exit.

.DESCRIPTION
    start.bat used to poll the two services from inside a batch :wait_loop,
    spawning a fresh powershell.exe process on every iteration (up to 45 of
    them). Process startup dominated the wait, so a healthy start still burned
    seconds for nothing.

    This script does the same polling internally in one process: it waits for
      * the renderer to answer HTTP 200, and
      * the FastAPI backend to answer /api/health with status "ok",
    then exits 0. It exits 1 only when the deadline passes.

    Ports come from the caller so there is a single source of truth.

.PARAMETER FrontendUrl
    Renderer URL that must return HTTP 200.

.PARAMETER BackendUrl
    Backend health endpoint that must return JSON with status "ok".

.PARAMETER TimeoutSec
    Overall deadline in seconds. Default 45.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\wait-for-services.ps1 `
        -FrontendUrl http://127.0.0.1:3000 -BackendUrl http://127.0.0.1:8000/api/health
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $FrontendUrl,
    [Parameter(Mandatory = $true)][string] $BackendUrl,
    [int] $TimeoutSec = 45,
    [int] $PollIntervalMs = 500
)

$ErrorActionPreference = 'SilentlyContinue'
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$frontendReadyAt = $null
$backendReadyAt = $null

function Test-Frontend {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $FrontendUrl -TimeoutSec 3
        return ($response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}

function Test-Backend {
    try {
        $payload = Invoke-RestMethod -Uri $BackendUrl -TimeoutSec 3
        return ($payload.status -eq 'ok')
    }
    catch {
        return $false
    }
}

while ((Get-Date) -lt $deadline) {
    if ($null -eq $frontendReadyAt -and (Test-Frontend)) {
        $frontendReadyAt = Get-Date
        Write-Host ('      renderer answered HTTP 200 at {0}' -f $frontendReadyAt.ToString('HH:mm:ss'))
    }
    if ($null -eq $backendReadyAt -and (Test-Backend)) {
        $backendReadyAt = Get-Date
        Write-Host ('      backend health is "ok" at {0}' -f $backendReadyAt.ToString('HH:mm:ss'))
    }
    if ($null -ne $frontendReadyAt -and $null -ne $backendReadyAt) {
        exit 0
    }
    Start-Sleep -Milliseconds $PollIntervalMs
}

if ($null -eq $frontendReadyAt) { Write-Host '      renderer never answered HTTP 200.' }
if ($null -eq $backendReadyAt) { Write-Host '      backend health never returned "ok".' }
exit 1
