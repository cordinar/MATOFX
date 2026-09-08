#!/usr/bin/env bash
# One-shot Linux installer for a MATOFX bundle.
# Mirrors INSTALL.md Part B steps B3..B12.
#
# Usage (from the extracted bundle dir):
#   ./scripts/install_linux.sh
#   APP_HOME=/srv/matofx ./scripts/install_linux.sh
#   USE_EXAMPLE_TOOLS=0 ./scripts/install_linux.sh

set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
APP_HOME="${APP_HOME:-/opt/matofx}"
CONFIG_DIR="${CONFIG_DIR:-/etc/matofx}"
JOBS_DIR="${JOBS_DIR:-/srv/matofx/jobs}"
USE_EXAMPLE_TOOLS="${USE_EXAMPLE_TOOLS:-1}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_FIRST_NAME="${ADMIN_FIRST_NAME:-Site}"
ADMIN_LAST_NAME="${ADMIN_LAST_NAME:-Administrator}"

echo "==> MATOFX bundle: $BUNDLE"
echo "==> Install root:  $APP_HOME"

# B3
sudo mkdir -p "$APP_HOME" "$CONFIG_DIR" /var/lib/matofx /var/log/matofx "$JOBS_DIR"
sudo chown -R "$USER:$USER" "$APP_HOME" /var/lib/matofx /var/log/matofx "$JOBS_DIR"

# B4
if [[ ! -d "$APP_HOME/venv" ]]; then
    echo "==> Creating virtualenv"
    python3 -m venv "$APP_HOME/venv"
fi
PY="$APP_HOME/venv/bin/python"
PIP="$APP_HOME/venv/bin/pip"
"$PIP" install --upgrade pip >/dev/null

# B5
"$PIP" config set global.require-hashes true >/dev/null

# B6 — the lockfile contains both deps and the matofx wheel itself (file: entry
# with --hash). Run pip install from inside the bundle so the relative path
# resolves. One command installs everything under --require-hashes.
LOCK="$BUNDLE/requirements.lock"
WHEELHOUSE="$BUNDLE/wheelhouse"
(
    cd "$BUNDLE"
    if [[ -d "$WHEELHOUSE" ]]; then
        echo "==> Installing offline from wheelhouse"
        "$PIP" install --no-index --find-links "$WHEELHOUSE" --requirement "$LOCK"
    else
        echo "==> Installing online (PyPI)"
        "$PIP" install --requirement "$LOCK"
    fi
)

# B7
CONFIG_DST="$CONFIG_DIR/matofx.toml"
if [[ ! -f "$CONFIG_DST" ]]; then
    sudo cp "$BUNDLE/matofx.toml" "$CONFIG_DST"
    sudo chmod 640 "$CONFIG_DST"
    echo "==> Wrote default config at $CONFIG_DST"
    # Patch defaults for SQLite + staging so the install runs out of the box.
    # Operator flips to Postgres + prod + require_signing=true when ready.
    sudo sed -i \
        -e "s|^dialect *= *\"postgres\"|dialect = \"sqlite\"|" \
        -e "s|^url *= *\"postgres://[^\"]*\"|url = \"sqlite:////var/lib/matofx/matofx.db\"|" \
        -e "s|^environment *= *\"prod\"|environment = \"staging\"|" \
        -e "s|^require_signing *= *true|require_signing = false|" \
        "$CONFIG_DST"
    echo "==> Patched config with SQLite + staging defaults"
else
    echo "==> Keeping existing config at $CONFIG_DST"
fi

# B8
sudo tee /etc/profile.d/matofx.sh >/dev/null <<EOF
export MATOFX_CONFIG=$CONFIG_DST
EOF
export MATOFX_CONFIG="$CONFIG_DST"

# B9
KEYPATH="$CONFIG_DIR/audit-signing.key"
if [[ ! -f "$KEYPATH" ]]; then
    echo "==> Generating audit signing key at $KEYPATH"
    sudo "$PY" -c "from pathlib import Path; from matofx.audit import generate_signing_key; generate_signing_key(Path('$KEYPATH'))"
    sudo chmod 600 "$KEYPATH"
else
    echo "==> Keeping existing audit key at $KEYPATH"
fi

# B10
echo "==> Applying database migrations"
"$PY" "$BUNDLE/scripts/matofx_migrate.py" upgrade head

# B11
echo "==> Creating first super-user '$ADMIN_USERNAME'"
read -rsp "Password for $ADMIN_USERNAME: " ADMIN_PASS; echo
"$APP_HOME/venv/bin/matofx" admin create-first-user \
    --username   "$ADMIN_USERNAME" \
    --email      "$ADMIN_EMAIL" \
    --first-name "$ADMIN_FIRST_NAME" \
    --last-name  "$ADMIN_LAST_NAME" \
    --password   "$ADMIN_PASS"
unset ADMIN_PASS

# Default branding (header icon + favicon). Idempotent: only writes a DB row
# when the matching general_setting is still null, so an admin's later upload
# is preserved across re-runs / upgrades. No-op if the bundle ships without
# a branding/ directory.
if [[ -d "$BUNDLE/branding" ]]; then
    echo "==> Seeding default branding"
    "$PY" "$BUNDLE/scripts/seed_default_branding.py"
fi

# B12
if [[ "$USE_EXAMPLE_TOOLS" -eq 1 ]]; then
    echo "==> Seeding example tools"
    "$PY" "$BUNDLE/scripts/seed_example_tools.py"
fi

# Install launcher shims into APP_HOME so the operator can run a single
# command to start/stop both services.
echo "==> Installing launcher scripts into $APP_HOME"
cp "$BUNDLE/scripts/matofx-start.sh" "$APP_HOME/matofx-start.sh"
cp "$BUNDLE/scripts/matofx-stop.sh"  "$APP_HOME/matofx-stop.sh"
chmod +x "$APP_HOME/matofx-start.sh" "$APP_HOME/matofx-stop.sh"

echo
echo "MATOFX installed."
echo "  Quick start:  $APP_HOME/matofx-start.sh"
echo "  Stop:         $APP_HOME/matofx-stop.sh"
echo "  Manual API:   MATOFX_CONFIG=$CONFIG_DST $APP_HOME/venv/bin/matofx-api"
echo "  Manual runner:MATOFX_CONFIG=$CONFIG_DST $APP_HOME/venv/bin/matofx-runner"
echo "  Login:        http://127.0.0.1:8000/ui/login"
