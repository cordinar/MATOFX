#!/usr/bin/env bash
# Stop matofx-api and matofx-runner that were started by matofx-start.sh.

set -euo pipefail

APP_HOME="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$APP_HOME/run"

stop_one() {
    local name="$1"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ ! -f "$pidfile" ]]; then
        echo "matofx-$name has no PID file — not running (or not started by matofx-start.sh)"
        return
    fi
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping matofx-$name (PID $pid)"
        kill "$pid"
        local n=0
        while kill -0 "$pid" 2>/dev/null && (( n < 20 )); do
            sleep 0.5; ((n++))
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "  did not stop after 10s; sending SIGKILL"
            kill -9 "$pid" || true
        fi
    else
        echo "matofx-$name PID $pid is not running — cleaning stale pidfile"
    fi
    rm -f "$pidfile"
}

stop_one api
stop_one runner
echo "Done."
