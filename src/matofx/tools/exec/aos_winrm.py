"""WinRM-backed Always-On-Server execution target.

:class:`WinRmAosTarget` is the Windows-native sibling of
:class:`matofx.tools.exec.aos_ssh.SshAosTarget`. The contract is
identical — submit / poll / cancel / fetch_logs — and the shared
mount is still the source of truth for working dirs, heartbeat
files, and logs. The only differences are the wire protocol
(WinRM over HTTP/HTTPS instead of SSH) and the on-host commands
(PowerShell ``Start-Process`` / ``taskkill`` instead of bash
``setsid nohup`` / ``kill``).

Authentication: NTLM with the host's username from the AOS host
row plus the password resolved from the secrets store. The
``secret_ref`` column is opaque to MATOFX and MATOFX never logs
the password material — :class:`matofx.secrets.Secret` carries a
redacted ``repr`` for that reason.

Transport: HTTPS by default when the host's port is 5986 (the
WinRM default for HTTPS). Plain HTTP (5985) is permitted but
discouraged — production deployments should pin a CA-signed cert
and use 5986. NTLM provides message-level encryption either way,
so HTTP isn't a credential leak, but the launcher invocation
arguments aren't private without TLS.

pywinrm is a synchronous library; this target wraps every WinRM
call in :func:`asyncio.to_thread` so the runner's event loop never
blocks on the network.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import winrm

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
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.db.session import SessionFactory
    from matofx.secrets import SecretsStore


_EMPTY_CONTEXT: TargetContext = TargetContext()
"""Module-level sentinel — protocol-parity default for ``context``."""

_WINRM_HTTPS_DEFAULT_PORT: int = 5986
"""WinRM's default HTTPS port. Hosts on this port get TLS + cert validation."""


# Re-use the SSH target's error class so callers can catch a single
# AosTargetError type regardless of which transport is in use.
from matofx.tools.exec.aos_ssh import AosTargetError  # noqa: E402


@dataclass(frozen=True)
class WinRmConnectOptions:
    """Resolved connection parameters for a :class:`winrm.Session`.

    Built from an :class:`AosHost` row plus the resolved password
    bytes from the secrets store. Frozen so the connection options
    don't shift mid-flight.
    """

    endpoint: str
    """Full WinRM URL — e.g. ``"https://aos.example.com:5986/wsman"``."""

    username: str
    password: bytes
    transport: str
    """pywinrm transport name. ``"ntlm"`` is the Phase 1 default."""

    server_cert_validation: str
    """Always ``"validate"`` on the AOS path — WinRM runs over HTTPS with
    certificate validation; MATOFX never sends NTLM credentials over
    plaintext HTTP (audit S4)."""

    def __repr__(self) -> str:
        """Redacted repr — never echo the password bytes (audit B6).

        The default dataclass ``repr`` would print the raw credential, so a
        stray ``log.debug(opts)`` or an exception that captures locals would
        leak it. Mirror :class:`~matofx.secrets.Secret`'s redaction.
        """
        return (
            f"WinRmConnectOptions(endpoint={self.endpoint!r}, "
            f"username={self.username!r}, transport={self.transport!r}, "
            f"server_cert_validation={self.server_cert_validation!r}, "
            f"password=<{len(self.password)} bytes redacted>)"
        )


