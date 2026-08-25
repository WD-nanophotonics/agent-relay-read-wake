[CmdletBinding()]
param([int]$Seconds = 30)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$probe = Join-Path $PSScriptRoot 'console_signal_probe.py'
$log = Join-Path $root ('.courier-console-signal-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.jsonl')

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) { throw "Probe not found: $probe" }

Write-Host "This probe does not use Courier, Chrome, WSL, or network."
Write-Host "Log: $log"
& $python $probe --seconds $Seconds --log $log
$code = $LASTEXITCODE
Write-Host "Probe exit code: $code"
Write-Host "Saved log: $log"
exit $code
