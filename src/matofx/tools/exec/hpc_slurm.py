"""Slurm HPC execution target.

:class:`SlurmHpcTarget` submits jobs to a Slurm cluster over SSH.
The shared mount is the source of truth for working dirs, heartbeat
files, and logs — the same model used by the AOS targets — and SSH
is only used to talk to the head node:

- **submit** — ``sbatch --wrap='cd <wd> && python3 launcher.py'``
  (plus translated resource flags, and the cluster's ``--account`` /
  ``--wckey`` where the site charges time to a project). Parses the
  Slurm jobid out of ``"Submitted batch job N"``.
- **poll** — reads the launcher's heartbeat off the shared mount;
  falls back to ``squeue`` / ``sacct`` when the heartbeat hasn't
  appeared yet (job still queued) or when the scheduler killed the
  launcher externally (heartbeat absent + scheduler reports terminal).
- **cancel** — ``scancel <jobid>``. The scheduler reaps the
  compute-node process tree.
- **fetch_logs** — paths on the shared mount.

Slurm jobs return their cluster-side handle as ``scheduler_job_id``
on :class:`SubmitResult`; ``pid`` is ``None`` because the actual PID
lives on a compute node we don't directly touch.
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
from matofx.tools.exec_orm import HpcCluster

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.db.session import SessionFactory
    from matofx.secrets import SecretsStore


_EMPTY_CONTEXT: TargetContext = TargetContext()


class HpcTargetError(RuntimeError):
    """Raised when an HPC target can't reach or use a cluster head node.

    Plain-English message — surfaces directly to the operator on the
    failed-job audit trail.
    """


# Slurm scheduler states we consider terminal. ``squeue`` only lists
# active jobs; once a job leaves the queue, ``sacct`` is the
# authority. Both vocabularies are normalised here.
_SLURM_TERMINAL_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"}
)
_SLURM_RUNNING_STATES: frozenset[str] = frozenset({"RUNNING", "COMPLETING"})


@dataclass(frozen=True)
class SshConnectOptions:
    """Resolved connect parameters for a Slurm head-node SSH session."""

    hostname: str
    port: int
    username: str
    client_keys: tuple[bytes, ...]
    known_hosts: str | None


class SlurmHpcTarget:
    """Run jobs on a Slurm cluster via SSH to the head node.

    The constructor takes:

    - ``session_factory``: looks up :class:`HpcCluster` rows by id at
      submit / cancel / poll time.
    - ``secrets_store``: resolves the cluster's ``secret_ref`` to an
      SSH private key (PEM bytes).
    - ``known_hosts_path_resolver``: optional per-cluster
      ``known_hosts`` pin. Production deployments should always pin.
    """

    target_kind: ExecutionTargetKind = "hpc"

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
        """Submit via ``sbatch``; return ``SubmitResult(scheduler_job_id=...)``."""
        opts = await self._connect_options_for(context)
        cluster = await self._load_cluster(_require_cluster_id(context))
        if cluster is None:  # pragma: no cover — _connect_options_for already validated
            raise HpcTargetError(f"HPC cluster #{context.hpc_cluster_id} not found.")
        cmd = _build_sbatch_command(
            staged=staged,
            resource_request=resource_request,
            default_queue=cluster.default_queue,
            account=cluster.account,
            wckey=cluster.wckey,
        )
        result = await self._run_remote(opts, cmd, op="submit")
        jobid = _parse_sbatch_jobid(result.stdout)
        if jobid is None:
            raise HpcTargetError(
                "Slurm accepted the submission but didn't return a job id. "
                f"Stdout: {result.stdout!r}; stderr: {result.stderr!r}."
            )
        return SubmitResult(scheduler_job_id=jobid)

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> JobObservation:
        """Combine heartbeat and ``squeue`` / ``sacct`` state.

        - If the launcher wrote a terminal heartbeat (complete /
          failed), return that — it's authoritative.
        - Otherwise, ask the scheduler. While the scheduler reports
          a non-terminal state, return the heartbeat (which may
          itself be ``None`` for queued / starting jobs).
        - If the scheduler reports a terminal state but no terminal
          heartbeat exists, the launcher must have been killed
          externally; report ``heartbeat_state="failed"`` so the
          runner promotes the canonical state.
        """
        hb = await asyncio.to_thread(_read_heartbeat, staged.heartbeat_dir)
        if hb.heartbeat_state in ("complete", "failed"):
            return hb

        scheduler_job_id = _scheduler_job_id_for_poll(context)
        if scheduler_job_id is None:
            # No id passed; we can only report the heartbeat.
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
        if sched_state is not None and sched_state in _SLURM_TERMINAL_STATES:
            # Scheduler says done but launcher didn't write terminal
            # heartbeat — treat as failed so the runner promotes state.
            return JobObservation(
                heartbeat_state="failed",
                started_at=observation.started_at,
                ended_at=observation.ended_at,
                exit_code=observation.exit_code,
                signal_name=observation.signal_name,
                failure_reason=(
                    f"Slurm reported state {sched_state!r} without a launcher heartbeat. "
                    "The job was likely killed by the scheduler (walltime, memory, or "
                    "manual scancel)."
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
        """Issue ``scancel <jobid>``.

        ``grace_seconds`` is honoured by waiting after ``scancel``;
        Slurm sends SIGTERM then SIGKILL with its own kill-grace
        configured cluster-side. We don't double-kill.
        """
        del staged
        scheduler_job_id = submit_result.scheduler_job_id
        if scheduler_job_id is None:
            return
        opts = await self._connect_options_for(context)
        cmd = f"scancel {shlex.quote(scheduler_job_id)}"
        await self._run_remote(opts, cmd, op="cancel", check=False)
        # Give the scheduler a moment to propagate the cancel before
        # callers re-poll. We don't issue a second kill — Slurm owns
        # the escalation.
        await asyncio.sleep(max(grace_seconds, 0))

    async def fetch_logs(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext = _EMPTY_CONTEXT,
    ) -> LogsRef:
        """Return paths to the launcher's stdout / stderr on the shared mount."""
        del context
        return LogsRef(
            stdout_path=staged.logs_dir / "stdout.log",
            stderr_path=staged.logs_dir / "stderr.log",
        )

    # ----- internals -----------------------------------------------------

    async def _scheduler_state(self, context: TargetContext, scheduler_job_id: str) -> str | None:
        """Ask the scheduler what state ``scheduler_job_id`` is in.

        Tries ``squeue`` first (active jobs); falls back to ``sacct``
        for completed / cancelled / timed-out jobs. Returns ``None``
        when neither tool knows about the id (cluster history may
        have purged it; treat as unknown).
        """
        opts = await self._connect_options_for(context)
        squeue_cmd = f"squeue -h -j {shlex.quote(scheduler_job_id)} -o %T"
        try:
            squeue_result = await self._run_remote(opts, squeue_cmd, op="poll", check=False)
        except HpcTargetError:
            return None
        active = squeue_result.stdout.strip()
        if active:
            # Take the first line in case there are multi-step jobs.
            return active.splitlines()[0].strip()

        sacct_cmd = f"sacct -j {shlex.quote(scheduler_job_id)} --noheader -P -o State"
        try:
            sacct_result = await self._run_remote(opts, sacct_cmd, op="poll", check=False)
        except HpcTargetError:
            return None
        for raw in sacct_result.stdout.splitlines():
            state = raw.strip().split()[0] if raw.strip() else ""
            # ``sacct`` can return state with a trailing modifier like
            # "CANCELLED+" — strip that.
            state = state.rstrip("+")
            if state:
                return state
        return None

    async def _connect_options_for(self, context: TargetContext) -> SshConnectOptions:
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
        if cluster.scheduler != "slurm":
            raise HpcTargetError(
                f"HPC cluster {cluster.name!r} runs scheduler {cluster.scheduler!r}, "
                "but this target only handles Slurm. Use a Microsoft HPC target instead."
            )
        if cluster.transport != "ssh":
            raise HpcTargetError(
                f"HPC cluster {cluster.name!r} uses transport {cluster.transport!r}, "
                "but this target only handles SSH transports."
            )

        secret = await self._secrets_store.resolve(cluster.secret_ref)
        known_hosts = (
            await self._known_hosts_resolver(cluster_id) if self._known_hosts_resolver else None
        )
        return SshConnectOptions(
            hostname=cluster.head_node,
            port=cluster.port,
            username=cluster.username,
            client_keys=(secret.as_bytes(),),
            known_hosts=known_hosts,
        )

    async def _load_cluster(self, cluster_id: int) -> HpcCluster | None:
        from matofx.db.session import session_scope  # noqa: PLC0415

        async with session_scope(self._session_factory) as session:
            return await self._fetch_cluster(session, cluster_id)

    async def _fetch_cluster(self, session: AsyncSession, cluster_id: int) -> HpcCluster | None:
        return await session.get(HpcCluster, cluster_id)

    async def _run_remote(
        self,
        opts: SshConnectOptions,
        command: str,
        *,
        op: str,
        check: bool = True,
    ) -> _RemoteRunResult:
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
            raise HpcTargetError(
                f"The HPC head node {opts.hostname!r} presented a host key that doesn't match "
                "the pinned known_hosts entry. Verify the host hasn't been replaced and update "
                "the pin if intentional."
            ) from e
        except asyncssh.PermissionDenied as e:
            raise HpcTargetError(
                f"The HPC head node {opts.hostname!r} refused our credentials. "
                "Check the secret stored for this cluster."
            ) from e
        except (OSError, asyncssh.Error, TimeoutError) as e:
            raise HpcTargetError(
                f"Could not {op} on HPC head node {opts.hostname!r}: {e}. "
                "Check the cluster is reachable and the runner has network access to it."
            ) from e

        stdout = _ensure_text(proc.stdout)
        stderr = _ensure_text(proc.stderr)
        exit_code = proc.exit_status if proc.exit_status is not None else -1
        if check and exit_code != 0:
            raise HpcTargetError(
                f"The {op} command failed on {opts.hostname!r} "
                f"(exit {exit_code}): {stderr.strip() or stdout.strip()}"
            )
        return _RemoteRunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


