# One-shot Windows installer for a MATOFX bundle.
#
# Executes the steps in INSTALL.md Part B B3..B12 end-to-end. Asks for the
# super-user password interactively. Run as a normal user from inside the
# extracted bundle directory.
#
# Usage (from the bundle dir):
#   .\scripts\install_windows.ps1
#   .\scripts\install_windows.ps1 -AppHome "D:\matofx" -UseExampleTools:$false

[CmdletBinding()]
param(
    [string]$AppHome      = "C:\ProgramData\matofx",
    [switch]$UseExampleTools = $true,
    [string]$AdminUsername   = "admin",
    [string]$AdminEmail      = "admin@example.com",
    [string]$AdminFirstName  = "Site",
    [string]$AdminLastName   = "Administrator"
)

$ErrorActionPreference = "Stop"
$Bundle = Split-Path -Parent $PSScriptRoot
Write-Host "==> MATOFX bundle: $Bundle"
Write-Host "==> Install root:  $AppHome"

# B3
New-Item -ItemType Directory -Force `
  $AppHome, "$AppHome\db", "$AppHome\logs", "$AppHome\jobs" | Out-Null

# B4
if (-not (Test-Path "$AppHome\venv")) {
    Write-Host "==> Creating virtualenv"
    python -m venv "$AppHome\venv"
}
$Py  = "$AppHome\venv\Scripts\python.exe"
$Pip = "$AppHome\venv\Scripts\pip.exe"
& $Py -m pip install --upgrade pip | Out-Null

# B5
& $Pip config set global.require-hashes true | Out-Null

# B6 - the lockfile contains both deps and the matofx wheel itself (file: entry
# with --hash). Run pip install from inside the bundle so the relative path
# resolves. One command installs everything under --require-hashes.
$Lock  = Join-Path $Bundle "requirements.lock"
$Wheelhouse = Join-Path $Bundle "wheelhouse"
Push-Location $Bundle
try {
    if (Test-Path $Wheelhouse) {
        Write-Host "==> Installing offline from wheelhouse"
        & $Pip install --no-index --find-links $Wheelhouse --requirement $Lock
    } else {
        Write-Host "==> Installing online (PyPI)"
        & $Pip install --requirement $Lock
    }
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

# B7 - install + Windows-ify the default config.
$ConfigDst = Join-Path $AppHome "matofx.toml"
if (-not (Test-Path $ConfigDst)) {
    Copy-Item (Join-Path $Bundle "matofx.toml") $ConfigDst
    Write-Host "==> Wrote default config at $ConfigDst"

    # The bundled template ships with Linux defaults. Substitute Windows paths
    # so the install works on this box without manual editing. Forward slashes
    # because TOML treats backslash as an escape.
    $configText = Get-Content $ConfigDst -Raw
    $appHomeFwd = $AppHome.Replace('\','/')
    $configText = $configText -replace '/etc/matofx/audit-signing\.key', "$appHomeFwd/audit-signing.key"
    $configText = $configText -replace 'sqlite:///var/lib/matofx/matofx\.db', "sqlite:///$appHomeFwd/db/matofx.db"
    # Default to SQLite for a quick local install; admin can flip to Postgres.
    $configText = $configText -replace '(?m)^dialect\s*=\s*"postgres"',     'dialect = "sqlite"'
    $configText = $configText -replace '(?m)^environment\s*=\s*"prod"',     'environment = "staging"'
    $configText = $configText -replace '(?m)^require_signing\s*=\s*true',   'require_signing = false'
    $configText = $configText -replace '(?m)^url\s*=\s*"postgres://[^"]+"', "url = `"sqlite:///$appHomeFwd/db/matofx.db`""
    # Write UTF-8 *without* a BOM. Windows PowerShell 5.1's `Set-Content -Encoding
    # UTF8` prepends a BOM, which Python's tomllib rejects ("not valid TOML") when
    # it loads the config at startup. WriteAllText with UTF8Encoding($false) emits
    # no BOM on either PowerShell edition.
    [System.IO.File]::WriteAllText($ConfigDst, $configText, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "==> Patched config with Windows paths (signing key, SQLite DB)"
} else {
    Write-Host "==> Keeping existing config at $ConfigDst"
}

# B8
[Environment]::SetEnvironmentVariable("MATOFX_CONFIG", $ConfigDst, "Machine")
$env:MATOFX_CONFIG = $ConfigDst

# B9
$KeyPath = Join-Path $AppHome "audit-signing.key"
if (-not (Test-Path $KeyPath)) {
    Write-Host "==> Generating audit signing key at $KeyPath"
    & $Py -c "from pathlib import Path; from matofx.audit import generate_signing_key; generate_signing_key(Path(r'$KeyPath'))"
} else {
    Write-Host "==> Keeping existing audit key at $KeyPath"
}

# B10
Write-Host "==> Applying database migrations"
& $Py (Join-Path $Bundle "scripts\matofx_migrate.py") upgrade head

# B11
Write-Host "==> Creating first super-user '$AdminUsername'"
$Pass = Read-Host -AsSecureString -Prompt "Password for $AdminUsername"
$PassPlain = [System.Net.NetworkCredential]::new("", $Pass).Password
& "$AppHome\venv\Scripts\matofx.exe" admin create-first-user `
    --username   $AdminUsername `
    --email      $AdminEmail `
    --first-name $AdminFirstName `
    --last-name  $AdminLastName `
    --password   $PassPlain
$PassPlain = $null

# Default branding (header icon + favicon). Idempotent: only writes a DB row
# when the matching general_setting is still null, so an admin's later upload
# is preserved across re-runs / upgrades. No-op if the bundle ships without
# a branding/ directory.
if (Test-Path (Join-Path $Bundle "branding")) {
    Write-Host "==> Seeding default branding"
    & $Py (Join-Path $Bundle "scripts\seed_default_branding.py")
}

# B12
if ($UseExampleTools) {
    Write-Host "==> Seeding example tools"
    & $Py (Join-Path $Bundle "scripts\seed_example_tools.py")
}

# Copy launcher shims into APP_HOME so the operator can double-click /
# right-click -> Run with PowerShell from Explorer.
Write-Host "==> Installing launcher scripts into $AppHome"
foreach ($name in @("matofx-start.ps1", "matofx-stop.ps1", "matofx-start.bat", "matofx-stop.bat")) {
    Copy-Item -Force (Join-Path $Bundle "scripts\$name") (Join-Path $AppHome $name)
}

Write-Host ""
Write-Host "MATOFX installed."
Write-Host "  Quick start:    double-click  $AppHome\matofx-start.bat"
Write-Host "                  (or right-click matofx-start.ps1 -> Run with PowerShell)"
Write-Host "  Stop:           $AppHome\matofx-stop.bat"
Write-Host "  Manual API:     `$env:MATOFX_CONFIG = '$ConfigDst'; & '$AppHome\venv\Scripts\matofx-api.exe'"
Write-Host "  Manual runner:  `$env:MATOFX_CONFIG = '$ConfigDst'; & '$AppHome\venv\Scripts\matofx-runner.exe'"
Write-Host "  Login:          http://127.0.0.1:8000/ui/login"
