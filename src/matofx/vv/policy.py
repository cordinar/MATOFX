"""Submit-time V&V policy gate (ADR-0011 §6.4 / design §6.4).

A project metadata field — ``vv_required_for_submit`` — gates whether
submission against an un-V&V'd tool is allowed. The field has three
allowed values:

- ``yes`` — submission requires the tool's V&V badge to be
  ``verified_validated`` (both an approved verification AND an approved
  validation in the bundle-hash cohort).
- ``no`` / ``not_set`` — no gate (the default; ``not_set`` is the
  effective value for any project with no explicit selection).

The check lives in this module rather than directly inside
:class:`VVService` so the hierarchy / job services can call it without
a vv → hierarchy → vv import loop. Returns ``None`` on pass; raises
:class:`VVPolicyError` on block — the same shape every other
submit-time guard uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.audit.writer import AuditWriter
from matofx.hierarchy.orm import (
    ProjectMetadata,
    ProjectMetadataField,
    ProjectMetadataValue,
)
from matofx.vv.bootstrap import VV_REQUIRED_FIELD_KEY
from matofx.vv.errors import VVPolicyError
from matofx.vv.service import make_vv_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def check_submit_policy(
    session: AsyncSession,
    *,
    project_id: int,
    tool_alias: str,
    tool_version: str,
    tool_version_id: int,
    audit: AuditWriter | None = None,
) -> None:
    """Raise :class:`VVPolicyError` if the project blocks this tool version.

    Resolution order:

    1. Look up ``vv_required_for_submit`` in :class:`ProjectMetadataField`.
       If the field has never been seeded (no admin has touched the
       per-project metadata page yet, and no submit-time bootstrap has
       run on this install), the field is absent and the gate cannot
       fire — return silently.
    2. Read the project's selection. Absent or ``"no"`` or ``"not_set"``
       → no gate; return silently.
    3. Read the tool version's V&V badge via
       :meth:`VVService.summarise_tool_status`. If the badge is
       ``verified_validated`` (approved verification AND approved
       validation in the cohort), allow. Otherwise raise
       :class:`VVPolicyError` with the tool alias + version so the API
       message is precise.

    ``audit`` is optional — when supplied, a ``vv.policy.block`` event
    is emitted on block so a compliance reader sees the gate firing on
    each refused submission.
    """
    field = await session.execute(
        select(ProjectMetadataField).where(ProjectMetadataField.key == VV_REQUIRED_FIELD_KEY)
    )
    field_row = field.scalar_one_or_none()
    if field_row is None:
        return  # Field never seeded — no gate possible. Bootstrap will land it lazily.

    rows = await session.execute(
        select(ProjectMetadataValue.value)
        .select_from(ProjectMetadata)
        .join(ProjectMetadataValue, ProjectMetadataValue.id == ProjectMetadata.value_id)
        .where(ProjectMetadata.project_id == project_id)
        .where(ProjectMetadata.field_id == field_row.id)
    )
    selected = next(iter(rows.scalars()), None)
    if selected != "yes":
        return  # "no", "not_set", or no selection — gate off.

    # Gate is ON. Resolve the badge.
    svc = make_vv_service(audit or AuditWriter(signing_key=None, key_id=None))
    badge = await svc.summarise_tool_status(session, tool_version_id=tool_version_id)
    if badge.kind == "verified_validated":
        return

    # Block.
    if audit is not None:
        await audit.record(
            session,
            _block_audit_payload(
                project_id=project_id,
                tool_alias=tool_alias,
                tool_version=tool_version,
                badge_kind=badge.kind,
            ),
        )
    raise VVPolicyError(tool_alias, tool_version)


def _block_audit_payload(
    *,
    project_id: int,
    tool_alias: str,
    tool_version: str,
    badge_kind: str,
) -> RecordInput:
    """Build the ``RecordInput`` for the ``vv.policy.block`` audit event."""
    return RecordInput(
        actor="system",
        actor_type="system",
        action="vv.policy.block",
        resource_type="project",
        resource_id=str(project_id),
        outcome="success",
        payload={
            "tool_alias": tool_alias,
            "tool_version": tool_version,
            "badge_kind": badge_kind,
        },
    )
