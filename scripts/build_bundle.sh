#!/usr/bin/env bash
# Build a deployable MATOFX bundle.
#
# Run from the repository root:
#   ./scripts/build_bundle.sh         # bundle as directory
#   ./scripts/build_bundle.sh --zip   # also produce a .zip archive

set -euo pipefail

ZIP=0
PYTHON_EXE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip)        ZIP=1; shift ;;
        --python)     PYTHON_EXE="$2"; shift 2 ;;
        *)            echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

# The repo root is scripts/'s parent, and the Python project now lives at
# that root (it used to sit one level down, in matofx/).
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$ROOT_DIR"
DIST_ROOT="$ROOT_DIR/dist"
SCRIPTS_DIR="$ROOT_DIR/scripts"

find_python() {
    if [[ -n "$PYTHON_EXE" && -x "$PYTHON_EXE" ]]; then echo "$PYTHON_EXE"; return; fi
    if [[ -x "$PROJECT/.venv/bin/python" ]]; then echo "$PROJECT/.venv/bin/python"; return; fi
    if command -v python3 >/dev/null; then echo "$(command -v python3)"; return; fi
    echo "Cannot find Python. Pass --python <path> or create $PROJECT/.venv first." >&2
    exit 1
}

PY="$(find_python)"

VERSION="$(grep -E '^version\s*=' "$PROJECT/pyproject.toml" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')"
[[ -z "$VERSION" ]] && { echo "Cannot read version" >&2; exit 1; }

BUNDLE="$DIST_ROOT/matofx-bundle-$VERSION"

echo "==> Bundle: $BUNDLE"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/scripts"

echo "==> Checking build tooling"
"$PY" -c "import build, piptools, cyclonedx_py" 2>/dev/null || {
    echo "Install dev/supply-chain extras first: $PY -m pip install -e \"$PROJECT[dev,supply-chain]\"" >&2
    exit 1
}

echo "==> Building wheel"
(cd "$PROJECT" && "$PY" -m build --wheel)
WHEEL="$(ls -t "$PROJECT"/dist/matofx-*.whl | head -n1)"

# Hatchling applies .gitignore patterns to wheel file selection, so an
# over-broad ignore (e.g. "secrets/") silently drops a whole source package
# — it shipped once as "No module named matofx.secrets" on a deployed
# install. Fail the build if any .py under src/matofx is missing.
echo "==> Verifying wheel contains every source module"
"$PY" - "$WHEEL" "$PROJECT/src/matofx" <<'PYEOF'
import sys, zipfile
from pathlib import Path

wheel, src = sys.argv[1], Path(sys.argv[2])
in_wheel = set(zipfile.ZipFile(wheel).namelist())
missing = [
    "matofx/" + p.relative_to(src).as_posix()
    for p in src.rglob("*.py")
    if "__pycache__" not in p.parts
    and "matofx/" + p.relative_to(src).as_posix() not in in_wheel
]
if missing:
    for m in missing:
        print(f"  MISSING: {m}", file=sys.stderr)
    sys.exit(f"Wheel is missing {len(missing)} source file(s) — check .gitignore patterns (hatchling honours them).")
PYEOF

echo "==> Staging assets"
cp "$WHEEL"                              "$BUNDLE/"
cp "$PROJECT/etc/matofx.example.toml"    "$BUNDLE/matofx.toml"
cp -R "$PROJECT/example_tools"           "$BUNDLE/example_tools"
# Optional default-branding pack. seed_default_branding.py looks for
# `branding/header_icon.*` / `branding/favicon.*`; missing files are
# skipped at install time, so an empty (or absent) source dir is fine.
if [[ -d "$PROJECT/etc/branding" ]]; then
    cp -R "$PROJECT/etc/branding" "$BUNDLE/branding"
fi
for name in \
    matofx_migrate.py \
    seed_example_tools.py \
    seed_default_branding.py \
    install_windows.ps1 \
    install_linux.sh \
    matofx-start.ps1 \
    matofx-stop.ps1 \
    matofx-start.bat \
    matofx-stop.bat \
    matofx-start.sh \
    matofx-stop.sh
do
    cp "$SCRIPTS_DIR/$name" "$BUNDLE/scripts/$name"
done
cp "$ROOT_DIR/INSTALL.md" "$BUNDLE/INSTALL.md"
cp "$ROOT_DIR/HARDWARE_SIZING.md" "$BUNDLE/HARDWARE_SIZING.md"
chmod +x "$BUNDLE/scripts/install_linux.sh" \
         "$BUNDLE/scripts/matofx-start.sh"  \
         "$BUNDLE/scripts/matofx-stop.sh"

echo "==> Generating hashed lockfile"
"$PY" -m piptools compile \
    --generate-hashes \
    --resolver=backtracking \
    --output-file "$BUNDLE/requirements.lock" \
    "$PROJECT/pyproject.toml"

# pip's --require-hashes mode requires every install (including the matofx
# wheel) to come from a hashed pin. Append the wheel as a file: URL so a
# single `pip install -r requirements.lock` from the bundle dir installs
# everything.
echo "==> Appending wheel hash to lockfile"
WHEEL_NAME="$(basename "$WHEEL")"
WHEEL_HASH="$("$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$BUNDLE/$WHEEL_NAME")"
cat >> "$BUNDLE/requirements.lock" <<EOF

# --- MATOFX wheel (appended by build_bundle) ---
matofx @ file:./${WHEEL_NAME} \\
    --hash=sha256:${WHEEL_HASH}
EOF

echo "==> Generating CycloneDX SBOM"
"$PY" -m cyclonedx_py requirements --of JSON \
    --output-file "$BUNDLE/sbom.json" \
    "$BUNDLE/requirements.lock"

GIT_SHA="$(git -C "$PROJECT" rev-parse HEAD 2>/dev/null || echo n/a)"
cat > "$BUNDLE/BUNDLE_INFO.txt" <<EOF
matofx bundle
version : $VERSION
git_sha : $GIT_SHA
built   : $(date '+%Y-%m-%d %H:%M:%S %z')
host    : $(hostname)
os      : $(uname -srm)
python  : $($PY --version 2>&1)
EOF

if [[ "$ZIP" -eq 1 ]]; then
    ZIPFILE="$DIST_ROOT/matofx-bundle-$VERSION.zip"
    echo "==> Compressing to $ZIPFILE"
    rm -f "$ZIPFILE"
    (cd "$DIST_ROOT" && zip -rq "$(basename "$ZIPFILE")" "matofx-bundle-$VERSION")
fi

SIZE_MB="$(du -sm "$BUNDLE" | cut -f1)"
echo
echo "Bundle ready:"
echo "  Path:   $BUNDLE"
echo "  Size:   ${SIZE_MB} MB"
echo "  Wheel:  $(basename "$WHEEL")"
echo "  Files:  $(find "$BUNDLE" -type f | wc -l)"
