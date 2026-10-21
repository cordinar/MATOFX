# Start matofx-api and matofx-runner in two new PowerShell windows.
#
# Self-locates from $PSScriptRoot - drop this script into your APP_HOME
# (the folder that contains `venv\` and `matofx.toml`) and either:
#
#   - Right-click -> Run with PowerShell
#   - Or double-click the matching .bat shim (matofx-start.bat)
#   - Or from a shell:  .\matofx-start.ps1
#
# Each service spawns in its own window so you can watch the logs live.
# Close a window or Ctrl+C in it to stop that service.
# `matofx-stop.ps1` is the hammer if a window has been lost.

[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$AppHome   = $PSScriptRoot
$VenvBin   = Join-Path $AppHome "venv\Scripts"
$ConfigToml= Join-Path $AppHome "matofx.toml"
$ApiExe    = Join-Path $VenvBin "matofx-api.exe"
$RunnerExe = Join-Path $VenvBin "matofx-runner.exe"

if (-not (Test-Path $ApiExe))    { throw "matofx-api.exe not found at $ApiExe - is the venv at $VenvBin?" }
if (-not (Test-Path $RunnerExe)) { throw "matofx-runner.exe not found at $RunnerExe - is the venv at $VenvBin?" }
if (-not (Test-Path $ConfigToml)){ throw "matofx.toml not found at $ConfigToml" }

Write-Host "==> MATOFX home:   $AppHome"
Write-Host "==> Using config:  $ConfigToml"

# Each spawned window inherits these env vars; -NoExit keeps the window
# open after the service stops so the user sees the exit message.
$apiArgs = @(
    "-NoExit",
    "-NoLogo",
    "-Command",
    "`$Host.UI.RawUI.WindowTitle='MATOFX API + UI'; `$env:MATOFX_CONFIG='$ConfigToml'; & '$ApiExe'"
)
$runnerArgs = @(
    "-NoExit",
    "-NoLogo",
    "-Command",
    "`$Host.UI.RawUI.WindowTitle='MATOFX Runner'; `$env:MATOFX_CONFIG='$ConfigToml'; & '$RunnerExe'"
)

Start-Process powershell -ArgumentList $apiArgs    -WindowStyle Normal | Out-Null
Start-Sleep -Milliseconds 800
Start-Process powershell -ArgumentList $runnerArgs -WindowStyle Normal | Out-Null

Write-Host ""
Write-Host "MATOFX is starting in two new PowerShell windows."
Write-Host "  Login:  http://127.0.0.1:8000/ui/login"
Write-Host "  Stop:   .\matofx-stop.ps1   (or Ctrl+C in each window)"
Write-Host ""

if (-not $NoBrowser) {
    Start-Sleep -Seconds 3
    Start-Process "http://127.0.0.1:8000/ui/login"
}
