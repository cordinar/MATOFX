"""Background sweeper that deletes aged jobs, logs, and working dirs.

Retention windows are admin-tunable via the General Settings catalog
(`retain_draft_jobs_days`, `retain_job_logs_days`,
`retain_completed_workdirs_days`, `retain_failed_workdirs_days`). A
window of ``0`` means "keep forever" — that target is skipped.

Wired into :class:`matofx.runner.supervisor.RunnerSupervisor` beside
the inactivity sweeper: it runs once on daemon startup (so a long
downtime still gets cleaned up) and then every
:data:`_SWEEP_INTERVAL_HOURS`.

There is deliberately **no** audit-log retention here: the audit log
is a tamper-evident hash chain (Ed25519-signed), so deleting old rows
would break full-chain ``verify()``. It is retained in full.

Each deletion is recorded in the audit log. Every target is processed
defensively — a single failed delete is logged + audited and the sweep
continues, so one locked file can't stall the whole cleanup. Filesystem
removals run in a worker thread so a slow network mount can't block the
event loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, SupportsInt, cast

import sqlalchemy as sa

from matofx.audit.schema import RecordInput
from matofx.config.general_service import load_general_settings
from matofx.hierarchy.orm import Job
from matofx.tools.exec import LOGS_DIRNAME
from matofx.tools.exec_orm import StorageLocation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.db.session import SessionFactory

_log = logging.getLogger(__name__)

_ACTOR = "system:retention_sweeper"
_SWEEP_INTERVAL_HOURS = 6


@dataclass
class RetentionResult:
    """Counts of what one sweep deleted — for the daemon log line + tests."""

    drafts_deleted: int = 0
    log_dirs_deleted: int = 0
    completed_workdirs_deleted: int = 0
    failed_workdirs_deleted: int = 0


async def run_retention_sweeper(
    factory: SessionFactory,
    *,
    audit: AuditWriter,
    stopping: asyncio.Event,
) -> None:
    """Run one sweep on startup, then every :data:`_SWEEP_INTERVAL_HOURS`.

    Body errors are logged and swallowed so the loop never dies — the
    same posture every other supervisor loop uses.
    """
    from matofx.db.session import session_scope  # noqa: PLC0415 — avoid import cycle

    while not stopping.is_set():
        try:
            async with session_scope(factory) as session:
                result = await run_retention_pass(session, audit=audit)
                _log.info(
                    "retention_sweeper ran: drafts=%d, log_dirs=%d, "
                    "completed_workdirs=%d, failed_workdirs=%d",
                    result.drafts_deleted,
                    result.log_dirs_deleted,
                    result.completed_workdirs_deleted,
                    result.failed_workdirs_deleted,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("retention_sweeper sweep failed; continuing")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=_SWEEP_INTERVAL_HOURS * 3600)
        except TimeoutError:
            continue


async def run_retention_pass(session: AsyncSession, *, audit: AuditWriter) -> RetentionResult:
    """Delete everything past its retention window. One pass; testable.

    Reads the four retention windows from General Settings each call so
    an admin change takes effect on the next sweep without a restart. A
    window of ``0`` skips that target entirely.
    """
    settings = await load_general_settings(session)
    now = _dt.datetime.now(tz=_dt.UTC)
    result = RetentionResult()

    draft_days = _window(settings.get("retain_draft_jobs_days"))
    if draft_days:
        result.drafts_deleted = await _delete_drafts(
            session, audit, cutoff=now - _dt.timedelta(days=draft_days)
        )

    log_days = _window(settings.get("retain_job_logs_days"))
    if log_days:
        result.log_dirs_deleted = await _delete_logs(
            session, audit, cutoff=now - _dt.timedelta(days=log_days)
        )

    completed_days = _window(settings.get("retain_completed_workdirs_days"))
    if completed_days:
        result.completed_workdirs_deleted = await _delete_workdirs(
            session, audit, state="complete", cutoff=now - _dt.timedelta(days=completed_days)
        )

    failed_days = _window(settings.get("retain_failed_workdirs_days"))
    if failed_days:
        result.failed_workdirs_deleted = await _delete_workdirs(
            session, audit, state="failed", cutoff=now - _dt.timedelta(days=failed_days)
        )

    return result


# --- internals ---------------------------------------------------------


def _window(raw: object) -> int:
    """Coerce a retention-window value to a non-negative int (0 = off)."""
    try:
        return max(int(cast("SupportsInt", raw)), 0)
    except (TypeError, ValueError):
        return 0


async def _delete_drafts(session: AsyncSession, audit: AuditWriter, *, cutoff: _dt.datetime) -> int:
    """Delete unsubmitted draft jobs (``state='created'``) created before ``cutoff``.

    Drafts are DB-only — they aren't staged to disk until submit — so
    this just removes the row. Each delete runs in its own savepoint so
    one FK surprise doesn't abort the batch.
    """
    drafts = (
        (
            await session.execute(
                sa.select(Job).where(Job.state == "created").where(Job.created_at < cutoff)
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for job in drafts:
        job_id = job.id
        try:
            async with session.begin_nested():
                await session.delete(job)
        except Exception:
            _log.exception("retention: failed to delete draft job %d; skipping", job_id)
            continue
        await audit.record(
            session,
            RecordInput(
                actor=_ACTOR,
                actor_type="system",
                action="job.draft.auto_deleted",
                resource_type="job",
                resource_id=str(job_id),
                outcome="success",
                reason="Unsubmitted draft older than the retention window.",
            ),
        )
        count += 1
    return count


async def _delete_logs(session: AsyncSession, audit: AuditWriter, *, cutoff: _dt.datetime) -> int:
    """Delete the ``logs/`` subdir of finished jobs completed before ``cutoff``.

    Keeps the job row and the rest of its working directory — only the
    captured console output is removed.
    """
    jobs = (
        (
            await session.execute(
                sa.select(Job)
                .where(Job.state.in_(("complete", "failed")))
                .where(Job.completed_at.is_not(None))
                .where(Job.completed_at < cutoff)
                .where(Job.working_dir_relative.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for job in jobs:
        working_dir = await _resolve_workdir(session, job)
        if working_dir is None:
            continue
        logs_dir = working_dir / LOGS_DIRNAME
        if not await asyncio.to_thread(logs_dir.is_dir):
            continue
        if not await _rmtree(logs_dir, what=f"logs for job {job.id}"):
            continue
        await audit.record(
            session,
            RecordInput(
                actor=_ACTOR,
                actor_type="system",
                action="job.logs.auto_deleted",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                reason="Job logs older than the retention window.",
            ),
        )
        count += 1
    return count


async def _delete_workdirs(
    session: AsyncSession, audit: AuditWriter, *, state: str, cutoff: _dt.datetime
) -> int:
    """Delete the whole working directory of ``state`` jobs completed before ``cutoff``."""
    jobs = (
        (
            await session.execute(
                sa.select(Job)
                .where(Job.state == state)
                .where(Job.completed_at.is_not(None))
                .where(Job.completed_at < cutoff)
                .where(Job.working_dir_relative.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for job in jobs:
        working_dir = await _resolve_workdir(session, job)
        if working_dir is None or not await asyncio.to_thread(working_dir.is_dir):
            continue
        if not await _rmtree(working_dir, what=f"working dir for job {job.id}"):
            continue
        await audit.record(
            session,
            RecordInput(
                actor=_ACTOR,
                actor_type="system",
                action="job.workdir.auto_deleted",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                reason=(
                    f"{state.capitalize()}-job working directory older than the retention window."
                ),
            ),
        )
        count += 1
    return count


async def _resolve_workdir(session: AsyncSession, job: Job) -> Path | None:
    """Absolute on-disk working dir for a job, or ``None`` if unresolvable."""
    if not job.working_dir_relative or job.storage_location_id is None:
        return None
    location = await session.get(StorageLocation, job.storage_location_id)
    if location is None or not location.mount_path:
        return None
    return Path(location.mount_path) / job.working_dir_relative


async def _rmtree(path: Path, *, what: str) -> bool:
    """Remove a directory tree off the event loop; log + swallow failures."""
    try:
        await asyncio.to_thread(shutil.rmtree, path)
    except OSError:
        _log.exception("retention: failed to delete %s at %s; skipping", what, path)
        return False
    return True
