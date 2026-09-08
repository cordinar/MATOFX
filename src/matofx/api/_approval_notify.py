"""Shared helper: email approvers when an approval request opens.

Each approval-creating endpoint calls :func:`notify_pending_approval`
after its service has opened the gate. The helper re-queries the
just-opened ``pending`` request for that target and hands it to the
notifier — fire-and-forget, mirroring the job path in
``routers/jobs.py``. A relay hiccup (or a notifier error) must never
fail the user's submit, so every error is logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.approvals.orm import ApprovalRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.notify import Notifier

_log = logging.getLogger(__name__)


async def notify_pending_approval(
    db: AsyncSession,
    notifier: Notifier,
    *,
    target_column: Any,
    target_value: int,
) -> None:
    """Notify the approvers of the newest pending request for a target.

    ``target_column`` is the :class:`ApprovalRequest` FK column for the
    kind of target (e.g. ``ApprovalRequest.library_file_version_id``);
    ``target_value`` is the row id the gate was opened for.
    """
    try:
        req = (
            (
                await db.execute(
                    select(ApprovalRequest)
                    .where(target_column == target_value)
                    .where(ApprovalRequest.status == "pending")
                    .order_by(ApprovalRequest.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if req is not None:
            await notifier.notify_approval_request_opened(db, request=req)
    except Exception:
        _log.exception("approval-opened notification failed; swallowed")
