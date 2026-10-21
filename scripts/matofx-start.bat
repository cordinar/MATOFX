@echo off
REM Double-click shim for matofx-start.ps1.
REM Lets the user start MATOFX from Explorer without configuring PowerShell
REM execution policy globally - ExecutionPolicy Bypass applies to this run only.
SET SCRIPT_DIR=%~dp0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%matofx-start.ps1" %*
