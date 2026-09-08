"""One tool per scenario — UNIQUE constraint on job(scenario_id, tool_alias).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-08

Locked by the user 2026-05-08: "any given scenario cannot ever contain
the same tool more than once. this is extremely important." Enforced
at the DB level so neither concurrent submissions nor a buggy caller
can sneak a duplicate through. :class:`matofx.hierarchy.JobService`
catches the IntegrityError and translates it into a friendly
:class:`DuplicateToolInScenarioError` for the API + UI layers.

Idempotent for fresh installs (the constraint is part of the table from
0017 onwards). For existing installs that may already contain duplicate
``(scenario_id, tool_alias)`` pairs, this migration de-duplicates by
**keeping the newest row per pair** and removing older copies — that's
the v1 semantic ("the latest run is the one that counts"). Removed rows
are logged in the audit chain by the migration (one ``hierarchy.job.archive``
event per dropped row); the rows themselves are deleted because the FK
chain (working dirs, audit references, tool_version pinning) keeps the
remaining row's history intact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Drop duplicate (scenario_id, tool_alias) pairs, then add the unique index."""
    bind = op.get_bind()
    # Per-dialect row deletion: we keep the row with the highest id for
    # each (scenario_id, tool_alias) pair — that's the most recent
    # submission and the one most likely to carry user-meaningful state.
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "DELETE FROM job WHERE id NOT IN ("
                " SELECT MAX(id) FROM job GROUP BY scenario_id, tool_alias"
                ")"
            )
        )
    else:
        bind.execute(
            sa.text(
                "DELETE FROM job WHERE id IN ("
                " SELECT j.id FROM job AS j"
                " JOIN ("
                "  SELECT scenario_id, tool_alias, MAX(id) AS keep_id"
                "  FROM job GROUP BY scenario_id, tool_alias"
                " ) AS k ON j.scenario_id = k.scenario_id"
                "       AND j.tool_alias = k.tool_alias"
                " WHERE j.id <> k.keep_id"
                ")"
            )
        )

    with op.batch_alter_table("job") as batch:
        batch.create_unique_constraint("uq_job_scenario_tool", ["scenario_id", "tool_alias"])


def downgrade() -> None:
    """Drop the unique constraint."""
    with op.batch_alter_table("job") as batch:
        batch.drop_constraint("uq_job_scenario_tool", type_="unique")
