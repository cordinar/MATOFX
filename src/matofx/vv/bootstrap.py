"""Runtime bootstrap for V&V-side runtime data (ADR-0011).

Holds the idempotent seed for the ``vv_required_for_submit`` per-project
metadata field. Per ``designs/vv-tracking.md`` §3.3 the field's seed
can't live in migration ``0056`` (which would be the natural place)
because ``project_metadata_field.created_by_user_id`` is ``NOT NULL``
with a RESTRICT FK to ``user`` and on a greenfield install no users
exist at migration time. The seed therefore runs at the first
submit-time read (lazy + idempotent), backed by the actor's user_id —
slice 7's submit-form gate is the natural caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from matofx.hierarchy.orm import ProjectMetadataField, ProjectMetadataValue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


VV_REQUIRED_FIELD_KEY = "vv_required_for_submit"
"""The unique ``key`` of the seeded project-metadata field."""

VV_REQUIRED_VALUES: tuple[str, ...] = ("yes", "no", "not_set")
"""The three allowed selections for the field. ``"not_set"`` is the
implicit default applied when no metadata row exists for a project."""


async def ensure_vv_required_for_submit_field(
    session: AsyncSession, *, created_by_user_id: int
) -> int:
    """Idempotently seed the ``vv_required_for_submit`` field + its three values.

    Returns the field id (existing or newly created). Safe to call on
    every submit; the work after the first call is a single
    ``SELECT id FROM project_metadata_field WHERE key = …`` so the per-
    request overhead is bounded.

    The field is a ``single_select`` with allowed values
    ``yes`` / ``no`` / ``not_set``. ``"not_set"`` is the seeded default
    in spirit (the gate treats a project without an explicit selection
    as "no gate" — see :func:`matofx.vv.policy.check_submit_policy`).
    """
    existing = await session.execute(
        select(ProjectMetadataField).where(ProjectMetadataField.key == VV_REQUIRED_FIELD_KEY)
    )
    field = existing.scalar_one_or_none()
    if field is not None:
        # Field present — sanity-check the three value rows are still
        # there (defensive against a curator manually deleting one via
        # the admin UI). Insert any missing ones; existing rows untouched.
        await _ensure_values(session, field_id=field.id)
        return int(field.id)

    field = ProjectMetadataField(
        key=VV_REQUIRED_FIELD_KEY,
        label="Require V&V'd tools only",
        field_type="single_select",
        required=False,
        display_order=0,
        show_in_table=False,
        active=True,
        created_by_user_id=created_by_user_id,
    )
    session.add(field)
    await session.flush()
    await _ensure_values(session, field_id=field.id)
    return int(field.id)


async def _ensure_values(session: AsyncSession, *, field_id: int) -> None:
    """Insert any of the three canonical values that aren't already present."""
    existing = {
        v
        for (v,) in (
            await session.execute(
                select(ProjectMetadataValue.value).where(ProjectMetadataValue.field_id == field_id)
            )
        ).all()
    }
    for idx, value in enumerate(VV_REQUIRED_VALUES):
        if value in existing:
            continue
        session.add(
            ProjectMetadataValue(
                field_id=field_id,
                value=value,
                display_order=idx,
                active=True,
            )
        )
    await session.flush()
