"""Execution-target abstraction.

A :class:`ExecutionTarget` is the runner-facing interface to one
of the three places a job can run: the local host (``LocalTarget``),
an Always-On Server (``SshAosTarget`` / ``WinRmAosTarget``), or an
HPC cluster (``SlurmHpcTarget`` / ``MsHpcTarget``).

All targets share four operations:

- :meth:`ExecutionTarget.submit` — launch the staged working dir's
  ``launcher.py`` and return what the runner needs to track it
  (PID for local, scheduler job id for HPC).
- :meth:`ExecutionTarget.poll` — read the heartbeat files (and,
  for HPC, the scheduler queue) and return a :class:`JobObservation`
  the runner uses to drive the canonical state machine.
- :meth:`ExecutionTarget.cancel` — request graceful then forceful
  termination of the running job.
- :meth:`ExecutionTarget.fetch_logs` — return paths to the log
  files. Logs always live on the shared mount, so this is a
  pointer fetch, not an actual transfer.

The shared dataclasses below carry the inputs/outputs of those
methods. They're frozen so a target implementation can't mutate
the runner's view of a previous result by accident.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from matofx.tools.exec.contracts import HeartbeatStateLiteral
from matofx.tools.exec.staging import StagedWorkingDir

ExecutionTargetKind = Literal["local", "aos", "hpc"]
"""Mirror of :data:`matofx.tools.orm.Tool.execution_target` vocabulary."""


@dataclass(frozen=True)
class TargetContext:
    """Per-job dispatch metadata for the execution target.

    :class:`LocalTarget` ignores this completely. AOS targets read
    ``aos_host_id`` to pick which always-on server to connect to;
    HPC targets read ``hpc_cluster_id`` to pick which scheduler to
    submit through, plus ``scheduler_job_id`` to query the queue
    for the cluster-side state of an in-flight job.

    Constructed by the runner from the :class:`Tool` row's
    ``aos_host_id`` / ``hpc_cluster_id`` columns and the
    :class:`Job` row's ``scheduler_job_id`` at submit time, then
    threaded through the polling / cancel / fetch-logs calls so a
    target instance can be a singleton per process and still serve
    every host registered in the install.

    ``scheduler_job_id`` is ``None`` on the submit call (the id
    doesn't exist yet) and on every non-HPC call. The HPC poll
    fallback uses it to detect "scheduler reports terminal but
    launcher never wrote a heartbeat" — i.e. jobs killed by the
    cluster (walltime, OOM, manual ``scancel``).
    """

    aos_host_id: int | None = None
    hpc_cluster_id: int | None = None
    scheduler_job_id: str | None = None


@dataclass(frozen=True)
class ResourceRequest:
    """Resource hints translated per-target into scheduler directives.

    Local execution ignores most of these (the OS scheduler does
    its own thing); they're declared per-tool so AOS and HPC
    submissions can pass them through without per-target plumbing.
    """

    cpu_cores: int = 1
    memory_mb: int = 512
    walltime_minutes: int | None = None
    gpu_count: int = 0
    queue_override: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    """What :meth:`ExecutionTarget.submit` returns to the runner.

    Exactly one of ``pid`` (local / AOS) or ``scheduler_job_id``
    (HPC) is set; the other is ``None``. The runner persists
    whichever is present onto the job row so cancellation has a
    handle to target.
    """

    pid: int | None = None
    scheduler_job_id: str | None = None


@dataclass(frozen=True)
class JobObservation:
    """Snapshot of an in-flight job, returned by :meth:`ExecutionTarget.poll`.

    ``heartbeat_state`` is the closed vocabulary the launcher
    writes; the runner translates it into the canonical
    ``Job.state`` (``running``, ``complete``, ``failed``).
    ``scheduler_state`` is set only by HPC targets — it tracks
    queue position before the launcher even starts (where
    ``heartbeat_state`` is still ``"pending"``).
    """

    heartbeat_state: HeartbeatStateLiteral | None
    """``None`` when the heartbeat directory is empty (job hasn't
    started yet, e.g. still queued on HPC)."""

    started_at: _dt.datetime | None = None
    ended_at: _dt.datetime | None = None
    exit_code: int | None = None
    signal_name: str | None = None
    failure_reason: str | None = None
    scheduler_state: str | None = None
    """Free-form scheduler-side state (e.g. Slurm ``"PENDING"``,
    ``"RUNNING"``). ``None`` for non-HPC targets."""


@dataclass(frozen=True)
class LogsRef:
    """Pointers to the stdout/stderr files the launcher captured."""

    stdout_path: Path
    stderr_path: Path


class ExecutionTarget(Protocol):
    """Runner-facing protocol every concrete target implements."""

    target_kind: ExecutionTargetKind

    async def submit(
        self,
        *,
        staged: StagedWorkingDir,
        resource_request: ResourceRequest,
        context: TargetContext,
    ) -> SubmitResult:
        """Launch the staged working dir's launcher; return tracking info."""

    async def poll(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext,
    ) -> JobObservation:
        """Read heartbeat (and scheduler state for HPC); return a snapshot."""

    async def cancel(
        self,
        *,
        submit_result: SubmitResult,
        staged: StagedWorkingDir,
        grace_seconds: int,
        context: TargetContext,
    ) -> None:
        """Request graceful then forceful termination."""

    async def fetch_logs(
        self,
        *,
        staged: StagedWorkingDir,
        context: TargetContext,
    ) -> LogsRef:
        """Return paths to the captured stdout / stderr logs."""
