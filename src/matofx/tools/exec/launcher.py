"""The per-job launcher script and its rendering helper.

The launcher is the same on every execution target — local
subprocess, AOS over SSH/WinRM, HPC compute node — and is what
makes the heartbeat protocol uniform across them. MATOFX renders a
copy into each job's working directory at submit time; the target
backend just *invokes* it.

What the launcher does (in order):

1. Writes ``heartbeat/running.json`` containing
   ``{state: "running", started_at, pid, host}``.
2. Spawns ``<interpreter_path> <main_script_relative>`` with the
   working directory as cwd, redirecting stdout to
   ``logs/stdout.log`` and stderr to ``logs/stderr.log``.
3. Forwards SIGTERM (POSIX) / CTRL_BREAK_EVENT (Windows) from
   itself to the child, waits up to ``cancel_grace_seconds``,
   then escalates to SIGKILL / TerminateProcess.
4. On exit, atomically writes either ``heartbeat/complete.json``
   (exit code 0) or ``heartbeat/failed.json`` (anything else),
   carrying the exit code, end timestamp, and signal name when
   the child was killed.

The script is intentionally dependency-free — it only imports
from the standard library — so it can run under any interpreter
the operator points :class:`PythonEnv` at. It also doesn't import
anything from ``matofx.*`` so the working-dir copy is self-contained
and survives a MATOFX restart mid-job.
"""

from __future__ import annotations

from dataclasses import dataclass

LAUNCHER_FILENAME: str = "launcher.py"
"""Filename the rendered launcher is written to inside each working dir."""


@dataclass(frozen=True)
class LauncherInputs:
    """Resolved values that get baked into the launcher source.

    Kept separate from :func:`render_launcher_script` so the
    staging layer can build it once and tests can introspect it
    without re-parsing the rendered string.
    """

    interpreter_path: str
    main_script_relative: str
    cancel_grace_seconds: int


def render_launcher_script(
    *,
    interpreter_path: str,
    main_script_relative: str,
    cancel_grace_seconds: int,
) -> str:
    """Render the launcher script as a UTF-8 string ready for ``Path.write_text``.

    The returned string is fully self-contained — no f-string
    placeholders left over, no import of ``matofx`` at the top.
    All configuration is baked in at render time so the script
    behaves the same after a MATOFX restart, after a Python
    upgrade on the host, or in a sandbox without network access.

    Args:
        interpreter_path: Absolute path to the Python interpreter
            to invoke. Quoted into the script literally; backslashes
            on Windows paths are handled by ``repr``.
        main_script_relative: Path to the tool's main script
            relative to the working directory (where the launcher
            runs).
        cancel_grace_seconds: Wait this long between SIGTERM and
            SIGKILL when the launcher is asked to cancel.

    Returns:
        The rendered launcher source code as a string.
    """
    if cancel_grace_seconds < 0:
        raise ValueError("cancel_grace_seconds must be >= 0")
    return _LAUNCHER_TEMPLATE.format(
        interpreter_path_repr=repr(interpreter_path),
        main_script_relative_repr=repr(main_script_relative),
        cancel_grace_seconds=cancel_grace_seconds,
    )


