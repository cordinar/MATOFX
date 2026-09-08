#!/usr/bin/env bash
# Start matofx-api and matofx-runner as background processes.
#
# Self-locates from the script's own directory — drop this script into your
# APP_HOME (the directory that contains `venv/` and `matofx.toml`) and run.
# Logs land under <APP_HOME>/logs/ and PIDs under <APP_HOME>/run/ so
# matofx-stop.sh can find them.
#
# This is the "quick-start for a dev box" launcher. For production use
# systemd units instead — see INSTALL.md §B14.

set -euo pipefail

APP_HOME="$(cd "$(dirname "$0")" && pwd)"
VENV_BIN="$APP_HOME/venv/bin"
CONFIG_TOML="$APP_HOME/matofx.toml"
LOG_DIR="$APP_HOME/logs"
RUN_DIR="$APP_HOME/run"

[[ -x "$VENV_BIN/matofx-api"    ]] || { echo "matofx-api not found at $VENV_BIN" >&2; exit 1; }
[[ -x "$VENV_BIN/matofx-runner" ]] || { echo "matofx-runner not found at $VENV_BIN" >&2; exit 1; }
[[ -f "$CONFIG_TOML"            ]] || { echo "Config not found at $CONFIG_TOML" >&2; exit 1; }

mkdir -p "$LOG_DIR" "$RUN_DIR"

start_one() {
    local name="$1"
    local exe="$2"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "matofx-$name already running (PID $(cat "$pidfile")) — skipping"
        return
    fi
    MATOFX_CONFIG="$CONFIG_TOML" nohup "$exe" \
        >> "$LOG_DIR/$name.log" 2>&1 &
    echo $! > "$pidfile"
    echo "Started matofx-$name (PID $(cat "$pidfile"))  log: $LOG_DIR/$name.log"
}

start_one api    "$VENV_BIN/matofx-api"
sleep 1
start_one runner "$VENV_BIN/matofx-runner"

echo
echo "MATOFX is up."
echo "  Login:  http://127.0.0.1:8000/ui/login"
echo "  Logs:   tail -f $LOG_DIR/api.log $LOG_DIR/runner.log"
echo "  Stop:   $APP_HOME/matofx-stop.sh"
