[CmdletBinding()]
param(
    [string]$RequestDirectory
)

$ErrorActionPreference = 'Stop'
$courierRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$courierPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if ([string]::IsNullOrWhiteSpace($RequestDirectory)) {
    $RequestDirectory = Join-Path $courierRoot 'outbox\COURIER_DIRECT_PROBE\COURIER-DIRECT-PROBE-20260825-001'
}
$request = [System.IO.Path]::GetFullPath($RequestDirectory)
$manifest = Join-Path $request 'request.json'
$probe = Join-Path $PSScriptRoot 'courier_event_write_probe.py'
$log = Join-Path $courierRoot ('.courier-event-write-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.jsonl')

if (-not (Test-Path -LiteralPath $courierPython -PathType Leaf)) { throw "Python not found: $courierPython" }
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Request manifest not found: $manifest" }
if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) { throw "Probe not found: $probe" }

$env:PYTHONPATH = $courierRoot
$env:CHAT_COURIER_EXPECTED_SOURCE_ROOT = $courierRoot
Write-Host 'This probe does not start Courier, Chrome, a queue, WSL, or network.'
Write-Host "Request directory: $request"
Write-Host "Log: $log"
& $courierPython $probe $request --log $log
$code = $LASTEXITCODE
Write-Host "Probe exit code: $code"
Write-Host "Saved log: $log"
exit $code
