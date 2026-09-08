"""Microsoft HPC Pack execution target.

:class:`MsHpcHpcTarget` is the Windows-cluster sibling of
:class:`matofx.tools.exec.hpc_slurm.SlurmHpcTarget`. The contract is
identical (submit / poll / cancel / fetch_logs); only the wire
protocol (WinRM) and the head-node commands (PowerShell HPC
cmdlets) differ:

- **submit** — ``New-HpcJob`` → ``Add-HpcTask`` → ``Submit-HpcJob``;
  parses ``$submitted.Id`` for the cluster-side job id.
- **poll** — reads launcher heartbeat from the shared mount; falls
  back to ``Get-HpcJob`` when the heartbeat hasn't appeared, or
  promotes to ``"failed"`` when the scheduler reports terminal
  state without a launcher heartbeat.
- **cancel** — ``Stop-HpcJob -Id <id>``.
- **fetch_logs** — paths on the shared mount.

The Microsoft HPC PowerShell module (``Microsoft.HPC``) is
``Import-Module``-ed at the top of every script. Customer head
nodes that don't have it installed surface as a friendly
:class:`HpcTargetError`.
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
from matofx.tools.exec.hpc_slurm import HpcTargetError
from matofx.tools.exec.staging import StagedWorkingDir
from matofx.tools.exec_orm import HpcCluster

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.db.session import SessionFactory
    from matofx.secrets import SecretsStore


_EMPTY_CONTEXT: TargetContext = TargetContext()

_WINRM_HTTPS_DEFAULT_PORT: int = 5986

# Microsoft HPC Pack scheduler states. Anything in this set means
# the launcher won't run (or won't run any further), so we promote
# to ``"failed"`` if no terminal heartbeat exists.
_MSHPC_TERMINAL_STATES: frozenset[str] = frozenset({"Finished", "Failed", "Canceled"})
"""See https://learn.microsoft.com/en-us/powershell/high-performance-computing/jobstate-enumeration."""


@dataclass(frozen=True)
class WinRmConnectOptions:
    """Resolved connect parameters for a Microsoft HPC head-node WinRM session."""

    endpoint: str
    username: str
    password: bytes
    transport: str
    server_cert_validation: str

    def __repr__(self) -> str:
        """Redacted repr — never echo the password bytes (audit B6)."""
        return (
            f"WinRmConnectOptions(endpoint={self.endpoint!r}, "
            f"username={self.username!r}, transport={self.transport!r}, "
            f"server_cert_validation={self.server_cert_validation!r}, "
            f"password=<{len(self.password)} bytes redacted>)"
        )


