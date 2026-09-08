"""Live Jobs — install-wide in-flight listing + bulk cancel.

Public surface for the Live Jobs tab on ``Admin → Usage & Statistics``
(gated by ``live_jobs.view`` / ``live_jobs.cancel``). See
:mod:`matofx.live_jobs.service` for the listing + cancellation logic.
"""

from __future__ import annotations

from matofx.live_jobs.service import (
    INFLIGHT_JOB_STATES,
    INFLIGHT_STUDY_STATES,
    CancelOutcome,
    CancelResult,
    LiveItem,
    LiveJobsService,
    LiveJobsView,
    LiveKind,
    LiveSubmitter,
    make_live_jobs_service,
)

__all__ = [
    "INFLIGHT_JOB_STATES",
    "INFLIGHT_STUDY_STATES",
    "CancelOutcome",
    "CancelResult",
    "LiveItem",
    "LiveJobsService",
    "LiveJobsView",
    "LiveKind",
    "LiveSubmitter",
    "make_live_jobs_service",
]
