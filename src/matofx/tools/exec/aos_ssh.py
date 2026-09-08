"""SSH-backed Always-On-Server execution target.

:class:`SshAosTarget` runs jobs on a remote Linux host over SSH.
The shared mount (a single uniform path mounted on the runner host
*and* every AOS host — see ``storage_location`` and the project
brief's deployment topology) is the source of truth for the
working dir, heartbeat files, and logs. This target uses SSH only
for the two operations the shared mount can't do:

- **submit** — start ``launcher.py`` on the remote host, detached,
  and capture its PID.
- **cancel** — signal the remote process tree (SIGTERM, then
  SIGKILL after the grace period).

**poll** and **fetch_logs** read the same shared filesystem the
launcher writes to, so they don't need SSH at all — they reuse the
local read paths from :class:`matofx.tools.exec.local.LocalTarget`.

Security posture (per the project brief's AOS notes):

- Host keys are *pinned*: every :class:`AosHost` row points at a
  ``known_hosts`` file with the expected public key. Unknown hosts
  raise rather than auto-add.
- Credentials live in the secrets store, never the database. The
  ``aos_host.secret_ref`` column is an opaque key into the store.
- The launcher invocation is a single fixed shell pipeline. In
  production the AOS-side ``authorized_keys`` should pin a
  ``command="..."`` ForceCommand so even a compromised key can
  only execute the launcher; this is an operational concern, not
  a code one — we document the contract here so the install
  playbook can lock it down.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

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
from matofx.tools.exec_orm import AosHost

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.db.session import SessionFactory
    from matofx.secrets import SecretsStore


_EMPTY_CONTEXT: TargetContext = TargetContext()
"""Module-level sentinel — protocol-parity default for ``context``."""


class AosTargetError(RuntimeError):
    """Raised when the SSH target can't reach or use an AOS host.

    Plain-English message — surfaces directly to the operator on
    the failed-job audit trail.
    """


@dataclass(frozen=True)
class SshConnectOptions:
    """Resolved connect parameters for an :class:`asyncssh.SSHClientConnection`.

    Built from an :class:`AosHost` row plus the resolved private
    key material from the secrets store. Frozen so the connection
    options don't change once the connection is in flight.
    """

    hostname: str
    port: int
    username: str
    client_keys: tuple[bytes, ...]
    known_hosts: str | None
    """Path to the pinned ``known_hosts`` file. ``None`` falls back
    to the default user known_hosts; production deployments should
    always pin per-host."""


class SshAosTarget:
    """Run jobs on a remote Linux host over SSH.

    The constructor takes:

    - ``session_factory``: looks up :class:`AosHost` rows by id at
      submit time, so the runner can serve every registered host
      from a single target instance.
    - ``secrets_store``: resolves the host's ``secret_ref`` to a
      private key (PEM bytes).
    - ``known_hosts_path_resolver``: optional callback that returns
      the per-host ``known_hosts`` file path. When ``None``, the
      target uses asyncssh's default which reads
      ``~/.ssh/known_hosts``. Production should pin per-host.

    The target itself holds no per-host state — every submit /
    cancel call opens a fresh SSH connection, runs one short
    command, and closes. SSH connection setup is cheap (≤ 100ms on
    a healthy LAN) and the runner's polling cadence (default 2 s)
    keeps connection churn low. Phase 2 may pool connections.
    """

    target_kind: ExecutionTargetKind = "aos"

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        secrets_store: SecretsStore,
        known_hosts_path_resolver: Callable[[int], Awaitable[str | None]] | None = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        """Construct the target. See class docstring for parameter notes."""
        self._session_factory = session_factory
        self._secrets_store = secrets_store
        self._known_hosts_resolver = known_hosts_path_resolver
        self._connect_timeout_s = connect_timeout_s

    # ----- ExecutionTarget protocol --------------------------------------

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> SubmitResult:
        """Spawn the launcher on the AOS host, detached; return its remote PID.

        ``resource_request`` is honoured opportunistically: queue
        names and walltime translate to AOS-host job-runner hints
        when the install supports it; when it doesn't (a bare
        ``ssh -t``-style host), the values are ignored.
        """
        del resource_request  # AOS hosts honour these via the descriptor only in Phase 1.
        opts = await self._connect_options_for(context)
        cmd = _build_submit_command(
            launcher_path=staged.launcher_path,
            working_dir=staged.working_dir,
        )
        result = await self._run_remote(opts, cmd, op="submit")
        pid = _parse_pid(result.stdout)
        if pid is None:
            raise AosTargetError(
                "The remote host accepted the launcher command but didn't return a PID. "
                f"Stdout: {result.stdout!r}; stderr: {result.stderr!r}."
            )
        return SubmitResult(pid=pid)

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> JobObservation:
        """Read the heartbeat directory off the shared mount — no SSH needed."""
        del context
        return await asyncio.to_thread(_read_heartbeat, staged.heartbeat_dir)

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> None:
        """SIGTERM the remote PID, wait grace, then SIGKILL.

        Best-effort: the kill commands are wrapped in ``2>/dev/null``
        so a process that's already exited doesn't raise. The
        runner's heartbeat watcher promotes the canonical state
        once the launcher writes ``failed.json``.
        """
        del staged  # heartbeat path lives on shared FS; cancel acts on the remote PID
        pid = submit_result.pid
        if pid is None:
            return
        opts = await self._connect_options_for(context)
        # Negative PID with kill targets the process group, which the
        # launcher creates via os.setsid in launcher.py. That way the
        # tool's children get cleaned up too.
        term_cmd = f"kill -TERM -{pid} 2>/dev/null; kill -TERM {pid} 2>/dev/null"
        await self._run_remote(opts, term_cmd, op="cancel-term", check=False)
        await asyncio.sleep(max(grace_seconds, 0))
        kill_cmd = f"kill -KILL -{pid} 2>/dev/null; kill -KILL {pid} 2>/dev/null"
        await self._run_remote(opts, kill_cmd, op="cancel-kill", check=False)

    async def fetch_logs(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> LogsRef:
        """Return paths to the launcher-captured logs on the shared mount."""
        del context
        return LogsRef(
            stdout_path=staged.logs_dir / "stdout.log",
            stderr_path=staged.logs_dir / "stderr.log",
        )

    # ----- internals -----------------------------------------------------

    async def _connect_options_for(self, context: TargetContext) -> SshConnectOptions:
        """Resolve a :class:`SshConnectOptions` for the host id in ``context``."""
        host_id = context.aos_host_id
        if host_id is None:
            raise AosTargetError(
                "AOS execution requires the tool to be linked to an always-on server. "
                "Set the tool's AOS host in the admin UI."
            )
        host = await self._load_host(host_id)
        if host is None:
            raise AosTargetError(
                f"Always-on server #{host_id} is no longer registered. "
                "Re-link the tool to an active host."
            )
        if not host.is_active:
            raise AosTargetError(
                f"Always-on server {host.name!r} is currently deactivated. "
                "Re-activate it in the admin UI before submitting jobs."
            )
        if host.transport != "ssh":
            raise AosTargetError(
                f"Always-on server {host.name!r} uses transport {host.transport!r}, "
                "but this runner only handles SSH transports. Use a Windows AOS target instead."
            )

        secret = await self._secrets_store.resolve(host.secret_ref)
        known_hosts = (
            await self._known_hosts_resolver(host_id) if self._known_hosts_resolver else None
        )
        return SshConnectOptions(
            hostname=host.hostname,
            port=host.port,
            username=host.username,
            client_keys=(secret.as_bytes(),),
            known_hosts=known_hosts,
        )

    async def _load_host(self, host_id: int) -> AosHost | None:
        from matofx.db.session import session_scope  # noqa: PLC0415 — late import to avoid cycles

        async with session_scope(self._session_factory) as session:
            return await self._fetch_host(session, host_id)

    async def _fetch_host(self, session: AsyncSession, host_id: int) -> AosHost | None:
        return await session.get(AosHost, host_id)

    async def _run_remote(
        self,
        opts: SshConnectOptions,
        command: str,
        *,
        op: str,
        check: bool = True,
    ) -> _RemoteRunResult:
        """Open an SSH connection, run ``command``, return the captured output.

        ``op`` is included in error messages so "submit failed" /
        "cancel failed" surfaces clearly to the operator.
        """
        try:
            async with asyncio.timeout(self._connect_timeout_s + 30.0):
                conn_kwargs: dict[str, Any] = {
                    "host": opts.hostname,
                    "port": opts.port,
                    "username": opts.username,
                    "client_keys": list(opts.client_keys),
                    "connect_timeout": self._connect_timeout_s,
                }
                if opts.known_hosts is not None:
                    conn_kwargs["known_hosts"] = opts.known_hosts
                async with asyncssh.connect(**conn_kwargs) as conn:
                    proc = await conn.run(command, check=False)
        except asyncssh.HostKeyNotVerifiable as e:
            raise AosTargetError(
                f"The always-on server {opts.hostname!r} presented a host key that doesn't match "
                "the pinned known_hosts entry. Verify the host hasn't been replaced and update "
                "the pin if intentional."
            ) from e
        except asyncssh.PermissionDenied as e:
            raise AosTargetError(
                f"The always-on server {opts.hostname!r} refused our credentials. "
                "Check the secret stored for this host."
            ) from e
        except (OSError, asyncssh.Error, TimeoutError) as e:
            raise AosTargetError(
                f"Could not {op} on always-on server {opts.hostname!r}: {e}. "
                "Check the host is reachable and the runner has network access to it."
            ) from e

        stdout = _ensure_text(proc.stdout)
        stderr = _ensure_text(proc.stderr)
        exit_code = proc.exit_status if proc.exit_status is not None else -1

        if check and exit_code != 0:
            raise AosTargetError(
                f"The {op} command failed on {opts.hostname!r} "
                f"(exit {exit_code}): {stderr.strip() or stdout.strip()}"
            )
        return _RemoteRunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


@dataclass(frozen=True)
class _RemoteRunResult:
    exit_code: int
    stdout: str
    stderr: str


def _build_submit_command(*, launcher_path: Path, working_dir: Path) -> str:
    """Build the bash command that detaches the launcher and prints its PID.

    The command:

    1. ``cd`` into the working directory so relative paths in the
       launcher behave the same way as on the local target.
    2. ``setsid nohup python launcher.py`` — detach into a new
       session so SIGHUP from the SSH session dropping doesn't
       reach the launcher; redirect stdin/stdout/stderr to
       ``/dev/null`` so the launcher doesn't keep the SSH channel
       open. The launcher itself opens the real log files.
    3. ``echo $!`` — print the background PID on stdout.

    Quoting uses :func:`shlex.quote` to handle paths with spaces /
    special characters; the rest of the command is fixed and safe.
    """
    wd = shlex.quote(str(working_dir))
    launcher = shlex.quote(str(launcher_path))
    # ``setsid`` ensures the launcher gets its own process group, so
    # ``kill -PGID`` later cleans up the tool's children. ``nohup``
    # is belt-and-braces in case the SSH server's pty bind sends
    # SIGHUP before setsid takes effect.
    return f"cd {wd} && setsid nohup python3 {launcher} </dev/null >/dev/null 2>&1 & echo $!"


def _parse_pid(stdout: str) -> int | None:
    """Pull the first non-empty line out of ``stdout`` and parse as int."""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def _ensure_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


# ----- shared with LocalTarget; duplicated here to keep aos_ssh self-contained


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
