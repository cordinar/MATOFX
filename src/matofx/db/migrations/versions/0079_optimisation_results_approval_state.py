"""Optimisation results-release gate state.

Revision ID: 0079
Revises: 0078
Create Date: 2026-06-15

Adds ``pending_results_approval`` to the ``optimisation_study.state`` CHECK
vocabulary (``designs/approval-detail-completeness.md`` §10). Until now an
optimisation honoured a tool's *pre* (start) approval policy but silently
dropped ``post`` / ``both`` — evaluations released as they finished. The new
state lets the runner withhold a campaign's results behind a results-release
gate (state non-terminal until an approver releases it to ``complete``).

The ``state`` column carries a single-column CHECK, but SQLite can't widen a
CHECK in place, so the table is recreated via ``batch_alter_table`` (a no-op
rewrite on Postgres). No data migration — the new value is only ever set by
the runner going forward.
"""

from __future__ import annotations

from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | None = None
depends_on: str | None = None

_OLD_STATES = (
    "draft",
    "pending_start_approval",
    "running",
    "complete",
    "cancelled",
    "failed",
)
_NEW_STATES = (
    "draft",
    "pending_start_approval",
    "running",
    "pending_results_approval",
    "complete",
    "cancelled",
    "failed",
)


def _check(states: tuple[str, ...]) -> str:
    return "state IN (" + ", ".join(repr(s) for s in states) + ")"


def upgrade() -> None:
    """Widen the state CHECK to admit ``pending_results_approval``."""
    # ``op.f`` marks the name as final so the metadata naming convention
    # (``ck_%(table_name)s_%(constraint_name)s``) isn't re-applied — the
    # constraint was minted with ``op.f`` in migration 0066.
    with op.batch_alter_table("optimisation_study", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(op.f("ck_optimisation_study_state"), type_="check")
        batch.create_check_constraint(op.f("ck_optimisation_study_state"), _check(_NEW_STATES))


def downgrade() -> None:
    """Restore the narrower CHECK (assumes no row sits in the new state)."""
    with op.batch_alter_table("optimisation_study", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(op.f("ck_optimisation_study_state"), type_="check")
        batch.create_check_constraint(op.f("ck_optimisation_study_state"), _check(_OLD_STATES))