# The launcher source. Single-quoted f-string format with three
# escaped placeholders so the rest of the script's braces stay
# literal. The script is tested end-to-end in tests/tools/test_exec_launcher.py.
_LAUNCHER_TEMPLATE: str = '''"""MATOFX per-job launcher (auto-generated; do not hand-edit).

Spawned by the runner / AOS-SSH / HPC scheduler. Drives the tool
through the heartbeat protocol the polling loop reads back.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

INTERPRETER_PATH = {interpreter_path_repr}
MAIN_SCRIPT_RELATIVE = {main_script_relative_repr}
CANCEL_GRACE_SECONDS = {cancel_grace_seconds}

WORKING_DIR = Path(__file__).resolve().parent
HEARTBEAT_DIR = WORKING_DIR / "heartbeat"
LOGS_DIR = WORKING_DIR / "logs"

HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _utcnow_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _atomic_write(target: Path, payload: dict) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _kill_child(proc: subprocess.Popen) -> None:
    """SIGTERM, wait CANCEL_GRACE_SECONDS, then SIGKILL."""
    if proc.poll() is not None:
        return
    if platform.system() == "Windows":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            proc.terminate()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, AttributeError):
            proc.terminate()
    try:
        proc.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if platform.system() == "Windows":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, AttributeError):
                proc.kill()


def main() -> int:
    started_at = _utcnow_iso()
    proc: subprocess.Popen | None = None
    cancel_requested = threading.Event()

    def _on_term(_signum, _frame) -> None:
        cancel_requested.set()
        if proc is not None:
            _kill_child(proc)

    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    else:
        try:
            signal.signal(signal.SIGBREAK, _on_term)
        except (AttributeError, ValueError):
            pass

    pid = os.getpid()
    _atomic_write(
        HEARTBEAT_DIR / "running.json",
        {{
            "state": "running",
            "started_at": started_at,
            "pid": pid,
            "host": _hostname(),
        }},
    )

    stdout_log = LOGS_DIR / "stdout.log"
    stderr_log = LOGS_DIR / "stderr.log"

    creationflags = 0
    preexec_fn = None
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        preexec_fn = os.setsid

    try:
        with open(stdout_log, "wb") as out, open(stderr_log, "wb") as err:
            proc = subprocess.Popen(
                # -B: never write .pyc, so importing sibling modules can't
                # leave a __pycache__ in the ephemeral working dir for the
                # post-run cleanup to trip over (Windows/OneDrive file locks).
                #
                # -u: unbuffered stdout/stderr. Without it Python
                # block-buffers when its output is a file rather than a
                # terminal — which this always is — so a tool's progress
                # sits in a 8 KB buffer until the process exits and
                # `stdout.log` stays EMPTY for the whole run. Live log
                # streaming then shows nothing on exactly the long jobs
                # people most want to watch, and the operator cannot tell
                # a working solve from a wedged one. Found on ENV-A
                # 2026-08-13 (D-13): a 90-second job wrote its log at the
                # 90-second mark. The cost of -u is a write syscall per
                # line, against a file on the shared mount.
                [INTERPRETER_PATH, "-B", "-u", MAIN_SCRIPT_RELATIVE],
                cwd=str(WORKING_DIR),
                stdout=out,
                stderr=err,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
            exit_code = proc.wait()
    except FileNotFoundError as e:
        _atomic_write(
            HEARTBEAT_DIR / "failed.json",
            {{
                "state": "failed",
                "started_at": started_at,
                "ended_at": _utcnow_iso(),
                "pid": pid,
                "host": _hostname(),
                "exit_code": None,
                "signal": None,
                "failure_reason": (
                    "Could not start the tool: " + str(e) + ". "
                    "Check that the Python environment and main script paths are correct."
                ),
            }},
        )
        return 127
    except Exception as e:
        _atomic_write(
            HEARTBEAT_DIR / "failed.json",
            {{
                "state": "failed",
                "started_at": started_at,
                "ended_at": _utcnow_iso(),
                "pid": pid,
                "host": _hostname(),
                "exit_code": None,
                "signal": None,
                "failure_reason": "Unexpected launcher error: " + str(e),
            }},
        )
        return 1

    ended_at = _utcnow_iso()
    if exit_code == 0 and not cancel_requested.is_set():
        _atomic_write(
            HEARTBEAT_DIR / "complete.json",
            {{
                "state": "complete",
                "started_at": started_at,
                "ended_at": ended_at,
                "pid": pid,
                "host": _hostname(),
                "exit_code": 0,
                "signal": None,
            }},
        )
    else:
        signal_name = None
        if cancel_requested.is_set():
            signal_name = "SIGTERM"
        _atomic_write(
            HEARTBEAT_DIR / "failed.json",
            {{
                "state": "failed",
                "started_at": started_at,
                "ended_at": ended_at,
                "pid": pid,
                "host": _hostname(),
                "exit_code": exit_code,
                "signal": signal_name,
                "failure_reason": (
                    "Cancelled by request." if cancel_requested.is_set()
                    else "Tool exited with non-zero status " + str(exit_code) + "."
                ),
            }},
        )
    return exit_code


if __name__ == "__main__":
    # The exit code lets the surrounding shell / scheduler still see
    # success / failure for its own bookkeeping; the heartbeat file
    # is the source of truth MATOFX consults.
    sys.exit(main())

# Touch unused import so ruff doesn't trip when this file is linted
# in-place (shouldn't be — it's auto-generated — but just in case).
_ = time
'''
