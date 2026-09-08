# Build a deployable MATOFX bundle.
#
# Run from the repository root:
#   .\scripts\build_bundle.ps1            # bundle as a directory
#   .\scripts\build_bundle.ps1 -Zip       # also produce a .zip archive
#
# Requires the dev venv at .\.venv with [dev,supply-chain] extras installed.
# Produces .\dist\matofx-bundle-<version>\.

[CmdletBinding()]
param(
    [switch]$Zip,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$Root         = Split-Path -Parent $PSScriptRoot
# The repo root is scripts/'s parent, and the Python project now lives
# at that root (it used to sit one level down, in matofx/).
$Project      = $Root
$DistRoot     = Join-Path $Root "dist"
$BundleStage  = $null
$Version      = $null

function Find-Python {
    if ($PythonExe -and (Test-Path $PythonExe)) { return $PythonExe }
    $venvPy = Join-Path $Project ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Cannot find a usable Python. Pass -PythonExe <path> or create .\.venv first."
}

function Read-Version {
    $pyproject = Join-Path $Project "pyproject.toml"
    $match = Select-String -Path $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) { throw "Could not read version from $pyproject" }
    return $match.Matches.Groups[1].Value
}

function Ensure-Tools($Py) {
    Write-Host "==> Checking build tooling in $Py"
    & $Py -c "import build, piptools, cyclonedx_py" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Missing build tooling. Install with: $Py -m pip install -e ""$Project[dev,supply-chain]"""
    }
}

function Build-Wheel($Py) {
    Write-Host "==> Building wheel"
    Push-Location $Project
    try {
        # Suppress the NativeCommandError wrapping that Windows PowerShell 5.1
        # applies to native-command stderr. The build's actual progress lines
        # go to stdout; only suppress the noise channel.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $Py -m build --wheel | Out-Host
        $rc = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($rc -ne 0) { throw "wheel build failed" }
    } finally {
        Pop-Location
    }
    $wheel = Get-ChildItem (Join-Path $Project "dist") -Filter "matofx-*.whl" |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $wheel) { throw "No wheel produced" }
    return $wheel.FullName
}

function Verify-WheelContents($WheelPath) {
    # Hatchling applies .gitignore patterns to wheel file selection, so an
    # over-broad ignore (e.g. "secrets/") silently drops a whole source
    # package from the wheel - it shipped once as "No module named
    # matofx.secrets" on a deployed install. Fail the build if any .py
    # under src/matofx is missing from the wheel.
    # (ASCII only in this file: PS 5.1 parses BOM-less files as ANSI.)
    Write-Host "==> Verifying wheel contains every source module"
    $srcRoot = Join-Path $Project "src\matofx"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($WheelPath)
    try {
        $inWheel = @{}
        foreach ($e in $zip.Entries) { $inWheel[$e.FullName] = $true }
    } finally {
        $zip.Dispose()
    }
    $missing = @(
        Get-ChildItem $srcRoot -Recurse -Filter *.py |
            Where-Object { $_.FullName -notmatch '__pycache__' } |
            ForEach-Object {
                "matofx/" + $_.FullName.Substring($srcRoot.Length + 1).Replace('\', '/')
            } |
            Where-Object { -not $inWheel.ContainsKey($_) }
    )
    if ($missing.Count -gt 0) {
        $missing | ForEach-Object { Write-Host "  MISSING: $_" }
        throw "Wheel is missing $($missing.Count) source file(s) - check .gitignore patterns (hatchling honours them)."
    }
}

function Compile-Lockfile($Py) {
    Write-Host "==> Generating hashed lockfile"
    $lock = Join-Path $BundleStage "requirements.lock"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Py -m piptools compile `
        --generate-hashes `
        --resolver=backtracking `
        --output-file $lock `
        (Join-Path $Project "pyproject.toml") | Out-Host
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -ne 0) { throw "pip-compile failed" }
    return $lock
}

function Append-WheelHash($Lockfile, $WheelPath) {
    # pip's --require-hashes mode requires every install (including the matofx
    # wheel itself) to come from a hashed pin. Append the wheel as a file: URL
    # entry so a single `pip install -r requirements.lock` from the bundle
    # directory installs everything.
    Write-Host "==> Appending wheel hash to lockfile"
    $wheelName = Split-Path $WheelPath -Leaf
    $hash = (Get-FileHash -Algorithm SHA256 $WheelPath).Hash.ToLower()
    $entry = @"

# --- MATOFX wheel (appended by build_bundle) ---
matofx @ file:./$wheelName ``
    --hash=sha256:$hash
"@
    # Replace the line-continuation marker we used in the here-string (PowerShell
    # backtick) with the literal backslash pip expects in requirements files.
    $entry = $entry.Replace("``", "\")
    Add-Content -Path $Lockfile -Value $entry -Encoding ascii
}

function Generate-Sbom($Py, $Lockfile) {
    Write-Host "==> Generating CycloneDX SBOM"
    $sbom = Join-Path $BundleStage "sbom.json"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Py -m cyclonedx_py requirements --of JSON --output-file $sbom $Lockfile | Out-Host
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -ne 0) { throw "cyclonedx-py failed" }
    return $sbom
}

function Stage-Assets($Wheel) {
    Write-Host "==> Staging bundle assets"
    Copy-Item $Wheel (Join-Path $BundleStage (Split-Path $Wheel -Leaf))
    Copy-Item (Join-Path $Project "etc\matofx.example.toml") (Join-Path $BundleStage "matofx.toml")
    Copy-Item (Join-Path $Project "example_tools") (Join-Path $BundleStage "example_tools") -Recurse
    # Optional default-branding pack. seed_default_branding.py looks for
    # `branding/header_icon.*` / `branding/favicon.*`; missing files are
    # skipped at install time, so an empty (or absent) source dir is fine.
    $brandingSrc = Join-Path $Project "etc\branding"
    if (Test-Path $brandingSrc) {
        Copy-Item $brandingSrc (Join-Path $BundleStage "branding") -Recurse
    }
    New-Item -ItemType Directory -Force (Join-Path $BundleStage "scripts") | Out-Null
    foreach ($name in @(
        "matofx_migrate.py",
        "seed_example_tools.py",
        "seed_default_branding.py",
        "install_windows.ps1",
        "install_linux.sh",
        "matofx-start.ps1",
        "matofx-stop.ps1",
        "matofx-start.bat",
        "matofx-stop.bat",
        "matofx-start.sh",
        "matofx-stop.sh"
    )) {
        Copy-Item (Join-Path $PSScriptRoot $name) (Join-Path $BundleStage "scripts\$name")
    }
    Copy-Item (Join-Path $Root "INSTALL.md") (Join-Path $BundleStage "INSTALL.md")
    Copy-Item (Join-Path $Root "HARDWARE_SIZING.md") (Join-Path $BundleStage "HARDWARE_SIZING.md")
}

function Write-BundleInfo {
    $gitSha = try { (& git -C $Project rev-parse HEAD 2>$null).Trim() } catch { "n/a" }
    $info = @"
matofx bundle
version : $Version
git_sha : $gitSha
built   : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz")
host    : $env:COMPUTERNAME
os      : $($PSVersionTable.OS)
python  : $(& $py --version 2>&1)
"@
    # UTF-8 without BOM (Set-Content -Encoding UTF8 on PS 5.1 prepends a BOM).
    [System.IO.File]::WriteAllText((Join-Path $BundleStage "BUNDLE_INFO.txt"), $info, (New-Object System.Text.UTF8Encoding $false))
}

$py = Find-Python
$Version = Read-Version
$bundleName = "matofx-bundle-$Version"
$BundleStage = Join-Path $DistRoot $bundleName

Write-Host "==> Bundle: $BundleStage"
if (Test-Path $BundleStage) {
    Write-Host "==> Removing previous stage"
    Remove-Item $BundleStage -Recurse -Force
}
New-Item -ItemType Directory -Force $BundleStage | Out-Null
New-Item -ItemType Directory -Force $DistRoot    | Out-Null

Ensure-Tools $py
$wheel = Build-Wheel $py
Verify-WheelContents $wheel
Stage-Assets $wheel
$lock = Compile-Lockfile $py
$wheelInBundle = Join-Path $BundleStage (Split-Path $wheel -Leaf)
Append-WheelHash $lock $wheelInBundle
Generate-Sbom $py $lock | Out-Null
Write-BundleInfo

if ($Zip) {
    $zipPath = "$BundleStage.zip"
    Write-Host "==> Compressing to $zipPath"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path "$BundleStage\*" -DestinationPath $zipPath
    Write-Host "==> Archive: $zipPath"
}

$size = (Get-ChildItem $BundleStage -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ""
Write-Host "Bundle ready:"
Write-Host "  Path:   $BundleStage"
Write-Host "  Size:   $([math]::Round($size / 1MB, 2)) MB"
Write-Host "  Wheel:  $(Split-Path $wheel -Leaf)"
Write-Host "  Files:  $((Get-ChildItem $BundleStage -Recurse -File).Count)"
