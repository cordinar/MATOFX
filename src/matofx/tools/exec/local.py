"""Local-host :class:`ExecutionTarget` implementation.

LocalTarget runs the launcher script directly on the MATOFX
server. The runner daemon's local supervisor enforces the
configured concurrency cap (default 2) before any LocalTarget
submission lands.

Why we don't keep an :class:`asyncio.subprocess.Process` handle
around: the launcher is the source of truth for state. Once it
starts, the heartbeat files describe the job's progress; the
runner can crash and restart without losing its read of an
in-flight job. Submission therefore detaches the child (new
process group on POSIX, new process group on Windows) so the
runner doesn't have to babysit the file descriptor.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from matofx.tools.exec.base import (
    ExecutionTargetKind,
    JobObservation,
    LogsRef,
    ResourceRequest,
    SubmitResult,
    TargetContext,
)
from matofx.tools.exec.contracts import HeartbeatRecord, HeartbeatStateLiteral
from matofx.tools.exec.staging import StagedWorkingDir

# Sentinel for protocol-parity-only kwargs. LocalTarget never reads
# from the TargetContext but accepting one keeps the signature
# uniform across LocalTarget / SshAosTarget / future HpcTarget.
_EMPTY_CONTEXT: TargetContext = TargetContext()


class LocalTarget:
    """Spawn the launcher locally and read its heartbeat back from the same disk.

    Implements :class:`matofx.tools.exec.base.ExecutionTarget`. The
    target is stateless — the runner constructs one and calls into
    it; no per-job state lives on the instance, which keeps the
    runner restart-safe.
    """

    target_kind: ExecutionTargetKind = "local"

    def __init__(self, *, python_executable: str = sys.executable) -> None:
        """Construct a LocalTarget.

        Args:
            python_executable: Interpreter the launcher is invoked
                under. Defaults to the runner's own
                ``sys.executable``, which keeps the launcher
                dependency-free regardless of the tool's chosen
                :class:`PythonEnv` (the launcher in turn invokes
                the tool's interpreter).
        """
        self._python_executable = python_executable

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> SubmitResult:
        """Spawn the launcher, detached, and record its PID.

        ``resource_request`` is accepted for protocol parity but
        unused on the local target — concurrency control happens
        in the runner before we ever get here, and OS-level
        resource limits aren't applied in Phase 1. ``context`` is
        accepted for protocol parity; LocalTarget never reads from it.

        Diagnostic capture: the launcher's own stdout/stderr is
        redirected to ``logs/launcher.log`` rather than ``/dev/null``.
        Without this, a launcher startup failure (interpreter path
        wrong, import error in the auto-generated launcher source,
        etc.) is silent — the per-tool ``stdout.log`` / ``stderr.log``
        files never get opened because the launcher dies before line
        ~208 of the launcher source, and the operator sees an empty
        ``logs/`` folder with no clue why. Capturing launcher chatter
        to its own file makes that failure mode debuggable.
        """
        del resource_request, context  # unused for local; AOS / HPC honour these

        # Make sure the directory exists even though staging already
        # created it — defensive, since a missing logs dir would crash
        # the open() below before the subprocess is even spawned and
        # surface as a generic "Failed to start the job" from the
        # supervisor's catch-all rather than something the operator
        # can act on.
        staged.logs_dir.mkdir(parents=True, exist_ok=True)
        launcher_log_path = staged.logs_dir / "launcher.log"

        # Open the file once; pass the same FD as both stdout AND
        # stderr so the launcher's writes interleave in a single
        # readable file. ``buffering=0`` (binary unbuffered) keeps the
        # output flushed as the launcher writes, which matters when
        # the launcher crashes mid-execution and we want the partial
        # output on disk to diagnose. We close our own handle after the
        # spawn — the child has its own copy of the underlying OS file
        # descriptor and goes on writing to it.
        launcher_log_handle = launcher_log_path.open("ab", buffering=0)
        try:
            if platform.system() == "Windows":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                proc = await asyncio.create_subprocess_exec(
                    self._python_executable,
                    str(staged.launcher_path),
                    cwd=str(staged.working_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=launcher_log_handle,
                    stderr=launcher_log_handle,
                    creationflags=creationflags,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    self._python_executable,
                    str(staged.launcher_path),
                    cwd=str(staged.working_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=launcher_log_handle,
                    stderr=launcher_log_handle,
                    start_new_session=True,
                )
        finally:
            launcher_log_handle.close()
        return SubmitResult(pid=proc.pid)

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> JobObservation:
        """Read the heartbeat directory and translate the latest file to a snapshot.

        Read order is ``failed → complete → running``. The launcher
        writes terminal files atomically (``rename`` over the
        target path), so the first one seen is authoritative even
        if the polling loop lands during a swap.
        """
        del context  # unused for local
        return await asyncio.to_thread(_read_heartbeat, staged.heartbeat_dir)

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> None:
        """Send SIGTERM to the process group, wait, then SIGKILL.

        Falls back to a single-process kill if process-group
        introspection fails (e.g. the launcher already exited and
        the PID belongs to a recycled process — check with
        :func:`os.kill(pid, 0)` style probe before killing).
        """
        # heartbeat-file path / dispatch meta unused; cancel acts on the PID.
        del staged, context
        pid = submit_result.pid
        if pid is None:
            return
        await asyncio.to_thread(_kill_pid_with_grace, pid, grace_seconds)

    async def fetch_logs(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> LogsRef:
        """Return paths to the launcher-captured logs (no copy needed)."""
        del context
        return LogsRef(
            stdout_path=staged.logs_dir / "stdout.log",
            stderr_path=staged.logs_dir / "stderr.log",
        )


_HEARTBEAT_FILES: tuple[tuple[str, HeartbeatStateLiteral], ...] = (
    ("failed.json", "failed"),
    ("complete.json", "complete"),
    ("running.json", "running"),
)


def _read_heartbeat(heartbeat_dir: Path) -> JobObservation:
    for filename, state in _HEARTBEAT_FILES:
        path = heartbeat_dir / filename
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Mid-write torn read: behave as if the heartbeat hasn't
            # appeared yet; the next poll will see the renamed file.
            continue
        try:
            record = HeartbeatRecord.model_validate(payload)
        except ValueError:
            continue
        return JobObservation(
            heartbeat_state=state,
            started_at=_parse_iso(record.started_at),
            ended_at=_parse_iso(record.ended_at),
            exit_code=record.exit_code,
            signal_name=record.signal,
            failure_reason=record.failure_reason,
        )
    return JobObservation(heartbeat_state=None)


def _parse_iso(value: str | None) -> _dt.datetime | None:
    if value is None:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _kill_pid_with_grace(pid: int, grace_seconds: int) -> None:
    """SIGTERM the process group, wait grace_seconds, then SIGKILL.

    Survives the case where the process has already exited (PID
    not found is silently ignored — there's nothing to do).
    """
    if platform.system() == "Windows":
        _windows_kill(pid, grace_seconds)
        return
    _posix_kill(pid, grace_seconds)


def _posix_kill(pid: int, grace_seconds: int) -> None:
    # ``os.getpgid`` / ``killpg`` / ``signal.SIGKILL`` are POSIX-only.
    # ``getattr`` keeps this branch importable on Windows where the
    # branch is never taken at runtime but mypy still type-checks it.
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if getpgid is None or killpg is None or sigkill is None:
        return
    try:
        pgid = getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = _dt.datetime.now() + _dt.timedelta(seconds=grace_seconds)
    while _dt.datetime.now() < deadline:
        if not _pid_alive(pid):
            return
        _sleep(0.1)
    try:
        killpg(pgid, sigkill)
    except (ProcessLookupError, PermissionError):
        return


def _windows_kill(pid: int, grace_seconds: int) -> None:
    # ``taskkill /T`` walks the process tree the launcher spawned
    # via CREATE_NEW_PROCESS_GROUP, which is why the launcher
    # bothers with the flag. Without /T we'd leave the tool's
    # child running.
    taskkill = _resolve_windows_tool("taskkill")
    if taskkill is None:
        return
    try:
        subprocess.run(  # noqa: S603
            [taskkill, "/PID", str(pid), "/T"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return
    deadline = _dt.datetime.now() + _dt.timedelta(seconds=grace_seconds)
    while _dt.datetime.now() < deadline:
        if not _pid_alive(pid):
            return
        _sleep(0.1)
    try:
        subprocess.run(  # noqa: S603
            [taskkill, "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return


def _pid_alive(pid: int) -> bool:
    if platform.system() == "Windows":
        tasklist = _resolve_windows_tool("tasklist")
        if tasklist is None:
            return False
        result = subprocess.run(  # noqa: S603
            [tasklist, "/FI", f"PID eq {pid}", "/NH"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return str(pid) in result.stdout.decode(errors="replace")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _resolve_windows_tool(name: str) -> str | None:
    r"""Resolve a built-in Windows tool to an absolute path.

    Looks under ``%SYSTEMROOT%\System32`` first (where Windows
    keeps the canonical copies), falling back to ``shutil.which``.
    Returns ``None`` only if neither resolves — in which case
    cancellation can't proceed and the caller bails out cleanly.
    """
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _sleep(seconds: float) -> None:
    """Stand-in for ``time.sleep`` so tests can monkeypatch a single seam."""
    import time  # noqa: PLC0415 — local import to keep the seam tight

    time.sleep(seconds)
