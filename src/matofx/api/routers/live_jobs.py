"""HTTP API for the Live Jobs tab (``Admin → Usage & Statistics``).

Two endpoints over :class:`~matofx.live_jobs.LiveJobsService`:

- ``GET /api/admin/live-jobs`` — the install-wide in-flight list (jobs,
  workflow runs, parametric studies), gated by ``live_jobs.view``.
- ``POST /api/admin/live-jobs/cancel`` — bulk cancel, gated by
  ``live_jobs.cancel`` (super-user via the catalog sentinel; off by
  default for administrators, grantable per role).

Both surface individual records (unlike the aggregate-only Usage tabs),
so they sit behind their own permissions rather than ``usage.view``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_live_jobs_service
from matofx.api.schemas import (
    LiveJobItem,
    LiveJobsCancelOutcome,
    LiveJobsCancelRequest,
    LiveJobsCancelResponse,
    LiveJobsListResponse,
    LiveJobsSubmitter,
)
from matofx.live_jobs import LiveItem, LiveJobsService
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission

router = APIRouter()

# The closed set of row kinds a ``kind=`` filter may carry.
_VALID_KINDS: frozenset[str] = frozenset({"job", "workflow", "study"})


async def _require_live_jobs_view(session: AsyncSession, actor_user_id: int) -> None:
    """Raise 403 unless the actor holds ``live_jobs.view``."""
    if not await user_has_permission(session, actor_user_id, Permission.LIVE_JOBS_VIEW):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to view live jobs (live_jobs.view). "
                "Ask an administrator if you need access."
            ),
        )


async def _require_live_jobs_cancel(session: AsyncSession, actor_user_id: int) -> None:
    """Raise 403 unless the actor holds ``live_jobs.cancel``."""
    if not await user_has_permission(session, actor_user_id, Permission.LIVE_JOBS_CANCEL):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to cancel live jobs (live_jobs.cancel). "
                "A super-user can grant it per role in Teams, roles & permissions."
            ),
        )


def _as_utc(value: _dt.datetime) -> _dt.datetime:
    """Return ``value`` as a UTC-aware datetime (naive ⇒ assume UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value.astimezone(_dt.UTC)


def _to_item(item: LiveItem) -> LiveJobItem:
    """Map a service :class:`LiveItem` to its wire schema."""
    return LiveJobItem(
        kind=item.kind,
        id=item.id,
        name=item.name,
        state=item.state,
        submitter_user_id=item.submitter_user_id,
        submitter_name=item.submitter_name,
        submitted_at=item.submitted_at,
        project_name=item.project_name,
        scenario_name=item.scenario_name,
        child_count=item.child_count,
        cancellable=item.cancellable,
    )


@router.get("/admin/live-jobs", response_model=LiveJobsListResponse)
async def get_live_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[LiveJobsService, Depends(get_live_jobs_service)],
    kind: Annotated[list[str] | None, Query()] = None,
    submitter_id: Annotated[list[int] | None, Query()] = None,
    since: Annotated[_dt.datetime | None, Query()] = None,
    until: Annotated[_dt.datetime | None, Query()] = None,
) -> LiveJobsListResponse:
    """List every in-flight job, workflow run, and study across the install.

    ``kind`` / ``submitter_id`` are repeatable multi-select filters
    (absent ⇒ all); ``since`` / ``until`` bound the submitted timestamp.
    """
    await _require_live_jobs_view(db, current_user.id)
    kinds = tuple(k for k in (kind or ()) if k in _VALID_KINDS)
    submitters = tuple(submitter_id or ())
    view = await service.list_inflight(
        db,
        kinds=kinds,
        submitter_user_ids=submitters,
        since=_as_utc(since) if since is not None else None,
        until=_as_utc(until) if until is not None else None,
    )
    can_cancel = await user_has_permission(db, current_user.id, Permission.LIVE_JOBS_CANCEL)
    return LiveJobsListResponse(
        items=[_to_item(i) for i in view.items],
        submitters=[LiveJobsSubmitter(user_id=s.user_id, name=s.name) for s in view.submitters],
        can_cancel=can_cancel,
    )


@router.post("/admin/live-jobs/cancel", response_model=LiveJobsCancelResponse)
async def cancel_live_jobs(
    body: LiveJobsCancelRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[LiveJobsService, Depends(get_live_jobs_service)],
) -> LiveJobsCancelResponse:
    """Bulk-cancel the selected items; returns a per-item outcome list."""
    await _require_live_jobs_cancel(db, current_user.id)
    result = await service.cancel_many(
        db,
        actor_user_id=current_user.id,
        job_ids=tuple(body.job_ids),
        run_ids=tuple(body.run_ids),
        study_ids=tuple(body.study_ids),
    )
    return LiveJobsCancelResponse(
        cancelled=result.cancelled_count,
        outcomes=[
            LiveJobsCancelOutcome(kind=o.kind, id=o.id, cancelled=o.cancelled, reason=o.reason)
            for o in result.outcomes
        ],
    )
