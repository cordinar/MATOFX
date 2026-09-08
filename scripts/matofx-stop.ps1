# Force-stop matofx-api and matofx-runner.
#
# Prefer Ctrl+C in the service windows for a graceful shutdown; this script
# is the hammer for the case where a window has been lost or the service
# is unresponsive.

[CmdletBinding()]
param()

$Killed = 0
Get-Process -Name "matofx-api", "matofx-runner" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Stopping $($_.ProcessName) (PID $($_.Id))"
    try {
        $_ | Stop-Process -Force
        $Killed++
    } catch {
        Write-Warning "Could not stop $($_.ProcessName) (PID $($_.Id)): $_"
    }
}

# Also catch uvicorn workers spawned by matofx-api whose process name
# might be python.exe (depending on Python build / NSSM wrapping).
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match "matofx-(api|runner)|matofx\.api\.daemon|matofx\.runner\.daemon" } |
    ForEach-Object {
        Write-Host "Stopping orphan python (PID $($_.ProcessId)): $($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length)))"
        try {
            Stop-Process -Id $_.ProcessId -Force
            $Killed++
        } catch {
            Write-Warning "Could not stop python PID $($_.ProcessId): $_"
        }
    }

if ($Killed -eq 0) {
    Write-Host "Nothing to stop — matofx-api and matofx-runner aren't running."
} else {
    Write-Host "Stopped $Killed process(es)."
}