class WinRmAosTarget:
    """Run jobs on a remote Windows host over WinRM.

    Constructed once per process. Looks up :class:`AosHost` rows by
    id at submit / cancel time, so a single instance serves every
    registered Windows host. The target itself is stateless — every
    WinRM call opens a fresh session and runs one short PowerShell
    command. WinRM session setup is heavier than SSH (≤ 500ms on a
    healthy LAN) but the runner's polling cadence keeps churn low,
    and ``poll`` / ``fetch_logs`` don't use WinRM at all.
    """

    target_kind: ExecutionTargetKind = "aos"

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        secrets_store: SecretsStore,
        operation_timeout_s: int = 30,
        read_timeout_s: int = 60,
    ) -> None:
        """Construct the target.

        Args:
            session_factory: Async DB session factory used to look up
                :class:`AosHost` rows.
            secrets_store: Resolves the host's ``secret_ref`` to the
                NTLM password bytes.
            operation_timeout_s: WinRM operation timeout. The remote
                host should complete the launcher command well within
                this window — the launcher itself doesn't run a
                long-lived job, it just spawns one.
            read_timeout_s: HTTP read timeout for the WinRM client.
                Must be > ``operation_timeout_s``.
        """
        self._session_factory = session_factory
        self._secrets_store = secrets_store
        self._operation_timeout_s = operation_timeout_s
        self._read_timeout_s = read_timeout_s

    # ----- ExecutionTarget protocol --------------------------------------

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> SubmitResult:
        """Spawn the launcher on the Windows host, detached; return its remote PID.

        ``Start-Process`` with ``-PassThru`` returns a process
        object whose ``Id`` is the new PID. ``-WindowStyle Hidden``
        prevents a console window from briefly flickering on
        interactive Windows hosts.
        """
        del resource_request  # Phase 1 honours these via the descriptor only.
        opts = await self._connect_options_for(context)
        ps_script = _build_submit_powershell(
            launcher_path=staged.launcher_path,
            working_dir=staged.working_dir,
        )
        result = await self._run_remote(opts, ps_script, op="submit")
        pid = _parse_pid(result.stdout)
        if pid is None:
            raise AosTargetError(
                "The remote Windows host accepted the launcher command but didn't return a PID. "
                f"Stdout: {result.stdout!r}; stderr: {result.stderr!r}."
            )
        return SubmitResult(pid=pid)

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> JobObservation:
        """Read the heartbeat directory off the shared mount — no WinRM needed."""
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
        """``taskkill /T`` the remote PID, wait grace, then ``/T /F``.

        ``/T`` walks the process tree (the launcher's children),
        same convention as the local Windows cancel path. Best
        effort: a process that's already exited returns non-zero
        from taskkill but doesn't raise here.
        """
        del staged  # heartbeat path lives on shared FS; cancel acts on the remote PID
        pid = submit_result.pid
        if pid is None:
            return
        opts = await self._connect_options_for(context)
        term_script = _build_taskkill(pid, force=False)
        kill_script = _build_taskkill(pid, force=True)
        await self._run_remote(opts, term_script, op="cancel-term", check=False)
        await asyncio.sleep(max(grace_seconds, 0))
        await self._run_remote(opts, kill_script, op="cancel-kill", check=False)

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

    async def _connect_options_for(self, context: TargetContext) -> WinRmConnectOptions:
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
        if host.transport != "winrm":
            raise AosTargetError(
                f"Always-on server {host.name!r} uses transport {host.transport!r}, "
                "but this runner only handles WinRM transports. Use a Linux AOS target instead."
            )

        secret = await self._secrets_store.resolve(host.secret_ref)
        # WinRM carries NTLM credentials, so MATOFX always uses HTTPS with
        # certificate validation — it never falls back to plaintext HTTP
        # (audit S4). The default WinRM HTTPS port is 5986; a host on a
        # non-standard HTTPS port is fine (scheme stays https). A self-signed
        # certificate must be added to the runner host's trust store rather
        # than disabling validation.
        endpoint = f"https://{host.hostname}:{host.port}/wsman"
        return WinRmConnectOptions(
            endpoint=endpoint,
            username=host.username,
            password=secret.as_bytes(),
            transport="ntlm",
            server_cert_validation="validate",
        )

    async def _load_host(self, host_id: int) -> AosHost | None:
        from matofx.db.session import session_scope  # noqa: PLC0415 — late import

        async with session_scope(self._session_factory) as session:
            return await self._fetch_host(session, host_id)

    async def _fetch_host(self, session: AsyncSession, host_id: int) -> AosHost | None:
        return await session.get(AosHost, host_id)

    async def _run_remote(
        self,
        opts: WinRmConnectOptions,
        ps_script: str,
        *,
        op: str,
        check: bool = True,
    ) -> _RemoteRunResult:
        """Run a PowerShell script via WinRM; return its captured output."""

        def _run() -> _RemoteRunResult:
            try:
                session = winrm.Session(
                    target=opts.endpoint,
                    auth=(opts.username, opts.password.decode("utf-8", errors="strict")),
                    transport=opts.transport,
                    server_cert_validation=opts.server_cert_validation,
                    operation_timeout_sec=self._operation_timeout_s,
                    read_timeout_sec=self._read_timeout_s,
                )
                response = session.run_ps(ps_script)
            except UnicodeDecodeError as e:
                raise AosTargetError(
                    f"The credential stored for {opts.endpoint!r} isn't valid UTF-8 — "
                    "WinRM expects a string password."
                ) from e
            except Exception as e:
                raise AosTargetError(
                    f"Could not {op} on Windows always-on server {opts.endpoint!r}: {e}. "
                    "Check the host is reachable, the credentials are correct, and "
                    "WinRM is enabled."
                ) from e
            stdout = _ensure_text(response.std_out)
            stderr = _ensure_text(response.std_err)
            exit_code = response.status_code if response.status_code is not None else -1
            return _RemoteRunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

        result = await asyncio.to_thread(_run)
        if check and result.exit_code != 0:
            raise AosTargetError(
                f"The {op} command failed on {opts.endpoint!r} "
                f"(exit {result.exit_code}): {result.stderr.strip() or result.stdout.strip()}"
            )
        return result


