[CmdletBinding()]
param(
    [string]$BaseRequestDirectory
)

$ErrorActionPreference = 'Stop'
$courierRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$courierPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if ([string]::IsNullOrWhiteSpace($BaseRequestDirectory)) {
    $BaseRequestDirectory = Join-Path $courierRoot 'outbox\COURIER_DIRECT_PROBE\COURIER-DIRECT-PROBE-20260825-001'
}
$baseRequest = [System.IO.Path]::GetFullPath($BaseRequestDirectory)
$manifest = Join-Path $baseRequest 'request.json'
$probe = Join-Path $PSScriptRoot 'courier_pre_browser_probe.py'
$log = Join-Path $courierRoot ('.courier-pre-browser-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.jsonl')

if (-not (Test-Path -LiteralPath $courierPython -PathType Leaf)) { throw "Python not found: $courierPython" }
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Request manifest not found: $manifest" }
if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) { throw "Probe not found: $probe" }

$env:PYTHONPATH = $courierRoot
$env:CHAT_COURIER_EXPECTED_SOURCE_ROOT = $courierRoot
Write-Host 'This probe creates a temporary local request and never starts Chrome, Chat, or network.'
Write-Host "Base request (read only): $baseRequest"
Write-Host "Log: $log"
& $courierPython $probe $baseRequest --log $log
$code = $LASTEXITCODE
Write-Host "Probe exit code: $code"
Write-Host "Saved log: $log"
exit $code
