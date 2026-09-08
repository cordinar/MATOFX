"""Cross-module helpers for the user→tool approver relationship.

Approvers live in two places:

1. Each :class:`matofx.users.orm.User` carries an ``is_approver`` flag.
2. Each :class:`Tool` descriptor carries an ``approver_usernames``
   list — the subset of approvers chosen for that specific tool.

When a user is deactivated or has their approver flag turned off,
the user's auto-removed from every tool's approver list. The
exception: if the user is the **only** approver on a tool, the
deactivation is refused with :class:`SoleApproverError` so the
admin gets a friendly chance to add another approver first.

This module is the *only* sanctioned place ``matofx.users`` reaches
into ``matofx.tools`` from. Both modules already share the SQLAlchemy
metadata; the dependency is one-way (users → tools via lazy import in
:mod:`matofx.users.service`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.tools.errors import SoleApproverError
from matofx.tools.orm import Tool

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


async def list_tools_referencing_approver(session: AsyncSession, *, username: str) -> list[Tool]:
    """Return every active (non-archived) tool whose descriptor lists ``username`` as an approver.

    Tool descriptors store approver usernames canonicalised lowercase
    (matching :class:`matofx.users.orm.User.username`); the supplied
    ``username`` is lower-cased before matching.
    """
    target = username.strip().lower()
    stmt = select(Tool).where(Tool.archived_at.is_(None))
    rows = list((await session.execute(stmt)).scalars())
    return [t for t in rows if target in t.descriptor.get("approver_usernames", [])]


async def remove_approver_from_tools(
    session: AsyncSession,
    *,
    username: str,
    actor_user_id: int,
    audit: AuditWriter,
    audit_action: str,
) -> list[Tool]:
    """Remove ``username`` from every tool's approver list.

    Pre-flight: refuses with :class:`SoleApproverError` if the user is
    the only approver on any tool (descriptor invariant requires at
    least one approver when ``approvals_policy != "none"``).

    On success: writes one audit event per affected tool with the
    supplied ``audit_action``, and increments each tool's
    ``row_version`` via the descriptor mutation.
    """
    target = username.strip().lower()
    affected = await list_tools_referencing_approver(session, username=target)
    sole = [
        (t.alias, t.name) for t in affected if t.descriptor.get("approver_usernames") == [target]
    ]
    if sole:
        raise SoleApproverError(target, sole)

    for tool in affected:
        descriptor = dict(tool.descriptor)
        approvers = [u for u in descriptor.get("approver_usernames", []) if u != target]
        descriptor["approver_usernames"] = approvers
        tool.descriptor = descriptor
        await audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=audit_action,
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={"alias": tool.alias, "removed_approver": target},
            ),
        )
    if affected:
        await session.flush()
    return affected
