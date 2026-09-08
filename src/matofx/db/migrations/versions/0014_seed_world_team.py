"""Seed the ``World`` team — universal-visibility marker for tools.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-07

The legacy app hardcoded a ``World`` category that meant "visible to
everyone". v2 reintroduces it as a real :class:`Team` row so the
team multi-select on the tool form can show it alongside the
admin-created teams. The visibility filter in
:meth:`ToolService.list_visible` treats ``World`` as a wildcard:
non-privileged users see a tool if its team list contains ``World``,
regardless of their own membership.

Idempotent: re-runs against an install that already has a ``World``
team are a no-op. The downgrade removes the seeded row only if it's
empty (no member users) so an admin who deliberately repurposed
``World`` doesn't lose data on rollback.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


_WORLD_NAME = "World"
_WORLD_DESCRIPTION = (
    "Universal-visibility marker. When a tool's team list contains 'World' "
    "the tool is visible to every authenticated user (subject to the tool's "
    "state-based visibility, e.g. tools in the New state still need the "
    "tools.see_new_state permission). Don't rename this team — the "
    "visibility filter looks it up by name."
)


def upgrade() -> None:
    """Insert a ``World`` team if one isn't already present."""
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT id FROM team WHERE name = :n").bindparams(n=_WORLD_NAME)
    ).fetchone()
    if existing is not None:
        return

    now = _dt.datetime.now(tz=_dt.UTC)
    op.bulk_insert(
        sa.table(
            "team",
            sa.column("name", sa.String()),
            sa.column("description", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "name": _WORLD_NAME,
                "description": _WORLD_DESCRIPTION,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    """Remove the seeded ``World`` team if it has no members.

    Skipping removal when membership exists keeps installs that have
    deliberately added users to ``World`` from losing those grants on
    a rollback.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id FROM team WHERE name = :n").bindparams(n=_WORLD_NAME)
    ).fetchone()
    if row is None:
        return
    team_id: int = row[0]
    member_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM user_team WHERE team_id = :t").bindparams(t=team_id)
    ).scalar()
    if member_count and member_count > 0:
        return
    bind.execute(sa.text("DELETE FROM team WHERE id = :t").bindparams(t=team_id))