@dataclass(frozen=True)
class _RemoteRunResult:
    exit_code: int
    stdout: str
    stderr: str


# ----- command + parsing helpers ----------------------------------------


def _build_sbatch_command(
    *,
    staged: StagedWorkingDir,
    resource_request: ResourceRequest,
    default_queue: str | None,
    account: str | None = None,
    wckey: str | None = None,
) -> str:
    """Render the ``sbatch --wrap=...`` command line.

    Resource flags translate from :class:`ResourceRequest`:

    - ``cpu_cores`` → ``--cpus-per-task``
    - ``memory_mb`` → ``--mem=<N>M``
    - ``walltime_minutes`` → ``--time=<N>`` (Slurm accepts plain
      minutes)
    - ``gpu_count`` → ``--gres=gpu:<N>``
    - ``queue_override`` (or ``default_queue``) → ``--partition=<N>``

    ``account`` and ``wckey`` come from the :class:`HpcCluster` row and
    name the project the run is charged to. Sites use one or the other
    (occasionally both), so they are emitted independently and a
    ``None`` emits nothing — a cluster that doesn't do accounting gets
    the same command line it always did.

    We deliberately don't pass ``--parsable``: the human "Submitted
    batch job N" form is what :func:`_parse_sbatch_jobid` reads, and it
    also carries any warning Slurm prints alongside the id.

    sbatch logs go to ``logs/sbatch.out`` / ``sbatch.err`` on the
    shared mount; the launcher itself writes its own
    ``logs/stdout.log`` / ``stderr.log`` files.
    """
    flags = ["sbatch"]
    flags.append(f"--cpus-per-task={resource_request.cpu_cores}")
    flags.append(f"--mem={resource_request.memory_mb}M")
    if resource_request.walltime_minutes is not None:
        flags.append(f"--time={resource_request.walltime_minutes}")
    if resource_request.gpu_count > 0:
        flags.append(f"--gres=gpu:{resource_request.gpu_count}")
    queue = resource_request.queue_override or default_queue
    if queue:
        flags.append(f"--partition={shlex.quote(queue)}")
    if account:
        flags.append(f"--account={shlex.quote(account)}")
    if wckey:
        flags.append(f"--wckey={shlex.quote(wckey)}")

    sbatch_out = shlex.quote(str(staged.logs_dir / "sbatch.out"))
    sbatch_err = shlex.quote(str(staged.logs_dir / "sbatch.err"))
    flags.append(f"--output={sbatch_out}")
    flags.append(f"--error={sbatch_err}")
    flags.append(f"--job-name=matofx-{staged.working_dir.name}")

    wd = shlex.quote(str(staged.working_dir))
    launcher = shlex.quote(str(staged.launcher_path))
    wrap = f"cd {wd} && python3 {launcher}"
    flags.append(f"--wrap={shlex.quote(wrap)}")
    return " ".join(flags)