class MsHpcHpcTarget:
    """Run jobs on a Microsoft HPC Pack cluster via WinRM to the head node.

    The constructor takes:

    - ``session_factory``: looks up :class:`HpcCluster` rows by id.
    - ``secrets_store``: resolves the cluster's ``secret_ref`` to a
      password (UTF-8 encoded bytes; NTLM auth).
    """

    target_kind: ExecutionTargetKind = "hpc"

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        secrets_store: SecretsStore,
        operation_timeout_s: int = 30,
        read_timeout_s: int = 60,
    ) -> None:
        """Construct the target. See class docstring for parameter notes."""
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
        """Submit via ``New-HpcJob``/``Add-HpcTask``/``Submit-HpcJob``."""
        opts = await self._connect_options_for(context)
        cluster = await self._load_cluster(_require_cluster_id(context))
        if cluster is None:  # pragma: no cover — connect_options validated
            raise HpcTargetError(f"HPC cluster #{context.hpc_cluster_id} not found.")
        ps_script = _build_submit_powershell(
            staged=staged,
            resource_request=resource_request,
            default_queue=cluster.default_queue,
        )
        result = await self._run_remote(opts, ps_script, op="submit")
        jobid = _parse_jobid(result.stdout)
        if jobid is None:
            raise HpcTargetError(
                "Microsoft HPC accepted the submission but didn't return a job id. "
                f"Stdout: {result.stdout!r}; stderr: {result.stderr!r}."
            )
        return SubmitResult(scheduler_job_id=jobid)

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> JobObservation:
        """Combine launcher heartbeat with ``Get-HpcJob`` state."""
        hb = await asyncio.to_thread(_read_heartbeat, staged.heartbeat_dir)
        if hb.heartbeat_state in ("complete", "failed"):
            return hb

        scheduler_job_id = _scheduler_job_id_for_poll(context)
        if scheduler_job_id is None:
            return hb

        sched_state = await self._scheduler_state(context, scheduler_job_id)
        observation = JobObservation(
            heartbeat_state=hb.heartbeat_state,
            started_at=hb.started_at,
            ended_at=hb.ended_at,
            exit_code=hb.exit_code,
            signal_name=hb.signal_name,
            failure_reason=hb.failure_reason,
            scheduler_state=sched_state,
        )
        if sched_state is not None and sched_state in _MSHPC_TERMINAL_STATES:
            return JobObservation(
                heartbeat_state="failed",
                started_at=observation.started_at,
                ended_at=observation.ended_at,
                exit_code=observation.exit_code,
                signal_name=observation.signal_name,
                failure_reason=(
                    f"Microsoft HPC reported state {sched_state!r} without a launcher heartbeat. "
                    "The job was likely killed by the scheduler (runtime, memory, or "
                    "manual Stop-HpcJob)."
                ),
                scheduler_state=sched_state,
            )
        return observation

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> None:
        """Issue ``Stop-HpcJob -Id <id>`` and wait the grace period."""
        del staged
        scheduler_job_id = submit_result.scheduler_job_id
        if scheduler_job_id is None:
            return
        opts = await self._connect_options_for(context)
        ps = (
            "Import-Module Microsoft.HPC -ErrorAction Stop; "
            f"Stop-HpcJob -Id {int(scheduler_job_id)} -ErrorAction Stop"
        )
        await self._run_remote(opts, ps, op="cancel", check=False)
        await asyncio.sleep(max(grace_seconds, 0))

    async def fetch_logs(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> LogsRef:
        """Return paths to launcher stdout / stderr on the shared mount."""
        del context
        return LogsRef(
            stdout_path=staged.logs_dir / "stdout.log",
            stderr_path=staged.logs_dir / "stderr.log",
        )

    # ----- internals -----------------------------------------------------

    async def _scheduler_state(self, context: TargetContext, scheduler_job_id: str) -> str | None:
        opts = await self._connect_options_for(context)
        ps = (
            "Import-Module Microsoft.HPC -ErrorAction Stop; "
            f"(Get-HpcJob -Id {int(scheduler_job_id)} -ErrorAction Stop).State"
        )
        try:
            result = await self._run_remote(opts, ps, op="poll", check=False)
        except HpcTargetError:
            return None
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line:
                return line
        return None

    async def _connect_options_for(self, context: TargetContext) -> WinRmConnectOptions:
        cluster_id = _require_cluster_id(context)
        cluster = await self._load_cluster(cluster_id)
        if cluster is None:
            raise HpcTargetError(
                f"HPC cluster #{cluster_id} is no longer registered. "
                "Re-link the tool to an active cluster."
            )
        if not cluster.is_active:
            raise HpcTargetError(
                f"HPC cluster {cluster.name!r} is currently deactivated. "
                "Re-activate it in the admin UI before submitting jobs."
            )
        if cluster.scheduler != "mshpc":
            raise HpcTargetError(
                f"HPC cluster {cluster.name!r} runs scheduler {cluster.scheduler!r}, "
                "but this target only handles Microsoft HPC. Use a Slurm target instead."
            )
        if cluster.transport != "winrm":
            raise HpcTargetError(
                f"HPC cluster {cluster.name!r} uses transport {cluster.transport!r}, "
                "but this target only handles WinRM transports."
            )

        secret = await self._secrets_store.resolve(cluster.secret_ref)
        # WinRM carries NTLM credentials, so MATOFX always uses HTTPS with
        # certificate validation — never plaintext HTTP (audit S4). Default
        # WinRM HTTPS port is 5986; a non-standard HTTPS port is fine. A
        # self-signed cert must be trusted on the runner host, not ignored.
        endpoint = f"https://{cluster.head_node}:{cluster.port}/wsman"
        return WinRmConnectOptions(
            endpoint=endpoint,
            username=cluster.username,
            password=secret.as_bytes(),
            transport="ntlm",
            server_cert_validation="validate",
        )

    async def _load_cluster(self, cluster_id: int) -> HpcCluster | None:
        from matofx.db.session import session_scope  # noqa: PLC0415

        async with session_scope(self._session_factory) as session:
            return await self._fetch_cluster(session, cluster_id)

    async def _fetch_cluster(self, session: AsyncSession, cluster_id: int) -> HpcCluster | None:
        return await session.get(HpcCluster, cluster_id)

    async def _run_remote(
        self,
        opts: WinRmConnectOptions,
        ps_script: str,
        *,
        op: str,
        check: bool = True,
    ) -> _RemoteRunResult:
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
                raise HpcTargetError(
                    f"The credential stored for {opts.endpoint!r} isn't valid UTF-8 — "
                    "WinRM expects a string password."
                ) from e
            except Exception as e:
                raise HpcTargetError(
                    f"Could not {op} on Microsoft HPC head node {opts.endpoint!r}: {e}. "
                    "Check the cluster is reachable, credentials are correct, and "
                    "WinRM is enabled."
                ) from e
            stdout = _ensure_text(response.std_out)
            stderr = _ensure_text(response.std_err)
            exit_code = response.status_code if response.status_code is not None else -1
            return _RemoteRunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

        result = await asyncio.to_thread(_run)
        if check and result.exit_code != 0:
            raise HpcTargetError(
                f"The {op} command failed on {opts.endpoint!r} "
                f"(exit {result.exit_code}): {result.stderr.strip() or result.stdout.strip()}"
            )
        return result


@dataclass(frozen=True)
class _RemoteRunResult:
    exit_code: int
    stdout: str
    stderr: str


# ----- command + parsing helpers ----------------------------------------


def _ps_quote(value: str) -> str:
    """Quote a string for PowerShell single-quote literals."""
    return "'" + value.replace("'", "''") + "'"


def _format_runtime(walltime_minutes: int) -> str:
    """Format a minutes-int as HPC ``HH:MM:SS``."""
    minutes = max(walltime_minutes, 1)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:00"


def _build_submit_powershell(
    *,
    staged: StagedWorkingDir,
    resource_request: ResourceRequest,
    default_queue: str | None,
) -> str:
    """Render the PowerShell pipeline that submits the launcher to MS HPC.

    Translates :class:`ResourceRequest` to HPC parameters:

    - ``cpu_cores`` → ``-NumCores`` (treated as total cores; HPC
      sums these across nodes if -AutoCalculateMin/Max isn't set)
    - ``walltime_minutes`` → ``-RunTime "HH:MM:SS"``
    - ``queue_override`` (or ``default_queue``) → ``-NodeGroups``
    - ``gpu_count`` is documented but not currently passed —
      Microsoft HPC handles GPUs via node-group placement.
    """
    wd = _ps_quote(str(staged.working_dir))
    launcher = _ps_quote(str(staged.launcher_path))
    job_name = _ps_quote(f"matofx-{staged.working_dir.name}")

    new_job_args = [f"-Name {job_name}", f"-NumCores {int(resource_request.cpu_cores)}"]
    if resource_request.walltime_minutes is not None:
        runtime = _ps_quote(_format_runtime(resource_request.walltime_minutes))
        new_job_args.append(f"-RunTime {runtime}")
    queue = resource_request.queue_override or default_queue
    if queue:
        new_job_args.append(f"-NodeGroups {_ps_quote(queue)}")

    cmd_line = _ps_quote("python launcher.py")
    return (
        "Import-Module Microsoft.HPC -ErrorAction Stop; "
        f"$job = New-HpcJob {' '.join(new_job_args)} -ErrorAction Stop; "
        f"Add-HpcTask -Job $job -CommandLine {cmd_line} -WorkDir {wd} "
        "-Stdout 'logs\\hpc-task.out' -Stderr 'logs\\hpc-task.err' -ErrorAction Stop | Out-Null; "
        "$submitted = Submit-HpcJob -Job $job -ErrorAction Stop; "
        "Write-Output $submitted.Id"
    ) + f" # launcher={launcher}"  # launcher path embedded for audit; not used in command


def _parse_jobid(stdout: str) -> str | None:
    """Pull the first non-empty line out of ``stdout`` and parse as int.

    HPC job ids are positive integers; anything else surfaces as
    ``None`` and the caller raises a friendly error.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.isdigit() and int(line) > 0:
            return line
    return None


def _require_cluster_id(context: TargetContext) -> int:
    if context.hpc_cluster_id is None:
        raise HpcTargetError(
            "HPC execution requires the tool to be linked to an HPC cluster. "
            "Set the tool's HPC cluster in the admin UI."
        )
    return context.hpc_cluster_id


def _scheduler_job_id_for_poll(context: TargetContext) -> str | None:
    """See :func:`matofx.tools.exec.hpc_slurm._scheduler_job_id_for_poll`."""
    return context.scheduler_job_id


def _ensure_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


# ----- shared with LocalTarget / AOS targets / Slurm --------------------


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


__all__ = [
    "MsHpcHpcTarget",
    "WinRmConnectOptions",
]
