"""Background surrogate fits with progress (ADR-0022 §7).

Per-result tuning fits up to twelve candidates per result, so a build can take
minutes — longer than a page should hold one request open, and on large models
close to the in-app request ceiling. A fit therefore runs as an in-process
background task that the page polls.

Each job keeps a ``title`` and the ``context`` it was started with (the request
body, and the model for a refit), so the Surrogate Models page can list a user's
fits and reopen one after they left the page: a *Fit & compare* result is only
ever held here, and was otherwise lost the moment its page went away.

Deliberately small: jobs live in process memory, belong to the user who started
them, and are forgotten an hour after they finish. A restart loses running jobs,
which is safe — a fit persists nothing until it completes, and the page tells
the user the job is gone rather than waiting forever.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from matofx.models.errors import ModelError

_log = logging.getLogger(__name__)

FitJobKind = Literal["compare", "create", "refit"]
FitJobStatus = Literal["running", "done", "failed"]

_KEEP_FINISHED = _dt.timedelta(hours=1)


@dataclass
class FitJob:
    """One background fit, as the page sees it."""

    id: str
    kind: FitJobKind
    user_id: int
    title: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    status: FitJobStatus = "running"
    done: int = 0
    total: int = 0
    message: str = "Starting…"
    result: Any = None
    error: str | None = None
    started_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    finished_at: _dt.datetime | None = None
    _task: asyncio.Task[None] | None = None

    def report(self, done: int, total: int, message: str) -> None:
        """Progress callback; safe to call from the fitting worker thread."""
        self.done, self.total, self.message = done, total, message


Work = Callable[[FitJob], Awaitable[Any]]


class FitJobRegistry:
    """The process's background fits."""

    def __init__(self) -> None:
        """An empty registry."""
        self._jobs: dict[str, FitJob] = {}

    def start(
        self,
        kind: FitJobKind,
        user_id: int,
        work: Work,
        *,
        title: str = "",
        context: dict[str, Any] | None = None,
    ) -> FitJob:
        """Run ``work(job)`` in the background; its return value is the result."""
        self._prune()
        job = FitJob(
            id=uuid.uuid4().hex,
            kind=kind,
            user_id=user_id,
            title=title,
            context=dict(context or {}),
        )
        self._jobs[job.id] = job
        job._task = asyncio.create_task(self._run(job, work))
        return job

    def get(self, job_id: str, user_id: int) -> FitJob | None:
        """The job, if it exists and belongs to ``user_id``."""
        job = self._jobs.get(job_id)
        return job if job is not None and job.user_id == user_id else None

    def for_user(self, user_id: int) -> list[FitJob]:
        """The user's jobs still held, newest first."""
        self._prune()
        mine = [j for j in self._jobs.values() if j.user_id == user_id]
        return sorted(mine, key=lambda j: j.started_at, reverse=True)

    async def _run(self, job: FitJob, work: Work) -> None:
        try:
            job.result = await work(job)
            job.status = "done"
            job.message = "Finished."
        except ModelError as exc:
            job.status, job.error = "failed", str(exc)
        except Exception as exc:  # the page must learn the job ended, whatever happened
            _log.exception("background surrogate fit %s failed", job.id)
            detail = getattr(exc, "detail", None)
            job.status = "failed"
            job.error = str(detail) if detail else "The fit failed unexpectedly. Please try again."
        finally:
            job.finished_at = _dt.datetime.now(_dt.UTC)

    def _prune(self) -> None:
        cutoff = _dt.datetime.now(_dt.UTC) - _KEEP_FINISHED
        stale = [
            k for k, j in self._jobs.items() if j.finished_at is not None and j.finished_at < cutoff
        ]
        for key in stale:
            del self._jobs[key]


FIT_JOBS = FitJobRegistry()
"""The process-wide registry (the API and UI share one process)."""

__all__ = ["FIT_JOBS", "FitJob", "FitJobKind", "FitJobRegistry", "FitJobStatus"]
