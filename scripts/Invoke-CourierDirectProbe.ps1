[CmdletBinding()]
param(
    [string]$RequestDirectory
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RequestDirectory)) {
    $RequestDirectory = Join-Path $PSScriptRoot '..\outbox\COURIER_DIRECT_PROBE\COURIER-DIRECT-PROBE-20260825-001'
}

$courierRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$courierPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$request = [System.IO.Path]::GetFullPath($RequestDirectory)
$manifest = Join-Path $request 'request.json'
$receipt = Join-Path $request 'receipt.json'

if (-not (Test-Path -LiteralPath $courierPython -PathType Leaf)) {
    throw "Approved Courier Python not found: $courierPython"
}
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "Request manifest not found: $manifest"
}

Write-Host "Courier source   : $courierRoot"
Write-Host "Courier Python   : $courierPython"
Write-Host "Request directory: $request"
Write-Host "Transport        : Windows direct Python Courier (no WSL, cmd, or bridge)"
Write-Host "The process will remain in this PowerShell window until Courier exits."

$env:PYTHONPATH = $courierRoot
$env:CHAT_COURIER_EXPECTED_SOURCE_ROOT = $courierRoot

Write-Host "--- validate ---"
& $courierPython -m chat_courier.cli validate $request
if ($LASTEXITCODE -ne 0) {
    throw "Courier validation failed with exit code $LASTEXITCODE"
}

Write-Host "--- run ---"
& $courierPython -m chat_courier.cli run $request
$courierExit = $LASTEXITCODE

Write-Host "--- final receipt ---"
if (Test-Path -LiteralPath $receipt -PathType Leaf) {
    Get-Content -Raw -LiteralPath $receipt
} else {
    Write-Warning "No receipt.json was produced."
}

exit $courierExit