_SBATCH_PREFIX_PARTS: int = 4
"""Number of tokens in the standard sbatch line ``["Submitted","batch","job","<id>"]``."""


def _parse_sbatch_jobid(stdout: str) -> str | None:
    """Pull the Slurm jobid out of ``"Submitted batch job 12345"``."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Standard sbatch output: "Submitted batch job N"
        if line.startswith("Submitted batch job"):
            parts = line.split()
            if len(parts) >= _SBATCH_PREFIX_PARTS and parts[3].isdigit():
                return parts[3]
        # `--parsable` output: "N" or "N;cluster"
        head = line.split(";", 1)[0]
        if head.isdigit():
            return head
    return None


def _require_cluster_id(context: TargetContext) -> int:
    if context.hpc_cluster_id is None:
        raise HpcTargetError(
            "HPC execution requires the tool to be linked to an HPC cluster. "
            "Set the tool's HPC cluster in the admin UI."
        )
    return context.hpc_cluster_id


def _scheduler_job_id_for_poll(context: TargetContext) -> str | None:
    """Return the scheduler-side job id the supervisor threaded through.

    The supervisor populates ``TargetContext.scheduler_job_id`` from
    ``Job.scheduler_job_id`` on every poll. Returns ``None`` while
    submit hasn't yet assigned an id (poll then falls back to
    heartbeat-only, which is the right behaviour during the brief
    window between staging and ``sbatch``).
    """
    return context.scheduler_job_id


def _ensure_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


# ----- shared with LocalTarget / AOS targets ----------------------------


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
    "HpcTargetError",
    "SlurmHpcTarget",
    "SshConnectOptions",
]