@dataclass(frozen=True)
class _RemoteRunResult:
    exit_code: int
    stdout: str
    stderr: str


def _ps_quote(value: str) -> str:
    r"""Quote a string for PowerShell single-quote literals.

    PowerShell single-quote literals don't expand variables and
    escape ``'`` by doubling: ``'don''t'``. Backslashes are
    *literal* inside single-quote literals — no special escaping
    needed for Windows paths.
    """
    return "'" + value.replace("'", "''") + "'"


def _build_submit_powershell(*, launcher_path: Path, working_dir: Path) -> str:
    """Build the PowerShell script that detaches the launcher and prints its PID.

    The script:

    1. ``Start-Process`` with the python executable from ``PATH``,
       ``-ArgumentList`` the launcher path, ``-WorkingDirectory`` the
       job's working dir, ``-PassThru`` so we get the process
       object back, ``-WindowStyle Hidden`` so no console flickers.
    2. ``Write-Output $proc.Id`` — print the new PID on stdout.

    The launcher itself opens its own log files; we redirect
    Start-Process stdout/stderr to ``$null`` so the WinRM channel
    can close cleanly.
    """
    wd = _ps_quote(str(working_dir))
    launcher = _ps_quote(str(launcher_path))
    # ``python.exe`` is resolved on the remote ``PATH``; admins who
    # run a pinned interpreter point ``PATH`` (or the
    # :class:`PythonEnv` row's ``interpreter_path``) at it. The
    # launcher itself does not depend on which python it runs under.
    return (
        "$ErrorActionPreference = 'Stop'; "
        f"$proc = Start-Process -FilePath 'python.exe' -ArgumentList {launcher} "
        f"-WorkingDirectory {wd} -PassThru -WindowStyle Hidden "
        "-RedirectStandardOutput $null -RedirectStandardError $null; "
        "Write-Output $proc.Id"
    )


def _build_taskkill(pid: int, *, force: bool) -> str:
    """Return the PowerShell command that walks and kills the PID's tree."""
    flag = "/T /F" if force else "/T"
    return f"taskkill /PID {int(pid)} {flag} 2>$null; exit 0"


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


# ----- shared with LocalTarget / SshAosTarget -----------------------------


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


# Re-export AosTargetError so callers can import it from this module
# alongside WinRmAosTarget without reaching back into aos_ssh.
__all__ = ["AosTargetError", "WinRmAosTarget", "WinRmConnectOptions"]
